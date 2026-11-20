"""Trade memory: learns from its own closed trades WHILE trading which situations lose and which win.

A situation ("bucket") is: style · BUY/SELL · session (Asia / London / London-NY overlap / New York) · volatility
(calm / normal / wild versus the last 24 h). After every closed trade the result per 0.01 lot ($) is added to its
bucket. Before a new entry the bot looks the bucket up:

* a bucket that has lost on average (shrunk towards zero, after `min_n` trades) is skipped — except every
  `explore`-th signal, so it can notice when a situation starts working again;
* the memory is saved to data/learning/memory.json and survives restarts and updates.

Backtest (directional 💀 Hyper, 2020–2026): the memory cut trading by ~85 % and the daily loss ~7× (−$85…−162 → −$16…−21
per day at 0.01 lot). On 🎯 Perfect it made results WORSE (trend breakouts often lose a few times right before the big
winner), so it is only switched on for the fast styles (basket / extreme / hyper).
"""
from __future__ import annotations

import json
import math
import time
from pathlib import Path

SESSIONS = ("asia", "london", "overlap", "newyork")


def situation(style: str, side: int, hour_utc: int, atr: float, atr_typ: float) -> str:
    sess = "asia" if hour_utc < 7 else "london" if hour_utc < 13 else "overlap" if hour_utc < 16 else "newyork"
    v = atr / atr_typ if atr_typ and math.isfinite(atr_typ) and atr_typ > 0 and math.isfinite(atr) else 1.0
    vol = "calm" if v < 0.8 else "wild" if v > 1.3 else "normal"
    return f"{style}|{'BUY' if side > 0 else 'SELL'}|{sess}|{vol}"


class TradeMemory:
    def __init__(self, path: Path, min_n: int = 30, k: float = 20.0, explore: int = 10, seed: Path | None = None):
        self.path, self.min_n, self.k, self.explore = Path(path), int(min_n), float(k), int(explore)
        self.b: dict[str, dict] = {}
        if self.path.exists():
            try:
                self.b = json.loads(self.path.read_text()).get("buckets", {})
            except ValueError:
                self.b = {}
        elif seed is not None and Path(seed).exists():  # start with what the backtest already learned
            try:
                self.b = json.loads(Path(seed).read_text()).get("buckets", {})
            except ValueError:
                self.b = {}

    # ------------------------------------------------------------------ learning
    def record(self, key: str, usd_per_001: float) -> None:
        s = self.b.setdefault(key, dict(n=0, sum=0.0, wins=0, seen=0, last=None))
        s["n"] += 1
        s["sum"] = round(s["sum"] + float(usd_per_001), 4)
        s["wins"] += 1 if usd_per_001 > 0 else 0
        s["last"] = time.strftime("%Y-%m-%d %H:%M", time.gmtime())
        self.save()

    def score(self, key: str) -> float:
        s = self.b.get(key)
        return s["sum"] / (s["n"] + self.k) if s else 0.0

    # ------------------------------------------------------------------ deciding
    def check(self, key: str) -> tuple[bool, str]:
        """(allowed, reason). Counts every look-up so blocked situations are re-tried every `explore`-th time."""
        s = self.b.setdefault(key, dict(n=0, sum=0.0, wins=0, seen=0, last=None))
        s["seen"] = s.get("seen", 0) + 1
        if s["n"] >= self.min_n and self.score(key) < 0:
            if s["seen"] % self.explore == 0:
                return True, f"memory: re-testing a losing situation ({s['wins']}/{s['n']} won)"
            return False, (f"memory: skipped — this situation lost before "
                           f"({s['wins']}/{s['n']} won, avg ${s['sum'] / s['n']:+.2f})")
        return True, ""

    # ------------------------------------------------------------------ reporting
    def summary(self, style: str | None = None) -> dict:
        rows = [(k, v) for k, v in self.b.items() if v["n"] and (style is None or k.startswith(style + "|"))]
        learned = [(k, v) for k, v in rows if v["n"] >= self.min_n]
        bad = sorted([(k, v) for k, v in learned if self.score(k) < 0], key=lambda kv: self.score(kv[0]))
        good = sorted([(k, v) for k, v in learned if self.score(k) >= 0], key=lambda kv: -self.score(kv[0]))
        fmt = lambda kv: dict(situation=kv[0].split("|", 1)[1].replace("|", " · "), trades=kv[1]["n"],
                              won=kv[1]["wins"], avg=round(kv[1]["sum"] / kv[1]["n"], 2))
        return dict(trades=sum(v["n"] for _, v in rows), situations=len(rows), avoided=[fmt(x) for x in bad[:5]],
                    working=[fmt(x) for x in good[:5]], blocked=len(bad))

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(dict(updated=time.strftime("%Y-%m-%d %H:%M"), buckets=self.b), indent=1))
        tmp.replace(self.path)
