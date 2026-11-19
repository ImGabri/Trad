"""Monitoring (§10): JSON logs with 7-day rotation, Telegram alerts + commands, heartbeat status file."""
from __future__ import annotations

import json
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


class Status:
    """status.json heartbeat read by the launcher's status screen."""

    def __init__(self, path: Path):
        self.path = path
        self.state: dict = {}

    def update(self, **kw):
        self.state.update(kw, heartbeat=time.time())
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.state, indent=2, default=str), encoding="utf-8")
        tmp.replace(self.path)

    @staticmethod
    def read(path: Path) -> dict:
        try:
            d = json.loads(path.read_text(encoding="utf-8"))
            d["heartbeat_age_s"] = round(time.time() - d.get("heartbeat", 0))
            return d
        except (FileNotFoundError, json.JSONDecodeError):
            return {}


def fmt_entry(t: dict) -> str:
    side = "BUY" if t["side"] > 0 else "SELL"
    return (f"🟢 {side} {t['symbol']} {t['lots']:.2f} lots @ {t['price']:.2f}\n"
            f"SL {t['sl']:.2f} | TP {t['tp']:.2f} | AI {t['conf']:.2f} | spread ${t['spread']:.2f}\n"
            f"{t['kind']} · {t['session']}")


def fmt_exit(t: dict) -> str:
    icon = "✅" if t["pnl"] > 0 else "🔴"
    return f"{icon} Closed #{t['ticket']} {t['reason']} — P&L ${t['pnl']:+.2f} ({t['hold_min']:.0f} min)"
