"""Live loop for watch (signals only) and demo (orders) modes.

Every ~0.5 s: kill-switch / Telegram commands, position management (trailing, time stop, pre-news
tightening, Friday flat). On every closed M1 bar: features → rules → AI → risk → order.
"""
from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

import execution as ex
from ai_model import DirectionModel, DriftMonitor, signal_from_proba
from data import MT5Connection, Store, mt5, tick_microstructure
from features import build_features
from monitor import Status, Telegram, fmt_entry, fmt_exit
from news import load_events, refresh_calendar
from risk import RiskState, min_required_equity, position_size, survival_breached
from strategy import compute_signals, decide_entry

log = logging.getLogger("live")
M1_HISTORY = 13_000  # enough for M15 EMA200 and the 200-bar H1 ATR percentile


class Runner:
    def __init__(self, cfg, app_dir: Path, mode: str, keep_running=lambda: True):
        assert mode in ("watch", "demo")
        self.cfg, self.mode, self.keep_running = cfg, mode, keep_running
        self.data_dir = app_dir / "data"
        self.data_dir.mkdir(exist_ok=True)
        self.stop_file = self.data_dir / "STOP"
        self.store = Store(self.data_dir / "xau.db")
        self.store.conn.execute("CREATE TABLE IF NOT EXISTS live_positions (ticket INTEGER PRIMARY KEY, "
                                "json TEXT)")
        self.model = DirectionModel.load(self.data_dir / "model")
        bt_wr = float(self.model.meta.get("backtest_win_rate", 50.0))
        self.drift = DriftMonitor(bt_wr, cfg.model.drift_window_trades, cfg.model.drift_tolerance_pct)
        self.tg = Telegram(cfg.secrets.telegram_token, cfg.secrets.telegram_chat_id)
        self.status = Status(self.data_dir / "status.json")
        self.state_path = self.data_dir / "state.json"
        self.state = self._load_state()
        self.risk = RiskState(cfg)
        self.risk.hard_stop = self.state.get("hard_stop", False)
        self.risk.drift_pause = self.state.get("drift_pause", False)
        self.paused = False
        self.events = pd.DatetimeIndex([], tz="UTC")
        self.last_cal = 0.0
        self.last_bar = None
        self.last_feat = None
        self.last_tg_poll = 0.0
        self.last_tick_log = None

    # ------------------------------------------------------------------ state
    def _load_state(self) -> dict:
        try:
            return json.loads(self.state_path.read_text())
        except (FileNotFoundError, json.JSONDecodeError):
            return {}

    def _save_state(self):
        self.state.update(hard_stop=self.risk.hard_stop, drift_pause=self.risk.drift_pause,
                          day=str(self.risk.day) if self.risk.day else None,
                          day_start_equity=self.risk.day_start_equity, trades_today=self.risk.trades_today,
                          daily_halt=self.risk.daily_halt)
        self.state_path.write_text(json.dumps(self.state, indent=2, default=str))

    def _pos_meta(self) -> dict[int, dict]:
        return {t: json.loads(j) for t, j in self.store.conn.execute("SELECT ticket, json FROM live_positions")}

    def _set_meta(self, ticket: int, meta: dict | None):
        if meta is None:
            self.store.conn.execute("DELETE FROM live_positions WHERE ticket=?", (ticket,))
        else:
            self.store.conn.execute("INSERT OR REPLACE INTO live_positions VALUES (?,?)", (ticket, json.dumps(meta)))
        self.store.conn.commit()

    # ------------------------------------------------------------------ main
    def run(self):
        cfg = self.cfg
        self.conn = MT5Connection(cfg)
        if not self.conn.connect():
            raise RuntimeError(f"Cannot connect to MT5 ({mt5.last_error()}). Open the PUPrime terminal and log in.")
        acc = mt5.account_info()
        if self.mode == "demo" and acc.trade_mode != mt5.ACCOUNT_TRADE_MODE_DEMO:
            raise RuntimeError("This MT5 account is not a demo account — demo mode refuses to trade it.")
        if self.mode == "demo" and not mt5.terminal_info().trade_allowed:
            raise RuntimeError("Enable the 'Algo Trading' button in MT5, then start again.")
        self.gold = self.conn.resolve_gold(cfg.symbols.primary)
        suffix = self.gold[len(cfg.symbols.primary):]
        self.corr = {k: self.conn.resolve_symbol(getattr(cfg.symbols, k), suffix) for k in ("dxy", "us10y", "spx")}
        self.info = mt5.symbol_info(self.gold)
        self.dev_points = int(cfg.risk.max_slippage_pips * cfg.instrument.pip_size / self.info.point)
        # restore day counters if same UTC day
        today = datetime.now(timezone.utc).date()
        if self.state.get("day") == str(today):
            self.risk.day, self.risk.day_start_equity = today, self.state["day_start_equity"]
            self.risk.trades_today, self.risk.daily_halt = self.state.get("trades_today", 0), self.state.get("daily_halt", False)
        msg = (f"▶️ XAU scalper started ({self.mode}) on {acc.server} #{acc.login}, {self.gold}, "
               f"equity {acc.equity:.2f} {acc.currency}. Correlated: {self.corr}")
        log.info(msg)
        self.tg.send(msg)
        if self.risk.hard_stop:
            self.tg.send("⛔ Survival hard stop is latched — reset it from the launcher before new entries.")
        self.status.update(state="running", mode=self.mode, symbol=self.gold, server=acc.server, detail="")
        try:
            while self.keep_running():
                if self.stop_file.exists():
                    self.kill("STOP file")
                    return
                if not self.conn.ensure_connected(on_give_up=self._on_disconnect):
                    raise RuntimeError("MT5 connection lost for 10 minutes")
                self._poll_telegram()
                self._refresh_calendar()
                self._manage_positions()
                self._check_new_bar()
                self.status.update(state="running", detail="paused" if self.paused else "")
                time.sleep(0.5)
        finally:
            self._save_state()
            self.status.update(state="stopped")

    # ------------------------------------------------------------------ helpers
    def _on_disconnect(self):
        self.tg.send("⚠️ MT5 disconnected for 10 min. Broker-side SL/TP remain on open positions.", block=True)

    def kill(self, why: str):
        n = ex.close_all(self.gold, self.dev_points, "kill") if self.mode == "demo" else 0
        self.risk.hard_stop = True
        self._save_state()
        msg = f"🛑 Kill switch ({why}): closed {n} positions, bot halted."
        log.critical(msg)
        self.tg.send(msg, block=True)
        self.status.update(state="killed", detail=why)

    def _poll_telegram(self):
        if not self.tg.enabled or time.time() - self.last_tg_poll < 5:
            return
        self.last_tg_poll = time.time()
        for cmd in self.tg.commands():
            if cmd == "/stop":
                self.stop_file.touch()
            elif cmd == "/pause":
                self.paused = True
                self.tg.send("⏸ New entries paused.")
            elif cmd == "/resume":
                self.paused, self.risk.drift_pause = False, False
                self.tg.send("▶️ Entries resumed.")
            elif cmd == "/status":
                self.tg.send(self.status_text())

    def status_text(self) -> str:
        acc = mt5.account_info()
        pos = ex.bot_positions(self.gold)
        return (f"Mode {self.mode} | equity {acc.equity:.2f} | open {len(pos)} | trades today {self.risk.trades_today}"
                f" | halt {self.risk.daily_halt} | hard stop {self.risk.hard_stop} | drift {self.risk.drift_pause}"
                f" | live WR {self.drift.live_win_rate:.0f}% vs BT {self.drift.bt:.0f}%")

    def _refresh_calendar(self):
        if time.time() - self.last_cal < 6 * 3600:
            return
        self.last_cal = time.time()
        refresh_calendar(self.cfg, self.data_dir)
        now = pd.Timestamp.now(tz="UTC")
        self.events = load_events(self.cfg, self.data_dir, now - pd.Timedelta(days=2), now + pd.Timedelta(days=8))

    def _phase_now(self) -> str:
        from strategy import news_phase
        idx = pd.DatetimeIndex([pd.Timestamp.now(tz="UTC").floor("min") - pd.Timedelta(minutes=1)])
        return news_phase(idx, self.events, self.cfg)["news_phase"].iloc[0]

    # ------------------------------------------------------------------ positions
    def _manage_positions(self):
        if self.mode != "demo":
            return
        st, nw = self.cfg.strategy, self.cfg.news
        live = {p.ticket: p for p in ex.bot_positions(self.gold)}
        meta = self._pos_meta()
        tick = mt5.symbol_info_tick(self.gold)
        now = time.time()
        phase = self._phase_now() if live else "none"
        for t, p in live.items():
            m = meta.get(t) or dict(atr=float(self.last_feat["m5_atr"]) if self.last_feat is not None else 2.0,
                                    open_ts=p.time - self.conn.server_offset.total_seconds(), best=p.price_open,
                                    side=1 if p.type == mt5.POSITION_TYPE_BUY else -1, kind="adopted", conf=0)
            side, a = m["side"], m["atr"]
            px = tick.bid if side > 0 else tick.ask
            m["best"] = max(m["best"], px) if side > 0 else min(m["best"], px)
            new_sl = None
            if (m["best"] - p.price_open) * side >= st.trail_trigger_atr * a:
                new_sl = m["best"] - side * st.trail_distance_atr * a
            if phase == "pre":
                tight = px - side * nw.pre_tighten_sl_atr * a
                new_sl = tight if new_sl is None else (max(new_sl, tight) if side > 0 else min(new_sl, tight))
            if new_sl is not None and (new_sl - p.sl) * side > 0.01:
                if ex.modify_sl(p, new_sl):
                    m["trailed"] = True
            if now - m["open_ts"] >= st.time_stop_minutes * 60:
                m["close_reason"] = "time"
                self._set_meta(t, m)
                ex.close_position(p, self.dev_points, "time")
                continue
            self._set_meta(t, m)
        # positions that disappeared → closed by SL/TP/us
        for t, m in meta.items():
            if t in live:
                continue
            res = ex.closed_pnl(t)
            if res is None:
                if now - m["open_ts"] > 3600:
                    self._set_meta(t, None)
                continue
            pnl, exit_px, comm = res
            reason = m.get("close_reason") or self._infer_reason(m, exit_px)
            hold = (now - m["open_ts"]) / 60
            self.store.conn.execute("UPDATE trades SET close_time=?, close_price=?, pnl=?, commission=?, reason_close=? "
                                    "WHERE ticket=?", (datetime.now(timezone.utc).isoformat(), exit_px, pnl, comm, reason, t))
            self.store.conn.commit()
            self._set_meta(t, None)
            self.tg.send(fmt_exit(dict(ticket=t, reason=reason, pnl=pnl, hold_min=hold)))
            log.info("closed %s %s pnl=%.2f", t, reason, pnl)
            if self.drift.add(pnl) and not self.risk.drift_pause:
                self.risk.drift_pause = True
                self._save_state()
                self.tg.send(f"⚠️ Model drift: live win rate {self.drift.live_win_rate:.0f}% vs backtest "
                             f"{self.drift.bt:.0f}% over {self.drift.window} trades. New entries paused (/resume).")

    @staticmethod
    def _infer_reason(m: dict, exit_px: float) -> str:
        if abs(exit_px - m.get("tp", np.inf)) < 0.3 * m["atr"]:
            return "tp"
        return "trail" if m.get("trailed") else "sl"

    # ------------------------------------------------------------------ bars
    def _check_new_bar(self):
        last = mt5.copy_rates_from_pos(self.gold, mt5.TIMEFRAME_M1, 1, 1)  # last CLOSED bar
        if last is None or not len(last):
            return
        t = int(last[0]["time"])
        if t == self.last_bar:
            return
        self.last_bar = t
        try:
            self._on_bar()
        except Exception:
            log.exception("bar processing failed")

    def _on_bar(self):
        cfg = self.cfg
        m1 = self.conn.rates_latest(self.gold, "M1", M1_HISTORY + 1)
        corr = {}
        for k, sym in self.corr.items():
            if sym and k in " ".join(self.model.features):
                bars = self.conn.rates_latest(sym, "M1", 300)
                corr[k] = bars["close"] if not bars.empty else None
        now = pd.Timestamp.now(tz="UTC")
        ticks = self.conn.ticks_range(self.gold, (now - pd.Timedelta(minutes=5)).to_pydatetime(), now.to_pydatetime())
        if not ticks.empty:
            new = ticks if self.last_tick_log is None else ticks[ticks.index > self.last_tick_log]
            self.store.log_spreads(self.gold, new)
            self.last_tick_log = ticks.index[-1]
        micro = tick_microstructure(ticks)
        f = build_features(m1, cfg, corr, micro, self.events)
        sig = compute_signals(f, cfg, self.events)
        row, s = f.iloc[-1], sig.iloc[-1]
        self.last_feat = row
        p_up = float(self.model.predict_proba(f.iloc[[-1]]).iloc[0])
        ai_dir, conf = signal_from_proba(pd.Series([p_up]))
        ai_dir, conf = int(ai_dir.iloc[0]), float(conf.iloc[0])
        bar_time = f.index[-1]

        acc = mt5.account_info()
        equity = acc.equity
        prev_day = self.risk.day
        self.risk.on_bar(bar_time.date(), equity)
        if prev_day and prev_day != self.risk.day:
            self.state["news_used"] = []
            self.tg.send(daily_summary(self.store, str(prev_day)))
        positions = ex.bot_positions(self.gold) if self.mode == "demo" else []
        if self.mode == "demo":
            if s["must_flat"] and positions:
                ex.close_all(self.gold, self.dev_points, "friday_flat")
                self.tg.send("🔚 Friday flat: closed all positions.")
            if not self.risk.daily_halt and self.risk.check_daily_loss(equity):
                ex.close_all(self.gold, self.dev_points, "daily_loss")
                self.tg.send(f"🛑 Daily loss limit hit ({cfg.risk.daily_loss_limit_pct}%). Halted until next UTC day.")
            tick = mt5.symbol_info_tick(self.gold)
            margin = mt5.order_calc_margin(mt5.ORDER_TYPE_BUY, self.gold, self.info.volume_min, tick.ask)
            req = min_required_equity(tick.ask, cfg.strategy.sl_atr * row["m5_atr"], self.info.trade_contract_size,
                                      acc.leverage or cfg.broker.leverage, self.info.volume_min, margin)
            if not self.risk.hard_stop and survival_breached(equity, req, cfg.risk.survival_multiplier):
                ex.close_all(self.gold, self.dev_points, "survival")
                self.risk.hard_stop = True
                self.tg.send(f"⛔ SURVIVAL HARD STOP: equity {equity:.2f} ≤ {cfg.risk.survival_multiplier}× "
                             f"{req:.2f}. All closed, no new entries.", block=True)
        news_used = set(self.state.get("news_used", []))
        side, slm, tpm, kind, why = decide_entry(
            cfg, rule=int(s["rule_signal"]), ai_dir=ai_dir, conf=conf, has_ai=True, session=s["session"],
            blocked=bool(s["entry_blocked"]), phase=s["news_phase"], event_id=int(self._event_key(s)),
            news_used=news_used, spread_ratio=float(row["spread_ratio"]), atr_m5=float(row["m5_atr"]),
            slippage_usd=self._expected_slippage())
        ok, why2 = (False, "paused") if self.paused else self.risk.can_open(
            len(positions), side, [(1 if p.type == 0 else -1, p.profit) for p in positions])
        acted = side != 0 and ok
        self.store.record_prediction(time=str(bar_time), p_up=p_up, p_down=1 - p_up, confidence=conf,
                                     model_version=self.model.meta.get("trained_at", ""), signal=side, acted=int(acted))
        log.info("bar %s close=%.2f spread=%.2f bias=%+d rule=%+d p_up=%.3f conf=%.2f phase=%s → %s",
                 bar_time.strftime("%H:%M"), row["close"], row["spread"], s["bias"], s["rule_signal"], p_up, conf,
                 s["news_phase"], (("ENTER " + ("BUY" if side > 0 else "SELL")) if acted else (why2 or why)))
        self._save_state()
        if not acted:
            return
        a = float(row["m5_atr"])
        sl_d, tp_d = slm * a, tpm * a
        from config import commission_per_side
        lots = position_size(equity, sl_d, self.info.trade_contract_size, cfg.risk.risk_per_trade_pct,
                             self.info.volume_step, self.info.volume_min, min(self.info.volume_max, cfg.instrument.max_lot),
                             commission_per_side(cfg), getattr(cfg.risk, "min_lot_max_risk_pct", 0))
        if lots <= 0:
            log.info("skip: min lot would risk more than %.2f%% of equity", cfg.risk.risk_per_trade_pct)
            return
        tick = mt5.symbol_info_tick(self.gold)
        spread = tick.ask - tick.bid
        info = dict(symbol=self.gold, side=side, lots=lots, conf=conf, spread=spread, kind=kind, session=s["session"])
        if self.mode == "watch":
            px = tick.ask if side > 0 else tick.bid
            self.tg.send("👀 (watch) " + fmt_entry(dict(info, price=px, sl=px - side * sl_d, tp=px + side * tp_d)))
            return
        fill = ex.open_market(self.gold, side, lots, sl_d, tp_d, self.dev_points, f"xau-v2 {kind} {conf:.2f}")
        if not fill.ok:
            self.tg.send(f"⚠️ Order rejected: {fill.retcode} {fill.comment}")
            return
        slip = (fill.price - fill.requested) * side
        pos = next((p for p in ex.bot_positions(self.gold) if p.ticket == fill.ticket), None)
        sl, tp = (pos.sl, pos.tp) if pos else (fill.price - side * sl_d, fill.price + side * tp_d)
        self.risk.trades_today += 1
        if kind == "news":
            self.state["news_used"] = sorted(news_used | {int(self._event_key(s))})
        self._set_meta(fill.ticket, dict(atr=a, open_ts=time.time(), best=fill.price, side=side, kind=kind,
                                         conf=conf, tp=tp, sl=sl, slip=slip))
        self.store.record_trade(ticket=fill.ticket, symbol=self.gold, side="BUY" if side > 0 else "SELL", lots=lots,
                                open_time=datetime.now(timezone.utc).isoformat(), open_price=fill.price, sl=sl, tp=tp,
                                spread_at_entry=spread, slippage=slip, confidence=conf, session=s["session"],
                                reason_open=kind, mode=self.mode)
        self._save_state()
        self.tg.send(fmt_entry(dict(info, price=fill.price, sl=sl, tp=tp)))
        if slip > self.cfg.risk.max_slippage_pips * self.cfg.instrument.pip_size:
            self.tg.send(f"⚠️ Slippage {slip:.2f} exceeded cap on #{fill.ticket}")

    def _event_key(self, s) -> int:
        """Stable id for the current event (epoch minutes) so 'one news scalp per event' survives restarts."""
        if s["news_phase"] == "none" or len(self.events) == 0:
            return -1
        now = pd.Timestamp.now(tz="UTC")
        nearest = self.events[np.argmin(np.abs((self.events - now).total_seconds()))]
        return int(nearest.timestamp() // 60)

    def _expected_slippage(self) -> float:
        r = self.store.conn.execute("SELECT slippage FROM trades WHERE slippage IS NOT NULL ORDER BY open_time DESC "
                                    "LIMIT 20").fetchall()
        return float(np.median([x[0] for x in r])) if len(r) >= 5 else 0.0


def daily_summary(store: Store, day: str) -> str:
    df = pd.read_sql_query("SELECT * FROM trades WHERE substr(open_time,1,10)=? AND pnl IS NOT NULL", store.conn,
                           params=[day])
    if df.empty:
        return f"📊 {day}: no closed trades."
    wr = (df.pnl > 0).mean() * 100
    eq = df.pnl.cumsum()
    return (f"📊 {day}: {len(df)} trades, P&L ${df.pnl.sum():+.2f}, win rate {wr:.0f}%, "
            f"max DD ${float((eq - eq.cummax()).min()):.2f}, avg AI conf {df.confidence.mean():.2f}, "
            f"avg spread ${df.spread_at_entry.mean():.2f}")
