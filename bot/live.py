"""Live loop for watch (signals only) and demo (orders) modes.

Every ~0.5 s: kill-switch / Telegram commands, position management (trailing, time stop, pre-news
tightening, Friday flat). On every closed M1 bar: features → rules → AI → risk → order.
"""
from __future__ import annotations

import json
import logging
import os
import time
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

import execution as ex
from ai_model import DirectionModel, DriftMonitor, signal_from_proba
from config import commission_per_side
from data import MT5Connection, Store, acct_per_usd, mt5, remember_terminal, tick_microstructure
from features import build_features
from learning import OutcomeModel, entry_features
from analyst import M15Analyst, chart_snapshot, m15_frame
from news_local import LocalNewsAgent
from monitor import Status, Telegram, fmt_entry, fmt_exit
from news import load_events, refresh_calendar
from risk import RiskState, min_required_equity, position_size, survival_breached
from strategy import compute_signals, decide_entry

log = logging.getLogger("live")
M1_HISTORY = 13_000  # enough for M15 EMA200 and the 200-bar H1 ATR percentile


class Runner:
    def __init__(self, cfg, app_dir: Path, mode: str, keep_running=lambda: True, auto_retrain: bool = False):
        assert mode in ("watch", "demo")
        self.cfg, self.mode, self.keep_running, self.auto_retrain = cfg, mode, keep_running, auto_retrain
        self.data_dir = app_dir / "data"
        self.data_dir.mkdir(exist_ok=True)
        self.stop_file = self.data_dir / "STOP"
        self.store = Store(self.data_dir / "xau.db")
        self.store.conn.execute("CREATE TABLE IF NOT EXISTS live_positions (ticket INTEGER PRIMARY KEY, "
                                "json TEXT)")
        self.model = DirectionModel.load(self.data_dir / "model")
        self.learn_dir = self.data_dir / "learning"
        self.outcome = OutcomeModel.load(self.learn_dir)  # learns from the bot's own trades
        self.analyst = M15Analyst.load(self.data_dir / "model")  # 15-minute chart outlook
        an = getattr(cfg, "analyst", None)
        want_news = an is not None and str(getattr(an, "news_agent", "on")).lower() not in ("off", "false", "0")
        self.news = LocalNewsAgent(self.data_dir, int(getattr(an, "news_interval_min", 15))) if want_news else None
        self.outlook: dict = {}
        self.news_track: dict = {}
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
        self.bar_detail = "Waiting for a closed M1 bar from MT5"
        self.last_bar = None
        self.last_feat = None
        self.last_tg_poll = 0.0
        self.last_tick_log = None
        self.last_ai: dict | None = None
        self.last_decision: dict = {}
        self.last_status = 0.0
        self.last_retrain_check = 0.0
        self.acct_usd = 1.0
        self.exit_reason = "stopped"

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
        if self.mode == "demo" and getattr(mt5.terminal_info(), "tradeapi_disabled", False):
            raise RuntimeError("MT5 Options > Expert Advisors: clear Disable automated trading via external Python API.")
        remember_terminal(self.data_dir)
        self.gold = self.conn.resolve_gold(cfg.symbols.primary)
        self.acct_usd = acct_per_usd(self.gold)
        suffix = self.gold[len(cfg.symbols.primary):]
        self.corr = self.conn.resolve_context(cfg, suffix)
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
        _keep_awake(True)
        try:
            while self.keep_running():
                if self.stop_file.exists():
                    self.kill("STOP file")
                    return "killed"
                if (self.data_dir / "RESET").exists():
                    self._apply_reset()
                if not self.conn.ensure_connected(on_give_up=self._on_disconnect):
                    raise RuntimeError("MT5 connection lost for 10 minutes")
                self._poll_telegram()
                self._refresh_calendar()
                self._manage_positions()
                self._check_new_bar()
                self._publish_status()
                if self._retrain_due():
                    self.exit_reason = "retrain"
                    break
                time.sleep(0.5)
        finally:
            _keep_awake(False)
            self._save_state()
            self.status.update(state="stopped" if self.exit_reason == "stopped" else self.exit_reason)
        return self.exit_reason

    # ------------------------------------------------------------------ helpers
    def _publish_status(self):
        """Rich heartbeat for the dashboard (every 2 s)."""
        if time.time() - self.last_status < 2:
            return
        self.last_status = time.time()
        acc, tick = mt5.account_info(), mt5.symbol_info_tick(self.gold)
        if acc is None:
            return
        pos = ex.bot_positions(self.gold) if self.mode == "demo" else []
        self.status.update(
            state="running", detail="paused" if self.paused else ("hard stop" if self.risk.hard_stop else
                                                                  "daily loss halt" if self.risk.daily_halt else self.bar_detail),
            mode=self.mode, login=acc.login, server=acc.server, currency=acc.currency, balance=acc.balance,
            equity=acc.equity, price=tick.bid if tick else None, spread=(tick.ask - tick.bid) if tick else None,
            positions=[dict(ticket=p.ticket, side="BUY" if p.type == 0 else "SELL", lots=p.volume,
                            open=p.price_open, sl=p.sl, tp=p.tp, profit=round(p.profit + p.swap, 2)) for p in pos],
            today=self._today_stats(), last=self.last_decision, outlook=self.outlook,
            news=dict(self.news.latest or {}, enabled=True, track=self.news_track) if self.news else {"enabled": False}, model=self.model.meta.get("trained_at", ""),
            hard_stop=self.risk.hard_stop, daily_halt=self.risk.daily_halt, drift_pause=self.risk.drift_pause)

    def _today_stats(self) -> dict:
        day = datetime.now(timezone.utc).date().isoformat()
        r = self.store.conn.execute("SELECT COUNT(*), COALESCE(SUM(pnl),0), COALESCE(SUM(pnl>0),0) FROM trades "
                                    "WHERE substr(open_time,1,10)=? AND pnl IS NOT NULL", (day,)).fetchone()
        return dict(trades=r[0], pnl=round(r[1], 2), wins=r[2], opened=self.risk.trades_today)

    def _retrain_due(self) -> bool:
        """Weekly retrain while gold is closed (Fri 22:00 → Sun 21:00 UTC), background worker only."""
        if not self.auto_retrain or time.time() - self.last_retrain_check < 600:
            return False
        self.last_retrain_check = time.time()
        now = datetime.now(timezone.utc)
        closed = now.weekday() == 5 or (now.weekday() == 4 and now.hour >= 22) or (now.weekday() == 6 and now.hour < 21)
        trained = pd.Timestamp(self.model.meta.get("trained_at", "2000-01-01"))
        age_days = (pd.Timestamp.now(tz="UTC") - trained.tz_convert("UTC") if trained.tzinfo else
                    pd.Timestamp.now() - trained).days
        due = age_days >= self.cfg.model.retrain_every_days or bool(self.model.meta.get("pretrained"))
        return closed and due and not ex.bot_positions(self.gold)

    def _ai_favours(self, side: int) -> bool:
        a = self.last_ai
        return bool(a and time.time() - a["ts"] < 180 and a["dir"] == side
                    and a["conf"] >= getattr(self.cfg.strategy, "equity_tp_hold_conf", 0.55))

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

    def _apply_reset(self):
        """Dashboard "Reset daily limits": a fresh trading day from now (loss counted from current equity), trade
        count 0, daily-loss halt / survival stop / drift pause / Telegram pause cleared. Open trades are untouched."""
        (self.data_dir / "RESET").unlink(missing_ok=True)
        acc = mt5.account_info()
        eq = float(acc.equity) if acc else self.risk.day_start_equity
        self.risk.day = datetime.now(timezone.utc).date()
        self.risk.day_start_equity, self.risk.trades_today = eq, 0
        self.risk.daily_halt = self.risk.hard_stop = self.risk.drift_pause = False
        self.paused = False
        self.drift.results.clear()
        self._save_state()
        log.info("reset: daily counters and safety stops cleared at equity %.2f", eq)
        self.tg.send(f"🔄 Reset: new day started at equity {eq:.2f}, all safety stops cleared.")

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
            elif cmd == "/reset":
                self._apply_reset()
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
        basket = self._basket_mode()
        if basket and live and getattr(self.cfg.basket, "independent", False):  # ☠️ Hyper: every trade has its own clock
            hold_s = float(self.cfg.basket.hold_minutes) * 60
            for t, p in list(live.items()):
                mm = meta.get(t) or {}
                if now - float(mm.get("open_ts", now)) >= hold_s:
                    mm["close_reason"] = "basket_time"
                    self._set_meta(t, mm)
                    ex.close_position(p, self.dev_points, "hyper time")
                    live.pop(t)
        elif basket and live:
            b = self.state.get("basket") or {}
            if b and now - b.get("open_ts", now) >= float(self.cfg.basket.hold_minutes) * 60:
                for p in live.values():
                    mm = meta.get(p.ticket) or {}
                    mm["close_reason"] = "basket_time"
                    self._set_meta(p.ticket, mm | {k: v for k, v in dict(atr=2.0, open_ts=now, best=p.price_open,
                                                                         side=1 if p.type == 0 else -1).items() if k not in mm})
                    ex.close_position(p, self.dev_points, "basket time")
                self.tg.send("🧺 Basket closed by time stop.")
                live = {}
        for t, p in live.items():
            if basket:  # basket legs share broker-side SL/TP; no per-leg trailing or equity exit
                if t not in meta:
                    self._set_meta(t, dict(atr=float(self.last_feat["m5_atr"]) if self.last_feat is not None else 2.0,
                                           open_ts=p.time - self.conn.server_offset.total_seconds(), best=p.price_open,
                                           side=1 if p.type == mt5.POSITION_TYPE_BUY else -1, kind="basket", conf=0,
                                           tp=p.tp, sl=p.sl))
                continue
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
            # equity take-profit: out at +X % of equity unless the AI still expects more
            eq_pct = float(getattr(st, "equity_tp_pct", 0) or 0)
            if eq_pct > 0 and m.get("eq_entry"):
                fee = 2 * commission_per_side(self.cfg) * p.volume * self.acct_usd
                net = p.profit + p.swap - fee
                if net >= m["eq_entry"] * eq_pct / 100:
                    if not self._ai_favours(side):
                        m["close_reason"] = "equity_tp"
                        self._set_meta(t, m)
                        ex.close_position(p, self.dev_points, "equity_tp")
                        continue
                    if not m.get("eq_hold"):
                        m["eq_hold"] = True
                        lock = p.price_open + getattr(st, "equity_tp_lock_frac", 0.5) * (px - p.price_open)
                        if (lock - p.sl) * side > 0 and ex.modify_sl(p, lock):
                            m["trailed"] = True
                        self.tg.send(f"🚀 #{t} is +{eq_pct:g}% — AI expects more ({self.last_ai['conf']:.2f}), "
                                     f"profit locked at {lock:.2f}, riding on")
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
            self.outcome.update_with_live(self.store, self.learn_dir / "sim_trades.csv")  # learn from this trade
            if self.outcome.pipe is not None:
                self.outcome.save(self.learn_dir)
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
            self.bar_detail = "Waiting for MT5 closed M1 history"
            return
        t = int(last[0]["time"])
        if t == self.last_bar:
            return
        self.last_bar = t
        try:
            self.bar_detail = ""
            self._on_bar()
        except Exception as exc:
            self.bar_detail = f"M1 processing failed: {exc}"
            log.exception("bar processing failed")

    def _on_bar(self):
        cfg = self.cfg
        m1 = self.conn.rates_latest(self.gold, "M1", M1_HISTORY + 1)
        corr = {}
        for k, sym in self.corr.items():
            used = any(c.startswith(k + "_chg") for c in self.model.features) or \
                (self.analyst is not None and any(c.startswith(k + "_r") for c in self.analyst.features))
            if sym and used:
                bars = self.conn.rates_latest(sym, "M1", 1600)  # ≥ 1 day: rolling silver beta needs it
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
        self._update_outlook(m1, corr, float(row["close"]))
        self.last_ai = dict(dir=ai_dir, conf=conf, ts=time.time())
        self.acct_usd = acct_per_usd(self.gold)

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
            margin = mt5.order_calc_margin(mt5.ORDER_TYPE_BUY, self.gold, self.info.volume_min, tick.ask) or 0.0
            atr_typ = float(row.get("m5_atr_typ", np.nan))
            atr_typ = atr_typ if np.isfinite(atr_typ) and atr_typ > 0 else float(row["m5_atr"])
            req = min_required_equity(tick.ask, cfg.strategy.sl_atr * atr_typ, self.info.trade_contract_size,
                                      acc.leverage or cfg.broker.leverage, self.info.volume_min, margin / self.acct_usd)
            if not self.risk.hard_stop and survival_breached(equity / self.acct_usd, req, cfg.risk.survival_multiplier):
                ex.close_all(self.gold, self.dev_points, "survival")
                self.risk.hard_stop = True
                self.tg.send(f"⛔ SURVIVAL HARD STOP: equity {equity:.2f} ≤ {cfg.risk.survival_multiplier}× "
                             f"{req:.2f}. All closed, no new entries.", block=True)
        if self._basket_mode():
            self._basket_step(row, s, equity, positions, bar_time, p_up, conf)
            self._save_state()
            return
        news_used = set(self.state.get("news_used", []))
        side, slm, tpm, kind, why = decide_entry(
            cfg, rule=int(s["rule_signal"]), ai_dir=ai_dir, conf=conf, has_ai=True, session=s["session"],
            blocked=bool(s["entry_blocked"]), phase=s["news_phase"], event_id=int(self._event_key(s)),
            news_used=news_used, spread_ratio=float(row["spread_ratio"]), atr_m5=float(row["m5_atr"]),
            slippage_usd=self._expected_slippage(), bias=int(s["bias"]),
            outlook_dir=int(self.outlook.get("dir", 0)), outlook_conf=float(self.outlook.get("conf", 0.0)),
            news=self._fresh_news())
        ok, why2 = (False, "paused") if self.paused else self.risk.can_open(
            len(positions), side, [(1 if p.type == 0 else -1, p.profit) for p in positions])
        x = entry_features(row, side, p_up, kind) if side != 0 else None
        if side != 0 and ok:
            allowed, p_win = self.outcome.allows(x)
            if not allowed:
                ok, why2 = False, f"self-learning veto (P(win) {p_win:.2f} < {self.outcome.threshold:.2f})"
        acted = side != 0 and ok
        self.store.record_prediction(time=str(bar_time), p_up=p_up, p_down=1 - p_up, confidence=conf,
                                     model_version=self.model.meta.get("trained_at", ""), signal=side, acted=0)
        decision = "Checking entry" if acted else (why2 or why)
        self.last_decision = dict(time=bar_time.strftime("%Y-%m-%d %H:%M"), close=round(float(row["close"]), 2),
                                  bias=int(s["bias"]), rule=int(s["rule_signal"]), p_up=round(p_up, 3),
                                  conf=round(conf, 3), session=s["session"], news=s["news_phase"], decision=decision)
        log.info("bar %s close=%.2f spread=%.2f bias=%+d rule=%+d p_up=%.3f conf=%.2f phase=%s → %s",
                 bar_time.strftime("%H:%M"), row["close"], row["spread"], s["bias"], s["rule_signal"], p_up, conf,
                 s["news_phase"], decision)
        self._save_state()
        if not acted:
            return
        a = float(row["m5_atr"])
        sl_d, tp_d = slm * a, tpm * a
        lots = position_size(equity / self.acct_usd, sl_d, self.info.trade_contract_size, cfg.risk.risk_per_trade_pct,
                             self.info.volume_step, self.info.volume_min, min(self.info.volume_max, cfg.instrument.max_lot),
                             commission_per_side(cfg), getattr(cfg.risk, "min_lot_max_risk_pct", 0))
        if lots <= 0:
            self.last_decision["decision"] = "Skipped: minimum lot exceeds the configured risk limit"
            log.info("skip: min lot would risk more than %.2f%% of equity", cfg.risk.risk_per_trade_pct)
            return
        tick = mt5.symbol_info_tick(self.gold)
        spread = tick.ask - tick.bid
        info = dict(symbol=self.gold, side=side, lots=lots, conf=conf, spread=spread, kind=kind, session=s["session"])
        side_name = "BUY" if side > 0 else "SELL"
        if self.mode == "watch":
            self.last_decision["decision"] = "Watch signal: " + side_name
            px = tick.ask if side > 0 else tick.bid
            self.tg.send("👀 (watch) " + fmt_entry(dict(info, price=px, sl=px - side * sl_d, tp=px + side * tp_d)))
            return
        fill = ex.open_market(self.gold, side, lots, sl_d, tp_d, self.dev_points, f"xau-v2 {kind} {conf:.2f}")
        if not fill.ok:
            self.last_decision["decision"] = f"MT5 rejected order ({fill.retcode}): {fill.comment}"
            log.warning("%s", self.last_decision["decision"])
            self.tg.send(f"⚠️ Order rejected: {fill.retcode} {fill.comment}")
            return
        self.last_decision["decision"] = (f"Filled {side_name} at {fill.price}" if fill.retcode == mt5.TRADE_RETCODE_DONE
                                          else f"MT5 accepted {side_name} order #{fill.ticket}")
        self.store.conn.execute("UPDATE predictions SET acted=1 WHERE time=?", (str(bar_time),))
        self.store.conn.commit()
        slip = (fill.price - fill.requested) * side
        pos = next((p for p in ex.bot_positions(self.gold)
                    if p.ticket == fill.ticket or getattr(p, "identifier", None) == fill.ticket), None)
        if pos is not None:
            fill.ticket = pos.ticket
        sl, tp = (pos.sl, pos.tp) if pos else (fill.price - side * sl_d, fill.price + side * tp_d)
        self.risk.trades_today += 1
        if kind == "news":
            self.state["news_used"] = sorted(news_used | {int(self._event_key(s))})
        self._set_meta(fill.ticket, dict(atr=a, open_ts=time.time(), best=fill.price, side=side, kind=kind,
                                         conf=conf, tp=tp, sl=sl, slip=slip, eq_entry=equity))
        self.store.record_trade(ticket=fill.ticket, symbol=self.gold, side="BUY" if side > 0 else "SELL", lots=lots,
                                open_time=datetime.now(timezone.utc).isoformat(), open_price=fill.price, sl=sl, tp=tp,
                                spread_at_entry=spread, slippage=slip, confidence=conf, session=s["session"],
                                reason_open=kind, mode=self.mode, features=json.dumps(x))
        self._save_state()
        self.tg.send(fmt_entry(dict(info, price=fill.price, sl=sl, tp=tp)))
        if slip > self.cfg.risk.max_slippage_pips * self.cfg.instrument.pip_size:
            self.tg.send(f"⚠️ Slippage {slip:.2f} exceeded cap on #{fill.ticket}")

    # ------------------------------------------------------------------ 🧺 basket mode
    def _basket_mode(self) -> bool:
        return self.mode == "demo" and str(getattr(self.cfg.strategy, "mode", "scalp")).lower() == "basket"

    def _basket_step(self, row, s, equity: float, positions: list, bar_time, p_up: float, conf: float):
        """Open a basket in the direction of the last N candles; add a leg every step×ATR against the last entry
        (up to max_entries); every leg shares the basket stop (from the first entry) and the basket target
        (average entry ± tp×ATR), so the whole basket closes together."""
        b, cfg = self.cfg.basket, self.cfg
        a = float(row["m5_atr"])
        tick = mt5.symbol_info_tick(self.gold)
        st = self.state.get("basket")
        decision = "basket: waiting for a candle run"
        if getattr(b, "independent", False):  # ☠️ Hyper: a new standalone trade on every signal, up to max_open at once
            side = int(s["rule_signal"])
            max_open = int(getattr(b, "max_open", 1))
            acc = mt5.account_info()
            lots = round(self.info.volume_min * max(1, int(equity // float(b.equity_per_001))), 2)
            margin = mt5.order_calc_margin(mt5.ORDER_TYPE_BUY if side >= 0 else mt5.ORDER_TYPE_SELL, self.gold,
                                           lots, tick.ask) or 0.0 if tick else 0.0
            if side == 0:
                decision = f"hyper: {len(positions)} open, no signal"
            elif s["entry_blocked"] or s["news_phase"] in ("pre", "event"):
                decision = "hyper: blocked (session / news)"
            elif float(row["spread_ratio"]) > cfg.risk.spread_filter_mult:
                decision = "hyper: spread too wide"
            elif self.paused or self.risk.hard_stop or self.risk.daily_halt or \
                    self.risk.trades_today >= cfg.risk.max_daily_trades:
                decision = "hyper: paused by a safety rule"
            elif len(positions) >= max_open:
                decision = f"hyper: {len(positions)}/{max_open} open (full)"
            elif not acc or acc.margin_free < 1.5 * margin:
                decision = "hyper: not enough free margin"
            else:
                sl_d, tp_d = float(b.sl_atr) * a, float(b.tp_atr) * a
                fill = ex.open_market(self.gold, side, lots, sl_d, tp_d, self.dev_points, "xau-v2 hyper")
                if fill.ok:
                    self._record_leg(fill, side, lots, fill.price - side * sl_d, fill.price + side * tp_d, a, conf, s)
                    decision = f"hyper: opened {'BUY' if side > 0 else 'SELL'} {lots} ({len(positions) + 1}/{max_open})"
                else:
                    decision = f"hyper: MT5 rejected ({fill.retcode})"
            self.last_decision = dict(time=bar_time.strftime("%Y-%m-%d %H:%M"), close=round(float(row["close"]), 2),
                                      bias=int(s["bias"]), rule=side, p_up=round(p_up, 3), conf=round(conf, 3),
                                      session=str(s["session"]), news=str(s["news_phase"]), decision=decision)
            log.info("bar %s %s", bar_time.strftime("%H:%M"), decision)
            return
        if not positions:
            self.state.pop("basket", None)
            side = int(s["rule_signal"])
            if side == 0:
                pass
            elif s["entry_blocked"] or s["news_phase"] in ("pre", "event"):
                decision = "basket: blocked (session / news)"
            elif float(row["spread_ratio"]) > cfg.risk.spread_filter_mult:
                decision = "basket: spread too wide"
            elif self.paused or self.risk.hard_stop or self.risk.daily_halt or \
                    self.risk.trades_today >= cfg.risk.max_daily_trades:
                decision = "basket: paused by a safety rule"
            else:
                lots = round(self.info.volume_min * max(1, int(equity // float(b.equity_per_001))), 2)
                sl_d, tp_d = float(b.sl_atr) * a, float(b.tp_atr) * a
                fill = ex.open_market(self.gold, side, lots, sl_d, tp_d, self.dev_points, "xau-v2 basket 1")
                if fill.ok:
                    self.state["basket"] = dict(side=side, first=fill.price, last=fill.price, n=1, open_ts=time.time(),
                                                sl=fill.price - side * sl_d, lots=lots)
                    self._record_leg(fill, side, lots, fill.price - side * sl_d, fill.price + side * tp_d, a, conf, s)
                    decision = f"basket: opened {'BUY' if side > 0 else 'SELL'} {lots}"
                    self.tg.send(f"🧺 Basket {'BUY' if side > 0 else 'SELL'} {lots} @ {fill.price:.2f} "
                                 f"(last {b.candles} {b.candles_tf.upper()} candles)")
                else:
                    decision = f"basket: MT5 rejected ({fill.retcode})"
        else:
            if not st:  # restart while a basket was open: rebuild from the positions
                side = 1 if positions[0].type == 0 else -1
                st = dict(side=side, first=positions[0].price_open, last=positions[-1].price_open, n=len(positions),
                          open_ts=min(p.time for p in positions) - self.conn.server_offset.total_seconds(),
                          sl=positions[0].sl, lots=positions[0].volume)
                self.state["basket"] = st
            side = st["side"]
            px = tick.bid if side > 0 else tick.ask
            decision = f"basket: {st['n']} leg(s) open"
            margin = mt5.order_calc_margin(mt5.ORDER_TYPE_BUY if side > 0 else mt5.ORDER_TYPE_SELL, self.gold,
                                           st["lots"], tick.ask) or 0.0
            acc = mt5.account_info()
            if st["n"] < int(b.max_entries) and (px - st["last"]) * side <= -float(b.step_atr) * a \
                    and acc and acc.margin_free > 1.5 * margin:
                entry = tick.ask if side > 0 else tick.bid
                fill = ex.open_market(self.gold, side, st["lots"], abs(entry - st["sl"]), float(b.tp_atr) * a,
                                      self.dev_points, f"xau-v2 basket {st['n'] + 1}")
                if fill.ok:
                    st["n"] += 1
                    st["last"] = fill.price
                    legs = ex.bot_positions(self.gold)
                    avg = sum(p.price_open * p.volume for p in legs) / sum(p.volume for p in legs)
                    tp = avg + side * float(b.tp_atr) * a
                    for p in legs:
                        ex.modify_tp(p, tp)
                    self._record_leg(fill, side, st["lots"], st["sl"], tp, a, conf, s)
                    decision = f"basket: added leg {st['n']}, new target {tp:.2f}"
                    self.tg.send(f"🧺 Basket leg {st['n']} @ {fill.price:.2f}, target {tp:.2f}, stop {st['sl']:.2f}")
        self.last_decision = dict(time=bar_time.strftime("%Y-%m-%d %H:%M"), close=round(float(row["close"]), 2),
                                  bias=int(s["bias"]), rule=int(s["rule_signal"]), p_up=round(p_up, 3), conf=round(conf, 3),
                                  session=str(s["session"]), news=str(s["news_phase"]), decision=decision)
        log.info("bar %s %s", bar_time.strftime("%H:%M"), decision)

    def _record_leg(self, fill, side, lots, sl, tp, atr_v, conf, s):
        self.risk.trades_today += 1
        tick = mt5.symbol_info_tick(self.gold)
        self._set_meta(fill.ticket, dict(atr=atr_v, open_ts=time.time(), best=fill.price, side=side, kind="basket",
                                         conf=conf, tp=tp, sl=sl, slip=(fill.price - fill.requested) * side))
        self.store.record_trade(ticket=fill.ticket, symbol=self.gold, side="BUY" if side > 0 else "SELL", lots=lots,
                                open_time=datetime.now(timezone.utc).isoformat(), open_price=fill.price, sl=sl, tp=tp,
                                spread_at_entry=(tick.ask - tick.bid) if tick else None,
                                slippage=(fill.price - fill.requested) * side, confidence=conf, session=str(s["session"]),
                                reason_open="basket", mode=self.mode)

    def _update_outlook(self, m1: pd.DataFrame, corr: dict, price: float):
        """On every closed 15-minute bar: model outlook for the next hour; kick off the news agent if due."""
        if self.analyst is None or m1.empty:
            return
        last_closed = m1.index[-1] + pd.Timedelta(minutes=1)          # end of the newest closed M1 bar
        m15_open = (last_closed - pd.Timedelta(minutes=15)).floor("15min")  # newest COMPLETE M15 bar
        if self.outlook.get("bar") == str(m15_open):
            return
        x15 = m15_frame(m1[m1.index < m15_open + pd.Timedelta(minutes=15)], corr, self.events)
        x15 = x15[x15.index <= m15_open]
        if x15.empty:
            return
        p = float(self.analyst.predict(x15.iloc[[-1]]).iloc[0])
        self.outlook = dict(bar=str(m15_open), p_up=round(p, 3), dir=1 if p >= 0.5 else -1, conf=round(max(p, 1 - p), 3),
                            valid_auc=self.analyst.meta.get("valid_auc"))
        if self.news is not None and self.news.due():
            now = pd.Timestamp.now(tz="UTC")
            upcoming = [str(t)[:16] for t in self.events[(self.events >= now) & (self.events <= now + pd.Timedelta(hours=6))]]
            snap = chart_snapshot(x15, p, upcoming, price)
            nxt, prv = self.events[self.events >= now], self.events[self.events < now]
            snap["minutes_to_event"] = (nxt[0] - now).total_seconds() / 60 if len(nxt) else None
            snap["minutes_since_event"] = (now - prv[-1]).total_seconds() / 60 if len(prv) else None
            self.news.run_async(snap)
            self.news_track = self.news.track_record(price)

    def _fresh_news(self) -> dict | None:
        n = self.news.latest if self.news is not None else None
        if not n:
            return None
        age = (pd.Timestamp.now(tz="UTC") - pd.Timestamp(n["time"])).total_seconds() / 60
        return n if age <= 2 * self.news.interval else None

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


def _keep_awake(on: bool):
    """Stop Windows from sleeping while the bot runs (the screen may still turn off). Reverted on exit."""
    if os.name != "nt":
        return
    import ctypes
    ES_CONTINUOUS, ES_SYSTEM_REQUIRED = 0x80000000, 0x00000001
    ctypes.windll.kernel32.SetThreadExecutionState(ES_CONTINUOUS | (ES_SYSTEM_REQUIRED if on else 0))


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
