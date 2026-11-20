"""📡 Signal reader: turns public Telegram signal-channel posts into tradeable, GRADED data.

* Reads public channels through Telegram's web preview (https://t.me/s/<channel>) — no login, no account, read-only.
* Parses two post formats:
    - zone posts ("🔴SELL ZONE: 3889.50 - 3901.29 level … towards the 3820 level", "Aggressive BUY zone: a - b"),
    - classic orders ("XAUUSD BUY 2350-2346  SL 2340  TP1 2355  TP2 2362").
* Converts each post into a rule the bot can execute (`zone_trade`): wait until price taps the zone within
  `valid_h` hours, require an M1 confirmation candle in the signal's direction, stop just beyond the zone's far edge
  (or the posted SL), target the posted level / TP (or 2R if none), max hold `hold_h`.
* Grades every parsed signal on real prices with the account's spread, per channel, and only lets the live bot
  FOLLOW a channel once its graded expectancy is positive over enough signals (`min_graded`). Until then its
  signals are paper-traded and shown on the dashboard. Live streams are not used (no reliable text).
"""
from __future__ import annotations

import html
import json
import logging
import math
import re
import time
import urllib.request
from dataclasses import dataclass, field, asdict
from pathlib import Path

import numpy as np
import pandas as pd

log = logging.getLogger("signals")
NUM = r"(?<![\d.,])(\d{3,5}(?:[.,]\d{1,3})?)(?!\d)"   # a price; never the middle of a longer number (590000, 2024…)
_THOUSANDS = re.compile(r"(?<=\d),(?=\d{3}\b)")         # 3,687.738 → 3687.738
_GOLD = re.compile(r"(xau\s*/?\s*usd|\bxau\b|\bgold(?:\s*/?\s*usd)?\b)", re.I)
_OTHER = re.compile(  # other instruments that gold channels also post (a gold price check alone cannot tell them apart)
    r"(xau\s*/?\s*(?:eur|jpy|gbp|aud|chf|cad|cnh|try)|\bxag|\bsilver\b|\bbtc|bitcoin|\beth(?:usd|usdt)?\b|ethereum|"
    r"\bus\s?30\b|\bus\s?100\b|\bus\s?500\b|\bnas(?:daq|100|dq)\b|\bspx|\bsp\s?500\b|s&p|\bger\s?(?:30|40)\b|\bdax\b|"
    r"\bdxy\b|\bdow\b|\bdj30\b|\bnikkei\b|\bjp225\b|\buk100\b|\bcrude\b|\boil\b|\bwti\b|\bbrent\b|\b(?:us|uk)oil\b|"
    r"\b(?:eur|gbp|aud|nzd|usd|cad|chf|jpy)(?:usd|jpy|chf|cad|gbp|eur|nzd|aud)\b)", re.I)
# result / management posts ("TP1 hit", "move SL to entry", "close all") are never new trades
_MANAGE = re.compile(r"(\btp\s*\d?\s*(?:is\s+)?(?:hit|done|reached|achieved|smashed)|\bsl\s+(?:hit|moved|move)\b|"
                     r"(?:move|moved|set|put)\s+(?:your\s+)?(?:sl|stop(?:\s*loss)?)\s+(?:to|at)\b|break\s*-?\s*even|"
                     r"\bclose\s+(?:all|half|partial|your)|\bcancel(?:led|ed)?\b)", re.I)


def instrument(text: str) -> str:
    """'gold', 'other' or 'none': the FIRST instrument a post names ("XAUUSD SELL … silver looks weak too" is gold,
    "BTCUSD BUY … join our gold VIP group" is not)."""
    g, o = _GOLD.search(text or ""), _OTHER.search(text or "")
    if g and (not o or g.start() <= o.start()):
        return "gold"
    return "other" if o else "none"


def sig_key(sig: "Signal") -> str:
    """Stable id of a signal: channel, post and its position in the post. An EDITED post keeps its key (its levels are
    updated in place), so it can never be traded twice."""
    return f"{sig.channel}:{sig.post_id}:{int(getattr(sig, 'n', 0) or 0)}"


@dataclass
class Signal:
    channel: str
    post_id: int
    time: str                 # ISO UTC
    side: int                 # +1 buy, -1 sell
    zone_lo: float
    zone_hi: float
    target: float | None = None
    sl: float | None = None
    tps: list = field(default_factory=list)
    kind: str = "zone"        # zone | order
    aggressive: bool = False
    text: str = ""
    n: int = 0                # position of this signal in its post (a post may hold a buy zone and a sell zone)


def _f(x: str) -> float:
    return float(x.replace(",", "."))


def parse_post(channel: str, post_id: int, t: str, text: str) -> list[Signal]:
    """All gold signals in one post (a post may contain a buy zone and a sell zone). Posts about another instrument
    (silver, XAUEUR, crypto, indices, oil, FX pairs) and result / management posts are ignored."""
    if not text or not re.search(r"(xau|gold|zone|buy|sell)", text, re.I):
        return []
    text = _THOUSANDS.sub("", text)
    if instrument(text) == "other" or _MANAGE.search(text):
        return []
    out: list[Signal] = []
    tgt = re.search(r"towards (?:the )?" + NUM, text, re.I)
    target = _f(tgt.group(1)) if tgt else None
    for m in re.finditer(r"(aggressive\s+)?(buy|sell)\s*zone\s*:?\s*" + NUM + r"\s*[-–]\s*" + NUM, text, re.I):
        a, b = _f(m.group(3)), _f(m.group(4))
        side = 1 if m.group(2).lower() == "buy" else -1
        tg = target if target is not None and (target - (a + b) / 2) * side > 0 else None
        if (a + b) / 2 < 500:
            continue
        out.append(Signal(channel, post_id, t, side, min(a, b), max(a, b), tg, kind="zone",
                          aggressive=bool(m.group(1)), text=text[:400], n=len(out)))
    if out:
        return out
    o = parse_order(channel, post_id, t, text)
    return [o] if o else []


_PX = r"\$?\s*(?<![\d.,])(\d{3,5}(?:\.\d{1,3})?)(?!\d)"
_RESULT = re.compile(r"(\bhit\b|booked|running|profit done|pips? profit|\+\s*\d+\s*pips|closed|in profit|secured)", re.I)


def parse_order(channel: str, post_id: int, t: str, text: str) -> Signal | None:
    """Classic gold order posts in the common formats, e.g.
       'XAUUSD SELL NOW 4030 / TP 4027 / TP 4024 / SL 4032'
       'Gold Buy / Trade $3570/65 Limited / Stope Loss $3557 / Take Profit $3580 / Take Profit $3590'
       'GOLD … BUY! … level 3765.8 / Target - 3801.1 / Stop Loss - 3742.3'
       'ENTER: #XAUUSD long trade / CURRENT PRICE: 3,687.738 / STOP LOSS: 3,663.379 / TAKE PROFIT: 3,724.442'
    Result / update posts ('TP1 HIT', '+80 pips', 'booked') and non-gold posts are ignored. Sanity checks: the stop
    is on the losing side, targets on the winning side, all prices within 10 % of the entry."""
    tx = _THOUSANDS.sub("", text)                                     # 3,687.738 → 3687.738
    if instrument(tx) != "gold" or _MANAGE.search(tx):                # must be a gold post, not a result / update
        return None
    d = re.search(r"\b(buy|sell|long|short)\b", tx, re.I)
    if not d:
        return None
    side = 1 if d.group(1).lower() in ("buy", "long") else -1
    sl_m = re.search(r"\b(?:sl|stop\s*loss|stope\s*loss|stoploss)\b[^\d$]{0,12}" + _PX, tx, re.I)
    tps = [float(x) for x in re.findall(r"\b(?:tp(?:\s?\d(?!\d))?|take\s*profit(?:\s?\d(?!\d))?|target)\b[^\d$]{0,8}" + _PX, tx, re.I)]
    ent = re.search(r"\b(?:now|@|trade|entry|enter(?:\s+at)?|current\s+price|level|price|zone)\b[^\d$]{0,6}" + _PX
                    + r"(?:\s*[-/]\s*(\d{1,5}(?:\.\d{1,3})?)(?!\d))?", tx, re.I)
    if not ent:
        ent = re.search(r"\b(?:buy|sell)\b[^\d$]{0,10}" + _PX + r"(?:\s*[-/]\s*(\d{1,5}(?:\.\d{1,3})?)(?!\d))?", tx, re.I)
    if not ent or (not sl_m and not tps):
        return None
    if _RESULT.search(tx) and not (sl_m and tps):
        return None
    e1 = float(ent.group(1)); e2 = ent.group(2)
    if e2:                                                    # '3570/65' shorthand → 3565
        e2 = float(e2) if len(e2.split(".")[0]) >= 3 else float(str(int(e1))[: -len(e2.split(".")[0])] + e2)
    else:
        e2 = e1
    lo, hi = min(e1, e2), max(e1, e2)
    mid = (lo + hi) / 2
    if mid < 500:
        return None
    sl = float(sl_m.group(1)) if sl_m else None
    if sl is not None and ((mid - sl) * side <= 0 or abs(sl - mid) > 0.1 * mid):
        sl = None
    tps = [x for x in tps if (x - mid) * side > 0 and abs(x - mid) <= 0.1 * mid]
    if sl is None and not tps:
        return None
    return Signal(channel, post_id, t, side, lo, hi, tps[0] if tps else None, sl, tps, kind="order", text=text[:400])


# ---------------------------------------------------------------------------------------------- reading
def dedupe(signals: list[Signal], hours: float = 12.0) -> list[Signal]:
    """Channels re-post the same trade as updates ('Hit TP ✔️ … Trade $3567 Active'). Keep the first post of the same
    channel / side / entry / stop within `hours`."""
    out, last = [], {}
    for sg in sorted(signals, key=lambda x: (x.time, x.post_id)):
        k = (sg.channel, sg.side, round(sg.zone_lo), round(sg.zone_hi), round(sg.sl) if sg.sl else None)
        t = pd.Timestamp(sg.time)
        if k in last and t - last[k] <= pd.Timedelta(hours=hours):
            continue
        last[k] = t
        out.append(sg)
    return out


def fetch_channel(channel: str, pages: int = 1, before: int | None = None, pause_s: float = 1.0,
                  timeout: float = 20.0) -> list[dict]:
    """Newest posts of a PUBLIC channel via its web preview. Returns [{id, time, text}] (oldest first)."""
    posts: dict[int, dict] = {}
    for _ in range(max(1, pages)):
        url = f"https://t.me/s/{channel}" + (f"?before={before}" if before else "")
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (TradV2 signal reader)"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            page = resp.read().decode("utf-8", "replace")
        got = []
        for blk in page.split('class="tgme_widget_message_wrap')[1:]:
            pid = re.search(r'data-post="[^/]+/(\d+)"', blk); tm = re.search(r'<time datetime="([^"]+)"', blk)
            tx = re.search(r'<div class="tgme_widget_message_text js-message_text"[^>]*>(.*?)</div>', blk, re.S)
            if not (pid and tm):
                continue
            txt = re.sub(r"<[^>]+>", "", re.sub(r"<br\s*/?>", "\n", tx.group(1))) if tx else ""
            got.append(dict(id=int(pid.group(1)), time=tm.group(1), text=html.unescape(txt).strip()))
        if not got:
            break
        for g in got:
            posts[g["id"]] = g
        before = min(g["id"] for g in got)
        if pages > 1:
            time.sleep(pause_s)
    return sorted(posts.values(), key=lambda g: g["id"])


# ---------------------------------------------------------------------------------------------- the trade rule
def _t0(sig: Signal) -> pd.Timestamp:
    t = pd.Timestamp(sig.time)
    return t.tz_convert("UTC") if t.tzinfo else t.tz_localize("UTC")


def find_confirmation(sig: Signal, bars: pd.DataFrame, spread: float = 0.32, atr: pd.Series | None = None,
                      valid_h: float = 4.0, buffer_atr: float = 0.5, min_buffer: float = 1.0) -> tuple[int, float] | None:
    """Index (into `bars`) of the M1 bar that CONFIRMS the signal, and the invalidation level beyond the zone.
    Rule: within valid_h after the post, price taps the zone, then an M1 candle closes in the signal's direction;
    if price runs beyond the zone's far edge (+ buffer) first, the signal is dead. Shared by grading and live."""
    t0 = _t0(sig)
    pos = np.flatnonzero((bars.index > t0) & (bars.index <= t0 + pd.Timedelta(hours=valid_h)))
    s, lo, hi = sig.side, sig.zone_lo, sig.zone_hi
    ask_adj = spread if s < 0 else 0.0
    tapped = False
    for i in pos:
        o, h, l, c = bars.open.iat[i], bars.high.iat[i], bars.low.iat[i], bars.close.iat[i]
        a = float(atr.asof(bars.index[i])) if atr is not None and len(atr) else 2.0
        buf = max(min_buffer, buffer_atr * (a if math.isfinite(a) else 2.0))
        far = (hi + buf) if s < 0 else (lo - buf)
        if (s < 0 and h + ask_adj >= far) or (s > 0 and l <= far):
            return None
        if not tapped:
            tapped = (h >= lo) if s < 0 else (l <= hi)
            if not tapped:
                continue
        if (c - o) * s > 0:
            return int(i), float(far)
    return None


def levels_for(sig: Signal, entry: float, far: float) -> tuple[float, float]:
    """Stop and target for an entry: posted SL if valid else just beyond the zone; posted target/TP if ≥ 1R else 2R."""
    s = sig.side
    sl = sig.sl if sig.sl is not None and (entry - sig.sl) * s > 0 else far
    risk = abs(entry - sl)
    tp = sig.target if sig.target is not None and (sig.target - entry) * s >= 1.0 * risk else entry + s * 2 * risk
    return float(sl), float(tp)


def zone_trade(sig: Signal, bars: pd.DataFrame, spread: float = 0.32, atr: pd.Series | None = None,
               valid_h: float = 4.0, hold_h: float = 8.0, buffer_atr: float = 0.5, min_buffer: float = 1.0,
               slip: float = 0.05, **_) -> dict | None:
    """Simulate the trade a signal implies on M1 BID bars. None if never tapped + confirmed in time, else
    dict(entry_time, entry, sl, tp, exit_time, exit, reason, usd) — usd = result per 1 oz (0.01 lot) after costs."""
    c = find_confirmation(sig, bars, spread, atr, valid_h, buffer_atr, min_buffer)
    if c is None or c[0] + 1 >= len(bars):
        return None
    i, far = c; j = i + 1; s = sig.side
    ask_adj = spread if s < 0 else 0.0
    entry = bars.open.iat[j] + (spread + slip if s > 0 else -slip)
    sl, tp = levels_for(sig, entry, far)
    t_in = bars.index[j]
    rest = bars[(bars.index >= t_in) & (bars.index <= t_in + pd.Timedelta(hours=hold_h))]
    for k in range(len(rest)):
        hh, ll = rest.high.iat[k] + ask_adj, rest.low.iat[k] + ask_adj
        if (s > 0 and ll <= sl) or (s < 0 and hh >= sl):
            px, why = sl - s * slip, "sl"
        elif (s > 0 and hh >= tp) or (s < 0 and ll <= tp):
            px, why = tp, "tp"
        else:
            continue
        return dict(entry_time=str(t_in), entry=round(entry, 2), sl=round(sl, 2), tp=round(tp, 2),
                    exit_time=str(rest.index[k]), exit=round(px, 2), reason=why, usd=round((px - entry) * s, 3))
    if len(rest):
        px = rest.close.iat[-1] + ask_adj - s * slip
        return dict(entry_time=str(t_in), entry=round(entry, 2), sl=round(sl, 2), tp=round(tp, 2),
                    exit_time=str(rest.index[-1]), exit=round(px, 2), reason="time", usd=round((px - entry) * s, 3))
    return None


def order_trade(sig: Signal, bars: pd.DataFrame, spread: float = 0.32, atr: pd.Series | None = None,
                hold_h: float = 8.0, slip: float = 0.05, max_away: float = 2.0, **_) -> dict | None:
    """Order posts as the live bot trades them: market entry on the next M1 bar if price is still within max_away $ of
    the posted entry, posted SL (else 2×ATR) and first TP (else 2R), max hold hold_h."""
    t0 = _t0(sig)
    after = np.flatnonzero(bars.index > t0)
    if not len(after):
        return None
    j = int(after[0]); s = sig.side
    if bars.index[j] - t0 > pd.Timedelta(minutes=10):
        return None                                           # no prices right after the post (weekend / gap)
    o = bars.open.iat[j]
    if not (sig.zone_lo - max_away <= o <= sig.zone_hi + max_away):
        return None
    entry = o + (spread + slip if s > 0 else -slip)
    a = float(atr.asof(bars.index[j])) if atr is not None and len(atr) else 3.0
    sl = sig.sl if sig.sl is not None and (entry - sig.sl) * s > 0 else entry - s * 2 * (a if math.isfinite(a) else 3.0)
    _, tp = levels_for(sig, entry, sl)
    ask_adj = spread if s < 0 else 0.0
    t_in = bars.index[j]
    rest = bars[(bars.index >= t_in) & (bars.index <= t_in + pd.Timedelta(hours=hold_h))]
    for k in range(len(rest)):
        hh, ll = rest.high.iat[k] + ask_adj, rest.low.iat[k] + ask_adj
        if (s > 0 and ll <= sl) or (s < 0 and hh >= sl):
            px, why = sl - s * slip, "sl"
        elif (s > 0 and hh >= tp) or (s < 0 and ll <= tp):
            px, why = tp, "tp"
        else:
            continue
        return dict(entry_time=str(t_in), entry=round(entry, 2), sl=round(sl, 2), tp=round(tp, 2),
                    exit_time=str(rest.index[k]), exit=round(px, 2), reason=why, usd=round((px - entry) * s, 3))
    if len(rest):
        px = rest.close.iat[-1] + ask_adj - s * slip
        return dict(entry_time=str(t_in), entry=round(entry, 2), sl=round(sl, 2), tp=round(tp, 2),
                    exit_time=str(rest.index[-1]), exit=round(px, 2), reason="time", usd=round((px - entry) * s, 3))
    return None


def grade(signals: list[Signal], bars: pd.DataFrame, spread: float = 0.32, atr: pd.Series | None = None, **kw) -> pd.DataFrame:
    rows = []
    for sg in signals:
        r = (order_trade if sg.kind == "order" else zone_trade)(sg, bars, spread, atr, **kw)
        rows.append(dict(channel=sg.channel, post_id=sg.post_id, time=sg.time, side=sg.side, zone_lo=sg.zone_lo,
                         zone_hi=sg.zone_hi, target=sg.target, kind=sg.kind, aggressive=sg.aggressive,
                         n=int(getattr(sg, "n", 0) or 0), traded=r is not None, **(r or {})))
    return pd.DataFrame(rows)


def channel_stats(graded: pd.DataFrame, min_graded: int = 20) -> dict:
    """Per channel: parsed / traded / win % / avg $ per 0.01 lot / trust (follow only if avg > 0 over ≥ min_graded)."""
    out = {}
    if graded is None or graded.empty:
        return out
    for ch, g in graded.groupby("channel"):
        t = g[g.traded]
        avg = float(t.usd.mean()) if len(t) else 0.0
        out[ch] = dict(parsed=int(len(g)), traded=int(len(t)), win=round(100 * float((t.usd > 0).mean()), 1) if len(t) else None,
                       avg_usd=round(avg, 3), total_usd=round(float(t.usd.sum()), 2) if len(t) else 0.0,
                       follow=bool(len(t) >= min_graded and avg > 0))
    return out


# ---------------------------------------------------------------------------------------------- learning
class SignalLearner:
    """Learns from graded signals which ones to take. Expected $ per 0.01 lot after costs is estimated hierarchically
    (all → channel → channel·side → channel·side·H4-agreement) with shrinkage, so a handful of lucky trades cannot
    switch a losing channel on. Retrained on every grade (seed history from the backtest + live grades).
      decide() → (take, size_mult, expected_usd, n, why)"""

    def __init__(self, k: float = 10.0, min_n: int = 100, boost_usd: float = 0.5, skip_below: float = -1.0):
        # Defaults from a walk-forward test on 352 graded trades of 4 channels (Jul 2025 – Nov 2026): strict filters
        # (skip at < $0 after 20) cut the result from +$656 to +$38; patient ones (≥ 100 graded, skip only if clearly
        # losing < −$1) kept +$505. So: follow everything, only drop channels that are clearly bad.
        self.k, self.min_n, self.boost, self.skip = float(k), int(min_n), float(boost_usd), float(skip_below)
        self.tab: dict = {}
        self.n_total = 0

    def fit(self, graded: pd.DataFrame) -> "SignalLearner":
        self.tab = {}
        ok = graded is not None and len(graded) and {"traded", "usd", "channel", "side"} <= set(graded.columns)
        t = graded[graded["traded"].astype(bool)].dropna(subset=["usd"]) if ok else pd.DataFrame()
        self.n_total = len(t)
        if t.empty:
            return self
        h4 = t["h4_ok"].map({True: 1, False: 0, "True": 1, "False": 0}).fillna(-1).astype(int) if "h4_ok" in t else pd.Series(-1, index=t.index)
        g_mean = float(t["usd"].mean())
        self.tab[()] = (g_mean, len(t))
        for keys in (["channel"], ["channel", "side"], ["channel", "side", "h4"]):
            tt = t.assign(h4=h4)
            for k, grp in tt.groupby(keys):
                k = k if isinstance(k, tuple) else (k,)
                self.tab[tuple(keys) + k] = (float(grp["usd"].sum()), len(grp))
        return self

    def _level(self, keys: tuple, vals: tuple, prior: float) -> tuple[float, int]:
        s, n = self.tab.get(keys + vals, (0.0, 0))
        return (s + self.k * prior) / (n + self.k), n

    def expect(self, channel: str, side: int, h4_ok: bool | None = None) -> tuple[float, int]:
        g = self.tab.get((), (0.0, 0))[0]
        e1, n1 = self._level(("channel",), (channel,), g)
        e2, _ = self._level(("channel", "side"), (channel, int(side)), e1)
        if h4_ok is None:
            return e2, n1
        e3, _ = self._level(("channel", "side", "h4"), (channel, int(side), 1 if h4_ok else 0), e2)
        return e3, n1

    def decide(self, channel: str, side: int, h4_ok: bool | None = None) -> tuple[bool, float, float, int, str]:
        e, n = self.expect(channel, side, h4_ok)
        if n < self.min_n:
            return True, 1.0, e, n, f"learning ({n}/{self.min_n} graded)"
        if e < self.skip:
            return False, 0.0, e, n, f"skipped: clearly losing, expected ${e:+.2f} per 0.01 lot after costs ({n} graded)"
        return True, (1.5 if e >= self.boost else 1.0), e, n, f"proven: expected ${e:+.2f} ({n} graded)"


# ---------------------------------------------------------------------------------------------- 🔮 signal-rating AI
LANES = {"max": dict(max_away=5.0, hold_h=8.0), "hyper": dict(max_away=1e9, hold_h=24.0)}
RATER_COLS = ["dist", "sl_atr", "rr", "h4_ok", "h1_ok", "mom15", "mom60", "vol", "hour", "dow", "order", "aggr", "side", "ch_recent"]


def signal_features(sig: Signal, t: pd.Timestamp, close: pd.Series, atr: pd.Series, h1: pd.Series, h4: pd.Series,
                    atr_med: pd.Series | None = None, posted_target: float | None = None) -> dict:
    """What the rating AI sees about a signal at time t (closed bars only): distance of price from the posted entry
    (in ATR, + = already moved the signal's way), stop size, posted reward:risk, 1 h / 4 h trend agreement, 15 / 60 min
    momentum, volatility vs the last 24 h, time of day / week, order vs zone post, side."""
    c = float(close.asof(t)); a = float(atr.asof(t)) if len(atr) else 2.0
    a = a if math.isfinite(a) and a > 0 else 2.0
    c15, c60 = float(close.asof(t - pd.Timedelta(minutes=15))), float(close.asof(t - pd.Timedelta(minutes=60)))
    mid = (sig.zone_lo + sig.zone_hi) / 2
    tgt = posted_target if posted_target is not None else (sig.tps[0] if sig.tps else None)
    med = float(atr_med.asof(t)) if atr_med is not None and len(atr_med) else a
    return dict(dist=(c - mid) * sig.side / a, sl_atr=(abs(mid - sig.sl) / a) if sig.sl else np.nan,
                rr=(abs(tgt - mid) / abs(mid - sig.sl)) if (sig.sl and tgt) else np.nan,
                h4_ok=float(np.sign(h4.asof(t) or 0) == sig.side), h1_ok=float(np.sign(h1.asof(t) or 0) == sig.side),
                mom15=(c - c15) * sig.side / a, mom60=(c - c60) * sig.side / a, vol=a / (med if med and math.isfinite(med) else a),
                hour=t.hour, dow=t.dayofweek, order=float(sig.kind == "order"), aggr=float(sig.aggressive), side=sig.side)


class SignalRater:
    """Gradient-boosted model (one per lane) predicting the $ result per 0.01 lot of trading a signal, after costs.
    Trained on the graded history shipped with the app (assets/signal_rater_train.csv, 7 channels, Jul 2025 – Nov 2026)
    plus every live grade; refit whenever new grades arrive. Walk-forward test (€500, Dec 2025 – Nov 2026): taking only
    signals it rates > $0 lifted Signal Max from +$1,080 to +$1,681 and Signal Hyper from +$1,468 to +$1,627 (per 0.01 lot)."""

    NEED = {"ch", "lane", "usd", *RATER_COLS}

    def __init__(self, seed: Path | None = None, live: Path | None = None):
        """seed: the shipped graded history; live: data/signals/rated_live.csv (every signal graded on this PC so far,
        so the AI keeps what it learned across restarts and updates)."""
        parts = [self._read(seed), self._read(live)]
        parts = [x for x in parts if len(x)]
        self.rows = pd.concat(parts, ignore_index=True) if parts else pd.DataFrame()
        self.models: dict = {}
        self.channels: list[str] = []
        self.fit()

    @classmethod
    def _read(cls, path: Path | None) -> pd.DataFrame:
        if path is None or not Path(path).exists():
            return pd.DataFrame()
        try:
            d = pd.read_csv(path)
        except (ValueError, OSError) as e:   # empty / half-written file: train without it rather than not at all
            log.warning("signal rater: cannot read %s (%s)", path, e)
            return pd.DataFrame()
        if not cls.NEED <= set(d.columns):
            log.warning("signal rater: %s lacks columns %s", path, sorted(cls.NEED - set(d.columns)))
            return pd.DataFrame()
        return d.dropna(subset=["usd"])

    def add(self, df: pd.DataFrame) -> pd.DataFrame:
        """Learn from newly graded signals (refits). Returns the added rows with their channel record filled in."""
        if df is None or not len(df):
            return pd.DataFrame()
        df = df.copy()
        df["_new"] = True
        allr = pd.concat([self.rows.assign(_new=False), df], ignore_index=True) if len(self.rows) else df.reset_index(drop=True)
        allr["_t"] = pd.to_datetime(allr["time"], utc=True, errors="coerce", format="mixed")
        allr = allr.sort_values("_t", kind="stable")
        # the channel's own record in this lane BEFORE each new signal: mean of its last 30 results (as in training)
        rec = allr.groupby(["lane", "ch"]).usd.transform(lambda x: x.shift(1).rolling(30, min_periods=5).mean())
        fill = allr["_new"] & allr["ch_recent"].isna() if "ch_recent" in allr else allr["_new"]
        allr.loc[fill, "ch_recent"] = rec[fill]
        added = allr[allr["_new"]].drop(columns=["_new", "_t"])
        self.rows = allr.drop(columns=["_new", "_t"]).reset_index(drop=True)
        self.fit()
        return added

    def ch_recent(self, ch: str, lane: str | None = None) -> float:
        """Mean $ result of the channel's last 30 graded signals in this lane (≥ 5 needed), oldest → newest by time."""
        if self.rows.empty:
            return np.nan
        d = self.rows[self.rows.ch == ch]
        if lane is not None and "lane" in d:
            d = d[d.lane == lane]
        if len(d) < 5:
            return np.nan
        if "time" in d:
            d = d.assign(_t=pd.to_datetime(d["time"], utc=True, errors="coerce", format="mixed")).sort_values("_t", kind="stable")
        x = d.usd.tail(30)
        return float(x.mean())

    def fit(self) -> None:
        try:
            self._fit()
        except Exception:  # noqa: BLE001 — no rating AI then: the signal falls back to the channel learner
            log.exception("signal rater: training failed")
            self.models = {}

    def _fit(self) -> None:
        self.models = {}
        if self.rows.empty:
            return
        import lightgbm as lgb
        self.channels = sorted(set(self.rows.ch.astype(str)))
        for lane, d in self.rows.groupby("lane"):
            if len(d) < 150:
                continue
            X = d[RATER_COLS].astype(float).copy()
            X["channel"] = pd.Categorical(d.ch.astype(str), categories=self.channels)
            m = lgb.LGBMRegressor(n_estimators=150, learning_rate=0.03, num_leaves=8, min_child_samples=25, subsample=0.8,
                                  subsample_freq=1, colsample_bytree=0.8, verbose=-1, random_state=7)
            m.fit(X, d.usd.clip(-30, 30))
            self.models[lane] = m

    def predict(self, lane: str, feats: dict, channel: str) -> float | None:
        m = self.models.get(lane)
        if m is None:
            return None
        X = pd.DataFrame([{k: float(feats.get(k, np.nan)) if feats.get(k) is not None else np.nan for k in RATER_COLS}])
        X["ch_recent"] = self.ch_recent(channel, lane)
        X["channel"] = pd.Categorical([channel], categories=self.channels if channel in self.channels else self.channels + [channel])
        if channel not in self.channels:
            X["channel"] = pd.Categorical([np.nan], categories=self.channels)
        return float(m.predict(X)[0])


# ---------------------------------------------------------------------------------------------- live store
class SignalBook:
    """Live: polls channels, keeps parsed signals + grades in data/signals/, answers 'which zones are active now'."""

    def __init__(self, folder: Path, channels: list[str], poll_s: float = 60, min_graded: int = 20,
                 grade_kw: dict | None = None, tp_mode: str = "first"):
        self.folder, self.channels, self.poll_s, self.min_graded = Path(folder), list(channels), float(poll_s), int(min_graded)
        self.grade_kw, self.tp_mode = dict(grade_kw or {}), str(tp_mode).lower()   # grade exactly as the bot trades
        self.folder.mkdir(parents=True, exist_ok=True)
        self.path = self.folder / "signals.json"
        self.signals: list[Signal] = []
        self.graded = pd.DataFrame()
        self.last_poll = 0.0
        self.errors: dict[str, str] = {}
        self._fails: dict[str, int] = {}
        self._next_try: dict[str, float] = {}
        if self.path.exists():
            try:
                fields = set(Signal.__dataclass_fields__)
                self.signals = [Signal(**{k: v for k, v in d.items() if k in fields})
                                for d in json.loads(self.path.read_text(encoding="utf-8"))]
            except (ValueError, TypeError, AttributeError):
                self.signals = []
        gp = self.folder / "graded.csv"
        seed = Path(__file__).with_name("assets") / "signals_seed.csv"   # backtest-graded history shipped with the app
        for f in (gp, seed):                     # this PC's grades, else (missing / empty / damaged) the shipped history
            if not f.exists():
                continue
            try:
                g = pd.read_csv(f)
            except (ValueError, OSError):
                continue
            if len(g) and {"channel", "post_id", "side", "traded"} <= set(g.columns):
                self.graded = g
                break
        self.learner = SignalLearner(min_n=self.min_graded).fit(self.graded)  # min_graded: config signals.min_graded
        self.rater: SignalRater | None = None
        self.lanes: list[str] = []

    def due(self) -> bool:
        return time.time() - self.last_poll >= self.poll_s

    KEEP_DAYS = 14          # signals older than this can no longer be traded or graded (the live M1 window is ~9 days)

    def poll(self) -> int:
        """Fetch every channel and merge the posts (the live bot calls fetch_all() and merge_all() separately so the
        network wait never holds the lock the trading loop needs)."""
        return self.merge_all(self.fetch_all())

    def fetch_all(self, timeout: float = 8.0) -> dict:
        """{channel: [posts] | Exception} for every channel that is due, fetched in parallel. A failing channel is
        retried with a growing pause (1, 2, 4 … 30 min) so a dead channel never slows the others down."""
        self.last_poll = time.time()
        now = time.monotonic()
        due = [ch for ch in self.channels if self._next_try.get(ch, 0.0) <= now]
        if not due:
            return {}
        from concurrent.futures import ThreadPoolExecutor

        def one(ch):
            try:
                return ch, fetch_channel(ch, pages=1, timeout=timeout)
            except Exception as e:  # noqa: BLE001 — network / layout problems must never stop trading
                return ch, e
        with ThreadPoolExecutor(max_workers=min(6, len(due)), thread_name_prefix="tg-fetch") as pool:
            return dict(pool.map(one, due))

    def merge_all(self, got: dict) -> int:
        """Parse fetched posts into signals. New posts are appended; an EDITED post (same channel / post / position)
        updates its signal in place. Returns how many genuinely new signals were added."""
        now = time.monotonic()
        idx = {sig_key(x): i for i, x in enumerate(self.signals)}
        fresh, changed = set(), False
        for ch, posts in got.items():
            if isinstance(posts, Exception):
                self._fails[ch] = self._fails.get(ch, 0) + 1
                self._next_try[ch] = now + min(1800.0, 60.0 * 2 ** (self._fails[ch] - 1))
                self.errors[ch] = str(posts)[:120] or type(posts).__name__
                continue
            self._fails.pop(ch, None); self._next_try.pop(ch, None); self.errors.pop(ch, None)
            for p in posts or []:
                try:
                    parsed = parse_post(ch, int(p["id"]), str(p["time"]), str(p.get("text") or ""))
                except Exception:  # noqa: BLE001 — one odd post must not hide the rest
                    log.exception("signals: cannot parse post %s/%s", ch, p.get("id"))
                    continue
                for sg in parsed:
                    k = sig_key(sg)
                    if k in idx:
                        old = self.signals[idx[k]]
                        if (old.side, old.zone_lo, old.zone_hi, old.sl, old.target, old.tps) != \
                                (sg.side, sg.zone_lo, sg.zone_hi, sg.sl, sg.target, sg.tps):
                            self.signals[idx[k]] = sg      # edited by the channel: new levels, same key
                            changed = True
                    else:
                        idx[k] = len(self.signals)
                        self.signals.append(sg)
                        fresh.add(k)
        if fresh or changed:
            cut = pd.Timestamp.now(tz="UTC") - pd.Timedelta(days=self.KEEP_DAYS)
            kept = [x for x in dedupe(self.signals) if _t0(x) >= cut]   # repeated update posts → one signal
            self.signals = kept[-5000:]
            self._save()
        return sum(1 for x in self.signals if sig_key(x) in fresh)

    def _save(self) -> None:
        from monitor import atomic_write_text
        try:
            atomic_write_text(self.path, json.dumps([asdict(x) for x in self.signals], ensure_ascii=False))
        except OSError as e:
            log.warning("signals: cannot save %s (%s)", self.path, e)

    def regrade(self, bars: pd.DataFrame, spread: float, atr: pd.Series | None = None, h4: pd.Series | None = None) -> None:
        """Grade every signal whose outcome is decidable with the bars we have (live M1 history)."""
        if bars is None or bars.empty:
            return
        g0 = self.graded
        if not g0.empty and {"channel", "post_id"} <= set(g0.columns):
            nn = g0["n"].fillna(0).astype(int) if "n" in g0 else pd.Series(0, index=g0.index)
            done = {f"{c}:{int(p)}:{int(n)}" for c, p, n in zip(g0["channel"], g0["post_id"], nn)}
        else:
            done = set()
        # grade only when the WHOLE trade is over in every lane: entry up to valid_h after the post + the longest hold
        hold = max([float(self.grade_kw.get("hold_h", 8.0))] + [float(LANES.get(l, {}).get("hold_h", 8.0)) for l in self.lanes])
        horizon = bars.index[-1] - pd.Timedelta(hours=float(self.grade_kw.get("valid_h", 4.0)) + hold + 0.25)
        todo = [s for s in self.signals if sig_key(s) not in done and bars.index[0] <= _t0(s) < horizon]
        if todo:
            if self.tp_mode == "2r":
                import copy
                todo = [copy.copy(x) for x in todo]
                for x in todo:
                    x.target = None
            g = grade(todo, bars, spread, atr, **self.grade_kw)
            if h4 is not None:
                g["h4_ok"] = [int(np.sign(h4.asof(pd.Timestamp(t)) or 0)) == sd for t, sd in zip(g.time, g.side)]
            self.graded = pd.concat([self.graded, g], ignore_index=True) if not self.graded.empty else g
            from monitor import atomic_write_text
            try:
                atomic_write_text(self.folder / "graded.csv", self.graded.to_csv(index=False))
            except OSError as e:
                log.warning("signals: cannot save graded.csv (%s)", e)
            self.learner.fit(self.graded)                               # retrain on every new grade
            if self.rater is not None and self.lanes and h4 is not None:
                self._rate_grades(todo, bars, atr, h4)

    def _rate_grades(self, todo: list[Signal], bars: pd.DataFrame, atr: pd.Series, h4: pd.Series) -> None:
        """Grade the new signals once per lane (exactly as each lane trades them) and teach the rating AI."""
        from strategy import closed_trend
        h1 = pd.Series(closed_trend(bars, 60, 20, 50), index=bars.index)
        med = atr.rolling(1440, min_periods=60).median()
        rows = []
        for lane in self.lanes:
            g = grade(todo, bars, 0.32, atr, **LANES.get(lane, {}))
            for sg, (_, r) in zip(todo, g.iterrows()):
                if not r.traded:
                    continue
                f = signal_features(sg, pd.Timestamp(sg.time), bars.close, atr, h1, h4, med)
                rows.append(dict(time=sg.time, ch=sg.channel, lane=lane, usd=r.usd, **f, ch_recent=np.nan))
        if rows:
            added = self.rater.add(pd.DataFrame(rows))
            out = self.folder / "rated_live.csv"
            try:            # reloaded at the next start (SignalRater(seed, live)): the AI keeps what it learned here
                cols = ["time", "ch", "lane", "usd"] + RATER_COLS
                added.reindex(columns=cols).to_csv(out, mode="a", index=False, header=not out.exists())
            except OSError as e:
                log.warning("signals: cannot append %s (%s)", out, e)

    def stats(self) -> dict:
        return channel_stats(self.graded, self.min_graded)

    def active(self, now: pd.Timestamp, valid_h: float = 4.0) -> list[Signal]:
        lo = now - pd.Timedelta(hours=valid_h)
        return [s for s in self.signals if lo < _t0(s) <= now]
