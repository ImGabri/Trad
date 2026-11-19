"""One-click dashboard: a tiny local web app (http://127.0.0.1:8765) that drives everything.

No extra libraries — Python's own http.server. It only listens on this PC (127.0.0.1), checks the Host
header and requires a per-session token on every action, so other websites cannot press its buttons.
"""
from __future__ import annotations

import json
import logging
import os
import secrets
import subprocess
import sys
import threading
import time
import webbrowser
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.request import Request, urlopen

import yaml

PORT = 8765
APP = Path(__file__).resolve().parent
DATA = APP / "data"
log = logging.getLogger("ui")

USER_KEYS = {  # settings the dashboard may change (written to data/user.yaml, which wins over everything)
    "equity_tp_pct": ("strategy", float, 0, 20),
    "risk_per_trade_pct": ("risk", float, 0.1, 10),
    "min_lot_max_risk_pct": ("risk", float, 0, 50),
    "daily_loss_limit_pct": ("risk", float, 1, 100),
    "max_daily_trades": ("risk", int, 1, 500),
    "asia_enabled": ("sessions", bool, None, None),
    "skip_market_open_check": ("strategy", bool, None, None),
}


# ----------------------------------------------------------------------------------- jobs
class Job:
    """One long task at a time (setup/training, connection check, kill), output streamed to the page."""

    def __init__(self):
        self.name, self.running, self.rc, self.started = "", False, None, 0.0
        self.lines: deque[str] = deque(maxlen=500)
        self.after = None  # callback(rc) when finished
        self.lock = threading.Lock()

    def start(self, name: str, args: list[str], after=None) -> bool:
        with self.lock:
            if self.running:
                return False
            self.name, self.running, self.rc, self.started, self.after = name, True, None, time.time(), after
            self.lines.clear()
        threading.Thread(target=self._run, args=(args,), daemon=True).start()
        return True

    def _run(self, args):
        exe = Path(sys.executable)
        if exe.name.lower() == "pythonw.exe":  # console python for clean stdout, but without a window
            exe = exe.with_name("python.exe")
        env = dict(os.environ, PYTHONUTF8="1", PYTHONUNBUFFERED="1")
        flags = 0x08000000 if os.name == "nt" else 0  # CREATE_NO_WINDOW
        try:
            p = subprocess.Popen([str(exe), "-X", "utf8", "-u", str(APP / "main.py"), *args], cwd=APP, env=env, stdout=subprocess.PIPE,
                                 stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, text=True, encoding="utf-8",
                                 errors="replace", creationflags=flags)
            for line in p.stdout:
                line = line.rstrip()
                if line and "Warning" not in line:
                    self.lines.append(line)
            self.rc = p.wait()
        except Exception as e:  # noqa: BLE001
            self.lines.append(f"Error: {e}")
            self.rc = -1
        if self.after:
            try:
                self.after(self.rc)
            except Exception as e:  # noqa: BLE001
                self.rc = -1
                self.lines.append(f"Error: {e}")
        self.running = False

    def state(self) -> dict:
        return dict(name=self.name, running=self.running, rc=self.rc, lines=list(self.lines)[-200:],
                    elapsed=round(time.time() - self.started) if self.started else 0,
                    #Live state fields (populated by main.py via /api/state updates):
                    ai_conf=None, ai_dir=None, rule_signal=None, session=None,
                    spread_ratio=None, atr_m5=None, entry_blocked=False)

def _json_serialize(d):
    """Recursively convert objects (datetime, np.int64, etc.) to JSON-serializable types."""
    if isinstance(d, dict):
        return {k: _json_serialize(v) for k, v in d.items()}
    if isinstance(d, (list, tuple)):
        return [_json_serialize(v) for v in d]
    if d is None or isinstance(d, (bool, int, float, str)):
        return d
    # datetime
    if hasattr(d, 'isoformat'):
        try:
            return d.isoformat()
        except Exception:
            return str(d)
    # numpy types
    if hasattr(d, 'item'):
        try:
            return d.item()
        except Exception:
            pass
    return str(d)


JOB = Job()


# ---------------------------------------------------------------------------------- state
def _read_json(p: Path) -> dict:
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (FileNotFoundError, ValueError):
        return {}


def _env() -> dict:
    out = {}
    p = APP / ".env"
    if p.exists():
        for line in p.read_text(encoding="utf-8").splitlines():
            if "=" in line and not line.strip().startswith("#"):
                k, v = line.split("=", 1)
                out[k.strip()] = v.strip()
    return out


def _write_env(updates: dict):
    p = APP / ".env"
    lines = [l for l in (p.read_text(encoding="utf-8").splitlines() if p.exists() else [])
             if l.split("=", 1)[0].strip() not in updates]
    lines += [f"{k}={v}" for k, v in updates.items() if v not in (None, "")]
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _user_settings() -> dict:
    from config import load_config
    cfg = load_config(APP / "config.yaml")
    return {k: getattr(getattr(cfg, sec), k, None) for k, (sec, *_rest) in USER_KEYS.items()}


STYLES = {  # one click instead of editing numbers
    "active": {"strategy": {"confidence_threshold": 0.5, "ai_only_min_conf": 0.0},
               "sessions": {"asia_min_confidence": 0.55}},
    "careful": {"strategy": {"confidence_threshold": 0.65, "ai_only_min_conf": 0.0},
                "sessions": {"asia_min_confidence": 0.75}},
    "turbo": {"strategy": {"mode": "turbo"}},  # exits/risk come from the config.yaml `turbo:` section
}
STYLES["perfect"] = STYLES["turbo"]  # 🎯 Perfect = best backtested strategy (improved Turbo, config `turbo:` section)
BASKET_DEFAULT = {"candles_tf": "m5", "candles": 5, "follow": True, "step_atr": 0.75, "max_entries": 2, "tp_atr": 1.0,
                  "sl_atr": 6.0, "hold_minutes": 240, "equity_per_001": 50}
STYLES["basket"] = {"strategy": {"mode": "basket"}, "basket": dict(BASKET_DEFAULT), "risk": {"max_daily_trades": 100000}}
# ☠️ Extreme: basket at maximum speed — 2 one-minute candles decide, 4 legs, quick target, 24 h, no daily cap.
# Backtest (€100): ~40 baskets/day, 78–86 % won, but blown in 2020–23, 2024–25 and Aug 2025–Nov 2026.
STYLES["extreme"] = {"strategy": {"mode": "basket"}, "risk": {"max_daily_trades": 100000},
                     "sessions": {"asia_enabled": True},
                     "basket": dict(BASKET_DEFAULT, candles_tf="m1", candles=2, step_atr=0.3, max_entries=4, tp_atr=0.5,
                                    sl_atr=3.0, hold_minutes=60)}
for _k in ("active", "careful"):  # leaving Turbo/Basket restores the scalping engine
    STYLES[_k].setdefault("strategy", {}).update(mode="scalp", ai_gate=True)


def _trades(limit: int = 15) -> tuple[list, list]:
    """Recent bot trades and the cumulative closed P&L curve, straight from data/xau.db."""
    import sqlite3
    db = DATA / "xau.db"
    if not db.exists():
        return [], []
    try:
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=2)
        rows = con.execute("SELECT ticket, side, lots, open_time, open_price, close_price, pnl, reason_close, confidence "
                           "FROM trades ORDER BY open_time DESC LIMIT ?", (limit,)).fetchall()
        curve = con.execute("SELECT close_time, pnl FROM trades WHERE pnl IS NOT NULL ORDER BY close_time "
                            "DESC LIMIT 300").fetchall()[::-1]
        con.close()
    except sqlite3.Error:
        return [], []
    keys = ["ticket", "side", "lots", "open_time", "open", "close", "pnl", "reason", "conf"]
    cum, pts = 0.0, []
    for t, p in curve:
        cum += p or 0
        pts.append([t, round(cum, 2)])
    return [dict(zip(keys, r)) for r in rows], pts


def _style() -> str:
    from config import load_config
    st = load_config(APP / "config.yaml").strategy
    if str(getattr(st, "mode", "scalp")) == "turbo":
        return "turbo"
    if str(getattr(st, "mode", "scalp")) == "basket":
        b = load_config(APP / "config.yaml").basket
        return "extreme" if str(b.candles_tf) == "m1" and int(b.candles) <= 2 else "basket"
    thr = st.confidence_threshold
    return "active" if thr <= 0.55 else "careful"


def state() -> dict:
    import background
    from monitor import Status
    meta = _read_json(DATA / "model" / "model.json")
    summ = _read_json(DATA / "report" / "summary.json")
    tuned = (DATA / "tuned.yaml").exists()
    opt = (summ.get("optimization") or {}) if summ else {}
    chosen = (opt.get("chosen") or {}).get("validate") if opt else None
    env = _env()
    return dict(
        version=(APP / "VERSION").read_text().strip() if (APP / "VERSION").exists() else "dev",
        background=background.status(APP), worker=Status.read(DATA / "status.json"), job=JOB.state(),
        model=dict(exists=bool(meta), trained_at=meta.get("trained_at", ""), rows=meta.get("rows", 0),
                   valid_auc=meta.get("valid_auc"), win_rate=meta.get("backtest_win_rate")),
        tuned=dict(adopted=tuned, validate=chosen), report=(DATA / "report" / "report.html").exists(),
        telegram=bool(env.get("TELEGRAM_TOKEN") and env.get("TELEGRAM_CHAT_ID")),
        mt5_login=env.get("MT5_LOGIN", ""), mt5_server=env.get("MT5_SERVER", ""),
        stop_file=(DATA / "STOP").exists(), settings=_user_settings(),
        check=_read_json(DATA / "mt5_check.json"), style=_style(),
        trades=_trades()[0], curve=_trades()[1],
    )


# -------------------------------------------------------------------------------- actions
def _start_bot(mode: str) -> str:
    import background
    if (DATA / "STOP").exists():
        return "Kill switch active. Review it before using Reset safety stops."
    from main import ensure_model
    if not ensure_model():  # no bundled models (source checkout): train first
        ok = JOB.start("First-time setup: downloading history, training the AI, tuning the rules",
                       ["prepare"], after=lambda rc: rc == 0 and background.start(APP, mode))
        return "Setting up (first time only) — the bot starts by itself when training is done." if ok else "Busy."
    background.start(APP, mode)
    return f"Bot started in {'DEMO TRADING' if mode == 'demo' else 'WATCH'} mode. It also starts at every Windows logon."


def _retrain() -> str:
    import background
    before = background.status(APP)
    mode = before.get("mode") if before.get("background") == "on" else None
    if mode:
        background.stop(APP)

    def after(rc):
        if mode:
            background.start(APP, mode)
    return "Retraining…" if JOB.start("Retraining on the latest data", ["prepare"], after=after) else "Busy."


def _telegram(token: str) -> str:
    from monitor import Telegram
    token = token.strip()
    if not token:
        return "Paste the token from @BotFather first."
    try:
        upd = Telegram(token, "0")._call("getUpdates", {"timeout": 0})
        chat = next(u["message"]["chat"]["id"] for u in reversed(upd.get("result", [])) if "message" in u)
    except StopIteration:
        return "Token OK — now open your bot in Telegram, send it any message, then press Connect again."
    except Exception as e:  # noqa: BLE001
        return f"Telegram error: {e}"
    _write_env({"TELEGRAM_TOKEN": token, "TELEGRAM_CHAT_ID": str(chat)})
    Telegram(token, chat).send("✅ XAU scalper connected. Commands: /status /pause /resume /stop", block=True)
    return "Telegram connected — a test message was sent. Restart the bot to apply."


def _restart_if_running() -> str:
    import background
    st = background.status(APP)
    if st.get("background") == "on":
        background.start(APP, st.get("mode") or "demo")
        return " The bot restarted with the new settings."
    return ""


def _write_user(updates: dict, replace: bool = False):
    import copy
    from config import _deep_update
    p = DATA / "user.yaml"
    cur = {} if replace else ((yaml.safe_load(p.read_text()) if p.exists() else {}) or {})
    updates = copy.deepcopy(updates)
    _deep_update(cur, updates)
    DATA.mkdir(exist_ok=True)
    p.write_text(yaml.safe_dump(cur, sort_keys=False))


def _save_settings(body: dict) -> str:
    p = DATA / "user.yaml"
    cur = yaml.safe_load(p.read_text()) if p.exists() else {}
    cur = cur or {}
    for k, v in body.items():
        if k not in USER_KEYS:
            continue
        sec, typ, lo, hi = USER_KEYS[k]
        if typ is bool:
            v = bool(v)
        else:
            v = typ(v)
            if lo is not None:
                v = max(lo, min(hi, v))
        cur.setdefault(sec, {})[k] = v
    DATA.mkdir(exist_ok=True)
    p.write_text(yaml.safe_dump(cur, sort_keys=False))
    return "Saved." + _restart_if_running()


def _open(what: str) -> str:
    target = {"report": DATA / "report" / "report.html", "logs": DATA / "logs", "folder": APP,
              "config": APP / "config.yaml"}.get(what)
    if target is None or not target.exists():
        return "Nothing to open yet."
    if os.name == "nt":
        os.startfile(target)  # noqa: S606
    return "Opened."


def action(name: str, body: dict) -> str:
    import background
    if name == "start":
        return _start_bot("watch" if body.get("mode") == "watch" else "demo")
    if name == "stop":
        background.stop(APP)
        return "Bot stopped. Open trades keep their stop-loss and take-profit at the broker."
    if name == "kill":
        return ("Closing all bot trades…" if JOB.start("Kill switch: closing all bot trades", ["kill"])
                else "Busy — try again in a moment.")
    if name == "reset":
        JOB.start("Reset", ["reset"])
        return "Kill switch and safety stops cleared."
    if name == "check":
        return "Checking MT5…" if JOB.start("Checking MT5 connection", ["doctor"]) else "Busy."
    if name == "retrain":
        return _retrain()
    if name == "telegram":
        return _telegram(body.get("token", ""))
    if name == "mt5":
        _write_env({"MT5_LOGIN": str(body.get("login", "")).strip(), "MT5_PASSWORD": str(body.get("password", "")),
                    "MT5_SERVER": str(body.get("server", "")).strip()})
        return "Saved on this PC (.env). The bot will log MT5 in by itself after a reboot."
    if name == "settings":
        return _save_settings(body)
    if name == "style":
        style = body.get("style")
        if style not in STYLES:
            return "Unknown style."
        _write_user(STYLES[style], replace=True)  # a style is a complete, tested setup — no leftover tweaks
        msg = {"active": "Active: trades every setup the AI agrees with.",
               "careful": "Careful: only high-confidence setups.",
               "turbo": "🎯 Perfect: the best backtested strategy (hourly trend breakouts, 1 % risk). Use €100+.",
               "perfect": "🎯 Perfect: the best backtested strategy (hourly trend breakouts, 1 % risk). Use €100+.",
               "extreme": "☠️ Extreme: ~40 baskets a day on 1-minute candles. Lost money in every backtest period.",
               "basket": "🧺 Basket: follows the last candles and adds legs; whole basket closes together (lost money in backtests)."}
        return msg[style] + _restart_if_running()
    if name == "test_trade":
        return ("Placing a 0.01-lot test trade on your demo…" if JOB.start("Test trade on your demo account",
                                                                            ["test-trade"]) else "Busy.")
    if name == "open":
        return _open(body.get("what", ""))
    if name == "quit":
        threading.Timer(0.5, lambda: os._exit(0)).start()
        return "Dashboard closed. The bot keeps running in the background."
    return "Unknown action."


# --------------------------------------------------------------------------------- server
class Handler(BaseHTTPRequestHandler):
    token = secrets.token_urlsafe(24)
    last_seen = time.time()
    live_state: dict = dict(ai_conf=None, ai_dir=None, rule_signal=None, session=None,
                              spread_ratio=None, atr_m5=None, entry_blocked=False,
                              # 15min AI interpretation features
                              m15_ema_fast_slope=None, m15_ema_slow_slope=None,
                              m15_atr=None, m15_momentum=None, m15_vol_skew=None,
                              vwap_dev_pct_15=None, vwma_15=None,
                              ret_15m=None, ret_45m=None, m15_range=None, m15_body=None, m15_adx=None)

    def log_message(self, *a):  # silence default stderr logging (pythonw has no console)
        pass

    def _host_ok(self) -> bool:
        return self.headers.get("Host", "") in (f"127.0.0.1:{PORT}", f"localhost:{PORT}")

    def _send(self, code: int, body: bytes, ctype="application/json"):
        self.send_response(code)
        self.send_header("Content-Type", ctype + "; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if not self._host_ok():
            return self._send(403, b"forbidden", "text/plain")
        Handler.last_seen = time.time()
        if self.path == "/":
            html = (APP / "assets" / "dashboard.html").read_text(encoding="utf-8").replace("__TOKEN__", self.token)
            return self._send(200, html.encode(), "text/html")
        if self.path == "/api/state":
            # Merge dashboard-managed state with live state from main.py (shared JSON file)
            base = state()
            try:
                shared = json.loads((APP / "data" / "state_live.json").read_text(encoding="utf-8"))
                base.update(shared)
            except Exception:
                pass
            base.update(Handler.live_state)
            return self._send(200, json.dumps(_json_serialize(base), default=str).encode())
        if self.path == "/api/ping":
            return self._send(200, b'{"app":"xau-scalper"}')
        if self.path == "/favicon.ico" and (APP / "assets" / "icon.ico").exists():
            return self._send(200, (APP / "assets" / "icon.ico").read_bytes(), "image/x-icon")
        return self._send(404, b"{}")

    def do_POST(self):
        if not self._host_ok() or self.headers.get("X-Token") != self.token:
            return self._send(403, b'{"msg":"forbidden"}')
        Handler.last_seen = time.time()
        n = int(self.headers.get("Content-Length") or 0)
        try:
            body = json.loads(self.rfile.read(n) or b"{}") if n else {}
        except ValueError:
            body = {}
        name = self.path.rsplit("/", 1)[-1]
        try:
            msg = action(name, body)
        except Exception as e:  # noqa: BLE001
            log.exception("action %s failed", name)
            msg = f"Error: {e}"
        return self._send(200, json.dumps({"msg": msg}).encode())


def _already_running() -> bool:
    try:
        with urlopen(Request(f"http://127.0.0.1:{PORT}/api/ping"), timeout=2) as r:
            return b"xau-scalper" in r.read()
    except Exception:  # noqa: BLE001
        return False


def make_shortcuts():
    """Desktop + Start-menu shortcut 'XAU Scalper' (once). Uses Windows' own WScript.Shell via PowerShell."""
    if os.name != "nt":
        return
    marker = DATA / ".shortcuts"
    if marker.exists():
        return
    q = lambda p: str(p).replace("'", "''")  # noqa: E731 — PowerShell single-quote escaping
    pyw = Path(sys.executable).with_name("pythonw.exe")
    icon = APP / "assets" / "icon.ico"
    ps = (
        "$s=New-Object -ComObject WScript.Shell;"
        "foreach($d in @([Environment]::GetFolderPath('Desktop'),[Environment]::GetFolderPath('Programs'))){"
        "$l=$s.CreateShortcut((Join-Path $d 'XAU Scalper.lnk'));"
        f"$l.TargetPath='{q(pyw)}';$l.Arguments='\"{q(APP / 'main.py')}\" ui';$l.WorkingDirectory='{q(APP)}';"
        f"$l.IconLocation='{q(icon)}';$l.Description='XAUUSD AI scalper dashboard';$l.Save()}}"
    )
    try:
        subprocess.run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", ps], timeout=30,
                       capture_output=True, creationflags=0x08000000)
        marker.write_text("ok")
    except Exception as e:  # noqa: BLE001
        log.warning("shortcut creation failed: %s", e)


def serve(open_browser: bool = True, idle_minutes: int = 30):
    url = f"http://127.0.0.1:{PORT}/"
    if _already_running():
        if open_browser:
            webbrowser.open(url)
        return
    DATA.mkdir(exist_ok=True)
    Handler.token = secrets.token_urlsafe(24)
    srv = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    srv.daemon_threads = True
    threading.Thread(target=make_shortcuts, daemon=True).start()
    if open_browser:
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()

    def idle_watch():  # close the (invisible) dashboard server when no tab has polled for a while
        while True:
            time.sleep(60)
            if time.time() - Handler.last_seen > idle_minutes * 60 and not JOB.running:
                os._exit(0)
    threading.Thread(target=idle_watch, daemon=True).start()
    srv.serve_forever()
