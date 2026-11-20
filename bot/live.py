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
from risk import RiskState, min_required_equity, position_size, risk_pct_for, survival_breached
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
        self._meta_json: dict[int, str] = {}  # live_positions rows as last read / written by this runner
        self.model = DirectionModel.load(self.data_dir / "model")
        self.learn_dir = self.data_dir / "learning"
        self.outcome = OutcomeModel.load(self.learn_dir)  # learns from the bot's own trades
        self.sbook, self._sig_lock, self._sig_new, self._sig_ctx = None, None, False, None
        scfg = getattr(cfg, "signals", None)
        if scfg is not None and getattr(scfg, "enabled", False) and list(getattr(scfg, "channels", []) or []):
            import threading
            from signals import SignalBook
            self.sbook = SignalBook(self.data_dir / "signals", [str(c).strip().lstrip("@") for c in scfg.channels],
                                    float(getattr(scfg, "poll_s", 20)), int(getattr(scfg, "min_graded", 100)),
                                    grade_kw=dict(max_away=float(getattr(scfg, "max_away", 5.0)),
                                                  hold_h=float(getattr(scfg, "hold_h", 8)),
                                                  valid_h=float(getattr(scfg, "valid_h", 4))),
                                    tp_mode=str(getattr(scfg, "tp_mode", "2r")))
            self._sig_lock = threading.Lock()
            self.sbook.lanes = [str(x) for x in (getattr(scfg, "lanes", None) or [])]
            if getattr(scfg, "ai_rate", False):
                from signals import SignalRater
                self.sbook.rater = SignalRater(Path(__file__).with_name("assets") / "signal_rater_train.csv",
                                               self.data_dir / "signals" / "rated_live.csv")
        import collections
        import threading
        self._sig_stop = threading.Event()       # ends the signal poller together with run()
        self._sig_gate: dict | None = None       # market state of the last closed bar (signal trades obey it too)
        self._sig_rate: dict[str, float | None] = {}   # AI rating per signal·lane (rated once, at the post time)
        self._close_retry: dict[int, float] = {}       # ticket → next close attempt (no order spam when MT5 refuses)
        self._missing: dict[int, float] = {}           # ticket → first time MT5 did not list it any more
        self._err_log: dict[str, float] = {}
        self._tg_cmds: collections.deque = collections.deque()
        self._tg_thread = self._cal_thread = None
        self._cal_loaded = False
        from memory import TradeMemory
        lc = getattr(cfg, "learning", None)
        self.memory = TradeMemory(self.learn_dir / "memory.json", seed=Path(__file__).with_name("assets") / "memory_seed.json") \
            if getattr(lc, "trade_memory", True) else None  # learns while trading which situations lose / win
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
        from monitor import atomic_write_text
        if not atomic_write_text(self.state_path, json.dumps(self.state, indent=2, default=str), best_effort=True):
            log.warning("state.json is locked by another program — will save again on the next change")

    def _pos_meta(self) -> dict[int, dict]:
        rows = self.store.conn.execute("SELECT ticket, json FROM live_positions").fetchall()
        self._meta_json = dict(rows)  # what the table holds now: lets _set_meta skip unchanged rewrites
        return {t: json.loads(j) for t, j in rows}

    def _set_meta(self, ticket: int, meta: dict | None):
        if meta is None:
            self.store.conn.execute("DELETE FROM live_positions WHERE ticket=?", (ticket,))
            self._meta_json.pop(ticket, None)
        else:
            j = json.dumps(meta)
            if self._meta_json.get(ticket) == j:  # position management runs every 0.5 s: no commit when unchanged
                return
            self.store.conn.execute("INSERT OR REPLACE INTO live_positions VALUES (?,?)", (ticket, j))
            self._meta_json[ticket] = j
        self.store.conn.commit()

    # ------------------------------------------------------------------ main
    def run(self):
        cfg = self.cfg
        t_start = time.time()
        stage = lambda txt: self.status.update(state="starting", mode=self.mode,  # noqa: E731
                                               detail=f"{txt} ({int(time.time() - t_start)} s)")
        stage("Connecting to MT5…")
        self.conn = MT5Connection(cfg)
        if not self.conn.connect():
            raise RuntimeError(self.conn.last_error or
                               f"Cannot connect to MT5 ({mt5.last_error()}). Open the PUPrime terminal and log in.")
        stage("Connected — checking the account…")
        acc = mt5.account_info()
        if self.mode == "demo" and acc.trade_mode != mt5.ACCOUNT_TRADE_MODE_DEMO:
            raise RuntimeError("This MT5 account is not a demo account — demo mode refuses to trade it.")
        if self.mode == "demo" and not mt5.terminal_info().trade_allowed:
            raise RuntimeError("Enable the 'Algo Trading' button in MT5, then start again.")
        if self.mode == "demo" and getattr(mt5.terminal_info(), "tradeapi_disabled", False):
            raise RuntimeError("MT5 Options > Expert Advisors: clear Disable automated trading via external Python API.")
        remember_terminal(self.data_dir)
        stage("Finding gold and the related markets…")
        self.gold = self.conn.resolve_gold(cfg.symbols.primary)
        self.acct_usd = acct_per_usd(self.gold)
        suffix = self.gold[len(cfg.symbols.primary):]
        self.corr = self.conn.resolve_context(cfg, suffix)
        stage(f"Found {self.gold} and {sum(1 for v in self.corr.values() if v)} related markets — starting…")
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
        if self.sbook is not None:   # 📡 channel reader: lives exactly as long as this run (no thread pile-up on restarts)
            import threading
            threading.Thread(target=self._signal_poller, daemon=True, name="signal-poller").start()
        try:
            while self.keep_running():
                if self.stop_file.exists():
                    self.kill("STOP file")
                    return "killed"
                if (self.data_dir / "RESET").exists():
                    self._apply_reset()
                if not self.conn.ensure_connected(on_give_up=self._on_disconnect):
                    raise RuntimeError("MT5 connection lost for 10 minutes")
                # one failing helper (an MT5 hiccup, a network error) must not stop trading: log it and carry on
                self._safe("time offset", self.conn.refresh_offset, self.gold)
                self._safe("telegram", self._poll_telegram)
                self._safe("calendar", self._refresh_calendar)
                self._safe("positions", self._manage_positions)
                if self._sig_new:
                    self._safe("signals", self._signal_step, immediate=True)
                self._check_new_bar()
                self._safe("status", self._publish_status)
                if self._safe("retrain check", self._retrain_due):
                    self.exit_reason = "retrain"
                    break
                time.sleep(0.5)
        finally:
            self._sig_stop.set()
            _keep_awake(False)
            self._save_state()
            self.status.update(state="stopped" if self.exit_reason == "stopped" else self.exit_reason)
        return self.exit_reason

    # ------------------------------------------------------------------ helpers
    def _safe(self, name: str, fn, *a, **kw):
        """Run one loop helper; on an error log it (once a minute per helper) and return None instead of crashing."""
        try:
            return fn(*a, **kw)
        except Exception:  # noqa: BLE001
            now = time.time()
            if now - self._err_log.get(name, 0.0) >= 60:
                self._err_log[name] = now
                log.exception("%s failed — carrying on", name)
            return None

    def _publish_status(self):
        """Rich heartbeat for the dashboard (every 2 s)."""
        if time.time() - self.last_status < 2:
            return
        self.last_status = time.time()
        acc, tick = mt5.account_info(), mt5.symbol_info_tick(self.gold)
        if acc is None:
            return
        pos = (ex.bot_positions_or_none(self.gold) or []) if self.mode == "demo" else []
        self.status.update(
            state="running", detail="paused" if self.paused else ("hard stop" if self.risk.hard_stop else
                                                                  "daily loss halt" if self.risk.daily_halt else self.bar_detail),
            mode=self.mode, login=acc.login, server=acc.server, currency=acc.currency, balance=acc.balance,
            equity=acc.equity, price=tick.bid if tick else None, spread=(tick.ask - tick.bid) if tick else None,
            positions=[dict(ticket=p.ticket, side="BUY" if p.type == 0 else "SELL", lots=p.volume,
                            open=p.price_open, sl=p.sl, tp=p.tp, profit=round(p.profit + p.swap, 2)) for p in pos],
            today=self._today_stats(), last=self.last_decision, outlook=self.outlook,
            news=dict(self.news.latest or {}, enabled=True, track=self.news_track) if self.news else {"enabled": False}, model=self.model.meta.get("trained_at", ""),
            hard_stop=self.risk.hard_stop, daily_halt=self.risk.daily_halt, drift_pause=self.risk.drift_pause,
            memory=self.memory.summary(self._fast_style()) if self.memory is not None and self._basket_mode() else None,
            signals=self._signal_status())

    def _signal_status(self):
        if self.sbook is None:
            return None
        if getattr(self, "_sig_status_cache", None) and time.time() - self._sig_status_cache[0] < 60:
            return self._sig_status_cache[1]                       # channel stats are heavy: refresh once a minute
        out = self._signal_status_now()
        self._sig_status_cache = (time.time(), out)
        return out

    def _signal_status_now(self):
        recent = [dict(channel=x.channel, time=x.time[:16].replace("T", " "), side="BUY" if x.side > 0 else "SELL",
                       zone=f"{x.zone_lo:.2f}–{x.zone_hi:.2f}", target=x.target, kind=x.kind) for x in self.sbook.signals[-6:][::-1]]
        learn = {}
        for ch in self.sbook.channels:
            e, n = self.sbook.learner.expect(ch, 1)
            e2, _ = self.sbook.learner.expect(ch, -1)
            learn[ch] = dict(buy=round(e, 2), sell=round(e2, 2), n=n)
        return dict(channels=self.sbook.channels, stats=self.sbook.stats(), errors=dict(self.sbook.errors), recent=recent,
                    min_graded=self.sbook.min_graded, learn=learn)

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
        return closed and due and ex.bot_positions_or_none(self.gold) == []   # None (MT5 not answering) = not now

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
        """Telegram commands are fetched by a short-lived thread (the HTTP call never blocks the trading loop) and
        handled here, in the loop."""
        if not self.tg.enabled:
            return
        if time.time() - self.last_tg_poll >= 5 and not (self._tg_thread and self._tg_thread.is_alive()):
            import threading
            self.last_tg_poll = time.time()
            self._tg_thread = threading.Thread(target=lambda: self._tg_cmds.extend(self.tg.commands()), daemon=True,
                                               name="telegram-commands")
            self._tg_thread.start()
        while self._tg_cmds:
            cmd = self._tg_cmds.popleft()
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
        """News times: the saved calendar at once (local files), the ForexFactory download every 6 h in a thread."""
        if time.time() - self.last_cal < 6 * 3600 or (self._cal_thread and self._cal_thread.is_alive()):
            return
        self.last_cal = time.time()

        def window():
            now = pd.Timestamp.now(tz="UTC")
            return load_events(self.cfg, self.data_dir, now - pd.Timedelta(days=2), now + pd.Timedelta(days=8))
        if not self._cal_loaded:
            self.events = window()
            self._cal_loaded = True

        def job():
            try:
                refresh_calendar(self.cfg, self.data_dir)
                self.events = window()
            except Exception:  # noqa: BLE001
                log.exception("calendar refresh")
        import threading
        self._cal_thread = threading.Thread(target=job, daemon=True, name="calendar")
        self._cal_thread.start()

    def _phase_now(self) -> str:
        from strategy import news_phase
        idx = pd.DatetimeIndex([pd.Timestamp.now(tz="UTC").floor("min") - pd.Timedelta(minutes=1)])
        return news_phase(idx, self.events, self.cfg)["news_phase"].iloc[0]

    # ------------------------------------------------------------------ positions
    def _close(self, p, why: str) -> bool:
        """Close a position; when MT5 refuses (market closed, requote …) wait 60 s before the next attempt."""
        if time.time() < self._close_retry.get(p.ticket, 0.0):
            return False
        ok = bool(ex.close_position(p, self.dev_points, why).ok)
        if ok:
            self._close_retry.pop(p.ticket, None)
        else:
            self._close_retry[p.ticket] = time.time() + 60
            log.warning("close #%s (%s) refused by MT5 — next try in 60 s", p.ticket, why)
        return ok

    def _manage_positions(self):
        if self.mode != "demo":
            return
        st, nw = self.cfg.strategy, self.cfg.news
        got = ex.bot_positions_or_none(self.gold)
        tick = mt5.symbol_info_tick(self.gold)
        if got is None or tick is None:        # MT5 did not answer: never treat that as "every trade closed"
            return
        live = {p.ticket: p for p in got}
        meta = self._pos_meta()
        now = time.time()
        phase = self._phase_now() if live else "none"
        basket = self._basket_mode()
        # basket styles own only their legs (and positions without notes); signal / other trades keep their own rules
        legs = {t: p for t, p in live.items() if (meta.get(t) or {}).get("kind", "basket") == "basket"} if basket else {}
        if basket and legs and getattr(self.cfg.basket, "independent", False):  # ☠️ Hyper: every trade has its own clock
            hold_s = float(self.cfg.basket.hold_minutes) * 60
            for t, p in list(legs.items()):
                mm = meta.get(t) or {}
                if now - float(mm.get("open_ts", now)) >= hold_s:
                    mm["close_reason"] = "basket_time"
                    self._set_meta(t, mm)
                    if self._close(p, "hyper time"):
                        live.pop(t); legs.pop(t)
        elif basket and legs:
            b = self.state.get("basket") or {}
            if b and now - b.get("open_ts", now) >= float(self.cfg.basket.hold_minutes) * 60:
                closed = 0
                for p in legs.values():
                    mm = meta.get(p.ticket) or {}
                    mm["close_reason"] = "basket_time"
                    self._set_meta(p.ticket, mm | {k: v for k, v in dict(atr=2.0, open_ts=now, best=p.price_open,
                                                                         side=1 if p.type == 0 else -1).items() if k not in mm})
                    if self._close(p, "basket time"):
                        live.pop(p.ticket); closed += 1
                if closed:
                    self.tg.send("🧺 Basket closed by time stop.")
                legs = {t: p for t, p in legs.items() if t in live}
        for t, p in live.items():
            if t in legs:  # basket legs share broker-side SL/TP; no per-leg trailing or equity exit
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
            if m.get("kind") != "signal":   # signal trades keep the channel's levels (as tested): no trail / news tightening
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
                        self._close(p, "equity_tp")
                        continue
                    if not m.get("eq_hold"):
                        m["eq_hold"] = True
                        lock = p.price_open + getattr(st, "equity_tp_lock_frac", 0.5) * (px - p.price_open)
                        if (lock - p.sl) * side > 0 and ex.modify_sl(p, lock):
                            m["trailed"] = True
                        self.tg.send(f"🚀 #{t} is +{eq_pct:g}% — AI expects more ({self.last_ai['conf']:.2f}), "
                                     f"profit locked at {lock:.2f}, riding on")
            if now - m["open_ts"] >= float(m.get("hold_min") or st.time_stop_minutes) * 60:
                m["close_reason"] = "time"
                self._set_meta(t, m)
                self._close(p, "time")
                continue
            self._set_meta(t, m)
        # positions that disappeared → closed by SL/TP/us
        for t in [t for t in self._missing if t in live]:
            self._missing.pop(t, None)
        for t, m in meta.items():
            if t in live:
                continue
            res = ex.closed_pnl(t)
            if res is None:   # not in the history (yet): forget it only after it has been missing for 10 min
                first = self._missing.setdefault(t, now)
                if now - first > 600 and now - float(m.get("open_ts", now)) > 3600:
                    self._set_meta(t, None)
                    self._missing.pop(t, None)
                continue
            self._missing.pop(t, None)
            self._close_retry.pop(t, None)
            pnl, exit_px, comm = res
            reason = m.get("close_reason") or self._infer_reason(m, exit_px)
            hold = (now - m["open_ts"]) / 60
            self.store.conn.execute("UPDATE trades SET close_time=?, close_price=?, pnl=?, commission=?, reason_close=? "
                                    "WHERE ticket=?", (datetime.now(timezone.utc).isoformat(), exit_px, pnl, comm, reason, t))
            self.store.conn.commit()
            self._set_meta(t, None)
            self.tg.send(fmt_exit(dict(ticket=t, reason=reason, pnl=pnl, hold_min=hold)))
            log.info("closed %s %s pnl=%.2f", t, reason, pnl)
            kind = m.get("kind")
            if self.memory is not None and m.get("mem"):  # learn from this trade's result in its situation
                self.memory.record(m["mem"], pnl / self.acct_usd / max(float(m.get("lots", 0.01)) / 0.01, 1e-9))
            if kind in ("signal", "adopted"):   # channel trades are graded by the signal AI; not the style's own record
                continue
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
        # cached windows: only the newest bars are downloaded each minute (identical frames, see rates_latest_cached)
        m1 = self.conn.rates_latest_cached(self.gold, "M1", M1_HISTORY + 1)
        corr = {}
        for k, sym in self.corr.items():
            used = any(c.startswith(k + "_chg") for c in self.model.features) or \
                (self.analyst is not None and any(c.startswith(k + "_r") for c in self.analyst.features))
            if sym and used:
                bars = self.conn.rates_latest_cached(sym, "M1", 1600)  # ≥ 1 day: rolling silver beta needs it
                corr[k] = bars["close"] if not bars.empty else None
        now = pd.Timestamp.now(tz="UTC")
        ticks = self.conn.ticks_range(self.gold, (now - pd.Timedelta(minutes=5)).to_pydatetime(), now.to_pydatetime())
        if not ticks.empty:
            new = ticks if self.last_tick_log is None else ticks[ticks.index > self.last_tick_log]
            self.store.log_spreads(self.gold, new)
            self.last_tick_log = ticks.index[-1]
        micro = tick_microstructure(ticks)
        f = build_features(m1, cfg, corr, micro, self.events)
        if self._turbo_mode() and (str(getattr(cfg.strategy, "turbo_regime", "") or "").lower() == "h4"
                                   or getattr(cfg.strategy, "turbo_extras", None)):
            h1 = self.conn.rates_latest_cached(self.gold, "H1", 800)   # ~33 days: H4 trend, 20-day and last-week highs
            f["h4_regime"] = self._h4_regime(f, h1)
        else:
            h1 = None
        sig = compute_signals(f, cfg, self.events, htf=h1)
        row, s = f.iloc[-1], sig.iloc[-1]
        self.last_feat = row
        if self.sbook is not None:
            try:
                hr = str(m1.index[-1].floor("15min"))
                if self.state.get("signals_graded_at") != hr:   # grade + retrain every 15 min on real prices
                    from strategy import closed_trend
                    h4s = pd.Series(f["h4_regime"].to_numpy() if "h4_regime" in f else closed_trend(f, 240, 20, 50), index=f.index)
                    with self._sig_lock:
                        self.sbook.regrade(m1, float(getattr(cfg.backtest, "spread_cost", 0.32)), f["m5_atr"], h4s)
                    self.state["signals_graded_at"] = hr
                if getattr(self.sbook, "rater", None) is not None:   # bars the rating AI reads (closed bars only)
                    from strategy import closed_trend
                    h4c = pd.Series(f["h4_regime"].to_numpy() if "h4_regime" in f else closed_trend(f, 240, 20, 50), index=f.index)
                    self._sig_ctx = (f["close"], f["m5_atr"], pd.Series(closed_trend(f, 60, 20, 50), index=f.index), h4c,
                                     f["m5_atr"].rolling(1440, min_periods=60).median())
            except Exception:  # noqa: BLE001
                log.exception("signal grading")
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
        positions = ex.bot_positions_or_none(self.gold) if self.mode == "demo" else []
        if positions is None:                  # MT5 did not list the positions: no decisions on a blind bar
            self.bar_detail = "MT5 did not report the open positions — waiting for the next bar"
            log.warning("positions_get failed (%s) — bar %s skipped", mt5.last_error(), bar_time)
            return
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
        # 📡 channel signals: after the Friday-flat / daily-loss / survival checks, with this bar's market state
        self._sig_gate = dict(t=time.time(), must_flat=bool(s["must_flat"]), closed=bool(s.get("market_closed", False)),
                              spread_avg=float(row.get("spread_avg20", np.nan)), atr=float(row["m5_atr"]))
        if self.sbook is not None:
            self._safe("signals", self._signal_step, m1=m1, row=row, atr=f["m5_atr"])
        if positions:   # signal trades have their own cap/risk: they never count against the style's entries
            _meta = self._pos_meta()
            own = [p for p in positions if (_meta.get(p.ticket) or {}).get("kind") != "signal"]
            if self._basket_mode():   # a basket only counts its own legs
                own = [p for p in own if (_meta.get(p.ticket) or {}).get("kind", "basket") == "basket"]
            positions = own
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
        rpct = risk_pct_for(cfg, (p_up if side > 0 else 1 - p_up) if self.model is not None else None)
        lots = position_size(equity / self.acct_usd, sl_d, self.info.trade_contract_size, rpct,
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

    # ------------------------------------------------------------------ 📡 live Telegram signals
    def _signal_poller(self):
        """Background thread (started by run()): fetch every channel every poll_s seconds WITHOUT holding the lock (the
        network may be slow), merge under the lock, flag new signals for the trading loop."""
        while not self._sig_stop.is_set():
            try:
                got = self.sbook.fetch_all()
                if got:
                    with self._sig_lock:
                        new = self.sbook.merge_all(got)
                    if new:
                        self._sig_new = True
                        log.info("signals: %d new signal(s)", new)
            except Exception:  # noqa: BLE001 — never let the reader kill the bot
                log.exception("signal poller")
            self._sig_stop.wait(max(5.0, self.sbook.poll_s))   # own timer; returns at once when run() ends

    ORDER_FRESH_MIN = 10      # an order post is a market order: only within 10 min of the post (as graded)
    MAX_STOP_FRAC = 0.03      # never a stop / entry more than 3 % of the price away (other instrument, typo, far limit)

    def _signals_done(self) -> dict:
        """{signal·lane key: epoch s} of signals already traded or rejected; old entries (> 2 days) are dropped."""
        d = self.state.get("signals_done")
        if isinstance(d, list):   # ≤ v2.22: a list of "channel:post:side:zone_lo[:lane]" keys
            out = {}
            for k in d:
                parts = str(k).split(":")
                if len(parts) >= 4:
                    out[f"{parts[0]}:{parts[1]}:0:{parts[4] if len(parts) > 4 else '-'}"] = time.time()
            d = out
        d = d if isinstance(d, dict) else {}
        cut = time.time() - 2 * 86400

        def ts(v):
            try:
                return float(v)
            except (TypeError, ValueError):
                return time.time()          # unreadable time: keep the key (never trade a signal twice)
        d = {str(k): ts(v) for k, v in d.items() if ts(v) >= cut}
        self.state["signals_done"] = d
        return d

    def _signal_step(self, immediate: bool = False, m1: pd.DataFrame | None = None, row=None, atr: pd.Series | None = None):
        """Trade live channel signals (both entry paths share every rule):
        * order posts (price + SL/TP): at market while ≤ 10 min old and price is still within the lane's distance;
        * zone posts: when price has tapped the zone and the LAST closed M1 bar is the confirmation candle (m1 given).
        Obeys the safety stops, news blocks, Friday flat, closed market and the spread filter; has its own open-trade cap
        and daily count (never uses the style's). With ai_rate the rating AI judges every signal per lane, once, on the
        market as it was when the signal was posted (exactly as it was trained)."""
        self._sig_new = False
        if self.sbook is None or self.mode != "demo" or self._basket_mode():
            return
        sc = self.cfg.signals
        gate = self._sig_gate
        if self.paused or self.risk.hard_stop or self.risk.daily_halt or self._phase_now() in ("pre", "event"):
            return
        if gate is None or time.time() - gate["t"] > 180 or gate["must_flat"] or gate["closed"]:
            return                                             # no fresh closed bar / Friday flat / market closed
        tick = mt5.symbol_info_tick(self.gold)
        acc = mt5.account_info()
        positions = ex.bot_positions_or_none(self.gold)
        if tick is None or acc is None or positions is None:
            return
        spread = float(tick.ask - tick.bid)
        if np.isfinite(gate["spread_avg"]) and gate["spread_avg"] > 0 and \
                spread > float(self.cfg.risk.spread_filter_mult) * gate["spread_avg"]:
            return                                             # spread blown out (rollover, news): wait
        from signals import LANES, _t0, find_confirmation, levels_for, sig_key, signal_features
        now = pd.Timestamp.now(tz="UTC")
        done = self._signals_done()
        day = str(now.date())
        if self.state.get("sig_day") != day or not isinstance(self.state.get("sig_today"), int):
            self.state["sig_day"], self.state["sig_today"] = day, 0
        max_daily = int(getattr(sc, "max_daily", 60) or 60)
        meta = self._pos_meta()
        open_sig = sum(1 for p in positions if (meta.get(p.ticket) or {}).get("kind") == "signal")
        with self._sig_lock:
            active = self.sbook.active(now, float(getattr(sc, "valid_h", 4)))
        lanes = [str(x) for x in (getattr(sc, "lanes", None) or [])] or [None]   # None = one lane from max_away / hold_h
        ai_rate = bool(getattr(sc, "ai_rate", False)) and self.sbook.rater is not None
        a_now = float(atr.iloc[-1]) if atr is not None and len(atr) else \
            float(self.last_feat["m5_atr"]) if self.last_feat is not None else float(gate["atr"])
        h4v = int(np.sign(float(self.last_feat.get("h4_regime", 0) or 0))) if self.last_feat is not None else 0
        changed = False
        for sg0 in active:
          for lane in lanes:
            sg = sg0
            lp = LANES.get(lane, {}) if lane else {}
            away = float(lp.get("max_away", getattr(sc, "max_away", 2.0)))
            hold_h = float(lp.get("hold_h", getattr(sc, "hold_h", 8)))
            key = f"{sig_key(sg)}:{lane or '-'}"
            if key in done or open_sig >= int(getattr(sc, "max_open", 2)) or self.state["sig_today"] >= max_daily:
                continue
            if getattr(sc, "require_h4", False) and h4v not in (0, sg.side):
                continue
            px = tick.ask if sg.side > 0 else tick.bid
            if abs(px - (sg.zone_lo + sg.zone_hi) / 2) > self.MAX_STOP_FRAC * px:   # posted price nowhere near gold now
                if sg.kind == "order":
                    done[key] = time.time(); changed = True    # a market order that far away is not a gold signal
                continue
            if sg.kind == "order":
                if now - _t0(sg) > pd.Timedelta(minutes=self.ORDER_FRESH_MIN):
                    done[key] = time.time(); changed = True    # stale: the move the post called is over
                    continue
                if not (sg.zone_lo - away <= px <= sg.zone_hi + away):
                    continue                                   # price away from the entry (for this lane): maybe later
                conf_far = None
            elif sg.kind == "zone" and m1 is not None and len(m1) > 2:
                c = find_confirmation(sg, m1, spread, atr, float(getattr(sc, "valid_h", 4)))
                if c is None or c[0] != len(m1) - 1:
                    continue                                   # not confirmed on the bar that just closed
                conf_far = c[1]
            else:
                continue
            p_rate = None
            if ai_rate:                                        # 🔮 the rating AI judges this signal for this lane
                if key not in self._sig_rate:
                    if self._sig_ctx is None:
                        continue                               # needs the first closed bar's market data
                    cl, at, h1s, h4s, med = self._sig_ctx
                    t_post = min(_t0(sg), cl.index[-1])
                    try:
                        self._sig_rate[key] = self.sbook.rater.predict(
                            lane or "max", signal_features(sg, t_post, cl, at, h1s, h4s, med), sg.channel)
                    except Exception:  # noqa: BLE001
                        log.exception("signal rating")
                        self._sig_rate[key] = None
                p_rate = self._sig_rate[key]
            if p_rate is not None:
                take = p_rate > float(getattr(sc, "ai_threshold", 0.0))
                size_mult = float(getattr(sc, "boost_mult", 1.0)) if p_rate > float(getattr(sc, "boost_above", 0.5)) else 1.0
                why = f"AI rates it ${p_rate:+.2f} per 0.01 lot"
            else:
                take, size_mult, exp_usd, n_gr, why = self.sbook.learner.decide(sg.channel, sg.side, None if h4v == 0 else h4v == sg.side)
            if not take:
                done[key] = time.time(); changed = True
                log.info("signal %s skipped: %s", key, why)
                continue
            if str(getattr(sc, "tp_mode", "first")).lower() == "2r":   # optimised: ignore posted TPs, target 2 × risk
                import copy as _copy
                sg = _copy.copy(sg); sg.target = None
            if conf_far is None:   # order post: posted SL if on the losing side, else 2 × ATR
                sl = sg.sl if sg.sl is not None and (px - sg.sl) * sg.side > 0 else px - sg.side * 2 * a_now
                _, tp = levels_for(sg, px, sl)
            else:
                sl, tp = levels_for(sg, px, conf_far)
            sl_d, tp_d = abs(px - sl), abs(tp - px)
            if sl_d <= 0 or tp_d <= 0:
                continue
            if sl_d > self.MAX_STOP_FRAC * px:
                done[key] = time.time(); changed = True
                log.info("signal %s skipped: stop %.2f away (> %.0f %% of the price)", key, sl_d, 100 * self.MAX_STOP_FRAC)
                continue
            lots = position_size(acc.equity / self.acct_usd, sl_d, self.info.trade_contract_size,
                                 float(getattr(sc, "risk_pct", 0.5)) * size_mult, self.info.volume_step, self.info.volume_min,
                                 min(self.info.volume_max, self.cfg.instrument.max_lot), commission_per_side(self.cfg),
                                 float(getattr(sc, "min_lot_max_risk_pct", 25) or 0))   # small accounts: 0.01 lot ≤ 25 %
            done[key] = time.time(); changed = True
            if lots <= 0:
                continue
            self._save_state()                                 # remembered BEFORE the order: a crash never repeats it
            fill = ex.open_market(self.gold, sg.side, lots, sl_d, tp_d, self.dev_points,
                                  f"xau-v2 sig{'' if not lane else lane[0]} {sg.channel}"[:31])
            if not fill.ok:
                log.warning("signal %s rejected (%s %s)", key, fill.retcode, fill.comment)
                continue
            open_sig += 1
            self.state["sig_today"] += 1
            self._set_meta(fill.ticket, dict(atr=a_now, open_ts=time.time(), best=fill.price, side=sg.side, kind="signal",
                                             conf=0, tp=fill.price + sg.side * tp_d, sl=fill.price - sg.side * sl_d,
                                             hold_min=hold_h * 60, channel=sg.channel, lots=lots))
            self.store.record_trade(ticket=fill.ticket, symbol=self.gold, side="BUY" if sg.side > 0 else "SELL", lots=lots,
                                    open_time=datetime.now(timezone.utc).isoformat(), open_price=fill.price,
                                    sl=fill.price - sg.side * sl_d, tp=fill.price + sg.side * tp_d,
                                    spread_at_entry=spread, slippage=(fill.price - fill.requested) * sg.side,
                                    confidence=float(p_rate) if p_rate is not None else 0.0, session="signal",
                                    reason_open=f"signal:{sg.channel}" + (f":{lane}" if lane else ""), mode=self.mode)
            self.tg.send(f"📡 {sg.channel}{' ' + lane if lane else ''}: {'BUY' if sg.side > 0 else 'SELL'} {lots} @ {fill.price:.2f} "
                         f"SL {fill.price - sg.side * sl_d:.2f} TP {fill.price + sg.side * tp_d:.2f}"
                         + (f" · AI ${p_rate:+.2f}" if p_rate is not None else ""))
            log.info("signal trade %s %s lots=%s", key, fill.price, lots)
        if len(self._sig_rate) > 2000:                         # ratings of expired signals are not needed any more
            self._sig_rate = {k: v for k, v in self._sig_rate.items() if k in done}
        if changed:
            self.state["signals_done"] = done
            self._save_state()

    def _turbo_mode(self) -> bool:
        return str(getattr(self.cfg.strategy, "mode", "scalp")).lower() == "turbo"

    def _h4_regime(self, f: pd.DataFrame, h1: pd.DataFrame | None = None):
        """H4 EMA20/50 trend from ~800 closed H1 bars (more history than the M1 window), aligned to f."""
        from strategy import closed_trend
        h1 = self.conn.rates_latest_cached(self.gold, "H1", 800) if h1 is None else h1
        close = h1["close"] if not h1.empty else None
        return closed_trend(f, 240, 20, 50, close=close)

    def _fast_style(self) -> str:
        b = self.cfg.basket
        if getattr(b, "independent", False):
            return "hyper"
        return "extreme" if str(b.candles_tf) == "m1" and int(b.candles) <= 2 else "basket"

    def _memory_gate(self, row, side: int, bar_time) -> tuple[str | None, bool, str]:
        """(situation key, allowed, reason) from the trade memory."""
        if self.memory is None or side == 0:
            return None, True, ""
        from memory import situation
        key = situation(self._fast_style(), side, int(pd.Timestamp(bar_time).hour), float(row["m5_atr"]),
                        float(row.get("m5_atr_typ", float("nan"))))
        ok, why = self.memory.check(key)
        return key, ok, why

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
            elif not (mg := self._memory_gate(row, side, bar_time))[1]:
                decision = mg[2]
            else:
                sl_d, tp_d = float(b.sl_atr) * a, float(b.tp_atr) * a
                fill = ex.open_market(self.gold, side, lots, sl_d, tp_d, self.dev_points, "xau-v2 hyper")
                if fill.ok:
                    self._record_leg(fill, side, lots, fill.price - side * sl_d, fill.price + side * tp_d, a, conf, s, mg[0])
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
            elif not (mg := self._memory_gate(row, side, bar_time))[1]:
                decision = mg[2]
            else:
                lots = round(self.info.volume_min * max(1, int(equity // float(b.equity_per_001))), 2)
                sl_d, tp_d = float(b.sl_atr) * a, float(b.tp_atr) * a
                fill = ex.open_market(self.gold, side, lots, sl_d, tp_d, self.dev_points, "xau-v2 basket 1")
                if fill.ok:
                    self.state["basket"] = dict(side=side, first=fill.price, last=fill.price, n=1, open_ts=time.time(),
                                                sl=fill.price - side * sl_d, lots=lots, mem=mg[0])
                    self._record_leg(fill, side, lots, fill.price - side * sl_d, fill.price + side * tp_d, a, conf, s, mg[0])
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
                    _mm = self._pos_meta()
                    legs = [p for p in ex.bot_positions(self.gold) if p.ticket == fill.ticket or
                            (_mm.get(p.ticket) or {}).get("kind", "basket") == "basket"]   # this basket's legs only
                    avg = sum(p.price_open * p.volume for p in legs) / sum(p.volume for p in legs)
                    tp = avg + side * float(b.tp_atr) * a
                    for p in legs:
                        ex.modify_tp(p, tp)
                    self._record_leg(fill, side, st["lots"], st["sl"], tp, a, conf, s, st.get("mem"))
                    decision = f"basket: added leg {st['n']}, new target {tp:.2f}"
                    self.tg.send(f"🧺 Basket leg {st['n']} @ {fill.price:.2f}, target {tp:.2f}, stop {st['sl']:.2f}")
        self.last_decision = dict(time=bar_time.strftime("%Y-%m-%d %H:%M"), close=round(float(row["close"]), 2),
                                  bias=int(s["bias"]), rule=int(s["rule_signal"]), p_up=round(p_up, 3), conf=round(conf, 3),
                                  session=str(s["session"]), news=str(s["news_phase"]), decision=decision)
        log.info("bar %s %s", bar_time.strftime("%H:%M"), decision)

    def _record_leg(self, fill, side, lots, sl, tp, atr_v, conf, s, mem: str | None = None):
        self.risk.trades_today += 1
        tick = mt5.symbol_info_tick(self.gold)
        self._set_meta(fill.ticket, dict(atr=atr_v, open_ts=time.time(), best=fill.price, side=side, kind="basket",
                                         conf=conf, tp=tp, sl=sl, slip=(fill.price - fill.requested) * side,
                                         lots=lots, mem=mem))
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
