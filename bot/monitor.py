"""Monitoring (§10): JSON logs with 7-day rotation, Telegram alerts + commands, heartbeat status file."""
from __future__ import annotations

import json
import os
import logging
import threading
import time
import urllib.parse
import urllib.request
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path


class JsonFormatter(logging.Formatter):
    def format(self, r: logging.LogRecord) -> str:
        d = {"ts": self.formatTime(r, "%Y-%m-%dT%H:%M:%S"), "level": r.levelname, "logger": r.name, "msg": r.getMessage()}
        if isinstance(r.args, dict):
            d.update(r.args)
        extra = getattr(r, "data", None)
        if extra:
            d.update(extra)
        if r.exc_info:
            d["exc"] = self.formatException(r.exc_info)
        return json.dumps(d, default=str)


def setup_logging(log_dir: Path, retention_days: int = 7, console: bool = True) -> logging.Logger:
    log_dir.mkdir(parents=True, exist_ok=True)
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    for h in list(root.handlers):
        root.removeHandler(h)
    fh = TimedRotatingFileHandler(log_dir / "bot.jsonl", when="midnight", backupCount=retention_days,
                                  encoding="utf-8", utc=True)
    fh.setFormatter(JsonFormatter())
    root.addHandler(fh)
    if console:
        ch = logging.StreamHandler()
        ch.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(message)s", "%H:%M:%S"))
        root.addHandler(ch)
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    return root


class Telegram:
    """Fire-and-forget alerts; commands /stop /status /pause /resume from the configured chat only."""

    def __init__(self, token: str | None, chat_id: str | None):
        self.token, self.chat_id = token, str(chat_id) if chat_id else None
        self.offset = 0
        self.log = logging.getLogger("telegram")

    @property
    def enabled(self) -> bool:
        return bool(self.token and self.chat_id)

    def _call(self, method: str, params: dict, timeout=10):
        url = f"https://api.telegram.org/bot{self.token}/{method}"
        data = urllib.parse.urlencode(params).encode()
        with urllib.request.urlopen(urllib.request.Request(url, data=data), timeout=timeout) as r:
            return json.loads(r.read().decode())

    def send(self, text: str, block: bool = False):
        if not self.enabled:
            return
        def go():
            try:
                self._call("sendMessage", {"chat_id": self.chat_id, "text": text[:4000]})
            except Exception as e:
                self.log.warning("telegram send failed: %s", e)
        if block:
            go()
        else:
            threading.Thread(target=go, daemon=True).start()

    def commands(self) -> list[str]:
        if not self.enabled:
            return []
        try:
            res = self._call("getUpdates", {"offset": self.offset, "timeout": 0}, timeout=8)
        except Exception:
            return []
        out = []
        for u in res.get("result", []):
            self.offset = u["update_id"] + 1
            msg = u.get("message") or {}
            if str(msg.get("chat", {}).get("id")) == self.chat_id and msg.get("text", "").startswith("/"):
                out.append(msg["text"].split()[0].lower().split("@")[0])
        return out


def atomic_write_text(path: Path, text: str, retries: int = 20, best_effort: bool = False) -> bool:
    """Write a file atomically (temp file + replace). On Windows the replace fails with PermissionError while another
    process (the dashboard, an antivirus scan, a backup tool) has the target open — retry briefly instead of crashing.
    best_effort=True: give up quietly after the retries (heartbeat files); otherwise re-raise the last error."""
    path = Path(path)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    tmp.write_text(text, encoding="utf-8")
    err = None
    for i in range(retries):
        try:
            os.replace(tmp, path)
            return True
        except PermissionError as e:  # target briefly locked by a reader (Windows)
            err = e
            time.sleep(0.02 + 0.02 * (i % 3))
    try:
        tmp.unlink()
    except OSError:
        pass
    if best_effort:
        return False
    raise err


def app_version() -> str:
    """This install's version (bot/VERSION), 'dev' when missing."""
    try:
        return Path(__file__).with_name("VERSION").read_text(encoding="utf-8").strip() or "dev"
    except OSError:
        return "dev"


class Status:
    """status.json heartbeat read by the launcher's status screen. Every heartbeat carries the version of the code
    that wrote it, so a newly installed dashboard can tell that the background bot still runs an older version."""

    def __init__(self, path: Path):
        self.path = path
        self.state: dict = {"version": app_version()}
        self._lock = threading.Lock()   # the trading loop and the startup pulse thread both write it

    def update(self, **kw):
        with self._lock:
            self.state.update(kw, heartbeat=time.time())
            text = json.dumps(self.state, default=str)
            # best effort: a heartbeat that cannot be written this time is simply written again 2 s later
            atomic_write_text(self.path, text, best_effort=True)

    def pulse(self, stop, alive=lambda: True, every: float = 15.0) -> None:
        """Keep the heartbeat fresh from a helper thread while long work runs (a retrain, the first download of
        market history) so the dashboard does not report a busy bot as dead. Stops when `stop` (an Event) is set or
        alive() turns False (no progress for too long: then the dashboard SHOULD see the missing heartbeat)."""
        while not stop.wait(every):
            if alive():
                self.update()

    @staticmethod
    def read(path: Path) -> dict:
        try:
            d = json.loads(path.read_text(encoding="utf-8"))
            d["heartbeat_age_s"] = round(time.time() - d.get("heartbeat", 0))
            return d
        except (OSError, json.JSONDecodeError):  # missing, or briefly locked / half-written on Windows
            return {}


def fmt_entry(t: dict) -> str:
    side = "BUY" if t["side"] > 0 else "SELL"
    return (f"🟢 {side} {t['symbol']} {t['lots']:.2f} lots @ {t['price']:.2f}\n"
            f"SL {t['sl']:.2f} | TP {t['tp']:.2f} | AI {t['conf']:.2f} | spread ${t['spread']:.2f}\n"
            f"{t['kind']} · {t['session']}")


def fmt_exit(t: dict) -> str:
    icon = "✅" if t["pnl"] > 0 else "🔴"
    return f"{icon} Closed #{t['ticket']} {t['reason']} — P&L ${t['pnl']:+.2f} ({t['hold_min']:.0f} min)"
