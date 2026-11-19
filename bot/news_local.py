"""Free headline analyst — no API key, no account.

Every 15 minutes it reads the latest headlines from Google News (gold / Fed / dollar searches, which also carry
Reuters, CNBC, Kitco, Bloomberg …) plus free RSS stations — Investing.com, MarketWatch, BBC, FT, FXEmpire,
Seeking Alpha, Federal Reserve and ECB press releases — de-duplicates the same story across outlets, scores each
headline with a gold-specific phrase lexicon (e.g. "dollar rises", "hawkish", "rate hike" → bearish for gold;
"rate cut", "safe-haven", "tensions", "weaker dollar" → bullish), weights newer headlines more, and returns the same
outlook the dashboard shows: bias, confidence, trade conditions and the strongest headlines as drivers.
Every call is graded 60 minutes later against the price (same track record as before).

Limits: headlines lag prices and the lexicon is simple. There is no free historical headline archive, so this
cannot be backtested — it therefore only gets a cautious veto (analyst.news_veto_conf).
"""
from __future__ import annotations

import json
import logging
import math
import re
import sqlite3
import threading
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path

log = logging.getLogger("news")

GOOGLE = "https://news.google.com/rss/search?q={q}+when:1d&hl=en-US&gl=US&ceid=US:en"
QUERIES = ["gold price", "XAUUSD", "Federal Reserve rates", "US dollar index", "gold Reuters", "gold Kitco"]
# Free RSS "stations" (no key). Google News additionally aggregates Reuters, CNBC, Kitco, Bloomberg, Mining.com …
STATIONS = {
    "Investing.com commodities": "https://www.investing.com/rss/news_11.rss",
    "Investing.com forex": "https://www.investing.com/rss/news_1.rss",
    "Investing.com markets": "https://www.investing.com/rss/news_95.rss",
    "MarketWatch": "https://feeds.content.dowjones.io/public/rss/mw_topstories",
    "BBC Business": "https://feeds.bbci.co.uk/news/business/rss.xml",
    "Financial Times": "https://www.ft.com/markets?format=rss",
    "FXEmpire": "https://www.fxempire.com/api/v1/en/articles/rss/news",
    "Seeking Alpha": "https://seekingalpha.com/market_currents.xml",
    "Federal Reserve": "https://www.federalreserve.gov/feeds/press_all.xml",
    "ECB": "https://www.ecb.europa.eu/rss/press.html",
}
MAX_BYTES = 2_000_000

# (regex, score): positive = bullish for gold, negative = bearish. Phrases are matched case-insensitively.
LEXICON: list[tuple[str, float]] = [
    # gold itself
    (r"\bgold\b[^.]*\b(rall(y|ies|ied)|surg(e|es|ed)|jump(s|ed)?|soar(s|ed)?|climb(s|ed)?|gain(s|ed)?|rebound(s|ed)?|recoup(s|ed)?|regain(s|ed)?|record high|all-time high|higher)\b", 1.0),
    (r"\bgold\b[^.]*\b(fall(s|ing)?|fell|drop(s|ped)?|slump(s|ed)?|tumbl(e|es|ed)|sink(s)?|sank|crash(es|ed)?|plung(e|es|ed)|slide(s)?|slid|lower|declin(e|es|ed)|under pressure|struggl(es|ing)|breakdown|breaks down|\d+-month low|lowest)\b", -1.0),
    # rates / Fed
    (r"\b(rate cut|cuts rates|cut rates|dovish|easing|pause in hikes|rate-cut bets)\b", 0.8),
    (r"\b(rate hike|hikes rates|raise rates|hawkish|tighter|tightening|higher for longer|rate fears)\b", -0.8),
    # dollar / yields
    (r"\b(dollar|greenback|dxy)\b[^.]*\b(fall(s)?|fell|weak(er|ens|ened)?|slid(es)?|drop(s|ped)?|declin(e|es|ed)|retreat(s|ed)?)\b", 0.7),
    (r"\b(dollar|greenback|dxy)\b[^.]*\b(rise(s)?|rose|strong(er)?|strengthen(s|ed)?|gain(s|ed)?|jump(s|ed)?|climb(s|ed)?|firm(s|er)?)\b", -0.7),
    (r"\b(yield(s)?)\b[^.]*\b(fall(s)?|fell|drop(s|ped)?|declin(e|es|ed)|slip(s|ped)?)\b", 0.6),
    (r"\b(yield(s)?)\b[^.]*\b(rise(s)?|rose|jump(s|ed)?|climb(s|ed)?|surg(e|es|ed))\b", -0.6),
    # risk / safe haven
    (r"\b(safe[- ]haven|geopolitic\w*|tension(s)?|war|conflict|missile|sanction(s)?|crisis|recession fears?)\b", 0.5),
    (r"\b(ceasefire|peace deal|risk-on|truce)\b", -0.4),
    (r"\b(central bank(s)? (buy|buying|purchases))\b", 0.5),
    (r"\b(profit[- ]taking|outflows?)\b", -0.4),
]
LEX = [(re.compile(p, re.I), w) for p, w in LEXICON]
NOISE = re.compile(r"\b(price forecast .* (xau/(mxn|brl|inr))|lakh|per 10 ?g)\b", re.I)


FETCHED: dict[str, int] = {}  # headlines delivered per station on the last fetch (shown on the dashboard)


def _read_feed(url: str, timeout: int) -> list[tuple[str, str]]:
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 xau-scalper"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        raw = r.read(MAX_BYTES)
    root = ET.fromstring(raw)
    return [((it.findtext("title") or "").strip(), it.findtext("pubDate") or "") for it in root.iter("item")]


def fetch_headlines(queries: list[str] = QUERIES, timeout: int = 15, stations: dict | None = None) -> list[dict]:
    """All headlines from Google News (several queries) + every free station, de-duplicated, fetched in parallel."""
    from concurrent.futures import ThreadPoolExecutor
    sources = {f"Google News: {q}": GOOGLE.format(q=urllib.parse.quote_plus(q)) for q in queries}
    sources.update(STATIONS if stations is None else stations)

    def one(item):
        name, url = item
        try:
            return name, _read_feed(url, timeout)
        except Exception as e:  # noqa: BLE001
            log.info("feed %s unavailable: %s", name, e)
            return name, []

    seen, out = set(), []
    FETCHED.clear()
    with ThreadPoolExecutor(8) as ex:
        for name, items in ex.map(one, sources.items()):
            FETCHED[name.split(":")[0]] = FETCHED.get(name.split(":")[0], 0) + len(items)
            for title, pub in items:
                key = re.sub(r"\s+-\s+[^-]+$", "", title).lower()[:90]  # same story, different outlet → once
                if not title or key in seen:
                    continue
                seen.add(key)
                try:
                    ts = parsedate_to_datetime(pub).astimezone(timezone.utc)
                except (TypeError, ValueError):
                    continue
                out.append({"title": title, "time": ts, "source": name.split(":")[0]})
    return out


def score_headline(title: str) -> float:
    if NOISE.search(title):
        return 0.0
    s = sum(w for rx, w in LEX if rx.search(title))
    return max(-2.0, min(2.0, s))


def analyse(headlines: list[dict], now: datetime | None = None, half_life_h: float = 4.0) -> dict:
    now = now or datetime.now(timezone.utc)
    scored = []
    for h in headlines:
        age = (now - h["time"]).total_seconds() / 3600
        if age < -0.5 or age > 24:
            continue
        s = score_headline(h["title"])
        if s:
            scored.append((s * math.exp(-max(age, 0) / half_life_h * math.log(2)), s, h))
    total = sum(w for w, _, _ in scored)
    mass = sum(abs(w) for w, _, _ in scored)
    bias = "up" if total > 0.15 else "down" if total < -0.15 else "neutral"
    agree = abs(total) / mass if mass else 0.0                     # 1 = all headlines point the same way
    confidence = 0.5 + 0.35 * agree * (1 - math.exp(-mass / 3))    # more, newer, aligned headlines → more confident
    top = sorted(scored, key=lambda x: -abs(x[0]))[:4]
    drivers = [h["title"] for w, s, h in top if (w > 0) == (total > 0)][:3]
    risks = [h["title"] for w, s, h in top if (w > 0) != (total > 0)][:2]
    n_src = len({h.get("source") for _, _, h in scored})
    summary = (f"{len(scored)} relevant headlines from {n_src} sources in the last 24 h lean {bias}" if bias != "neutral"
               else f"{len(scored)} relevant headlines from {n_src} sources, no clear direction")
    by_source: dict[str, int] = {}
    for _, _, h in scored:
        by_source[h.get("source", "?")] = by_source.get(h.get("source", "?"), 0) + 1
    return dict(bias=bias, confidence=round(confidence, 3), summary=summary, drivers=drivers, risks=risks,
                headlines=len(scored), score=round(total, 2), sources=by_source)


class LocalNewsAgent:
    """Drop-in for the old Claude agent: same latest / due / run_async / track_record interface."""

    def __init__(self, data_dir: Path, interval_min: int = 15, queries: list[str] | None = None):
        self.data_dir, self.interval, self.queries = data_dir, interval_min, queries or QUERIES
        self.db = data_dir / "xau.db"
        self.latest: dict | None = None
        self.busy = False
        self.last_run = 0.0
        con = sqlite3.connect(self.db)
        con.execute("CREATE TABLE IF NOT EXISTS analyst_calls (time TEXT PRIMARY KEY, price REAL, bias TEXT, "
                    "confidence REAL, conditions TEXT, summary TEXT, payload TEXT, price_after REAL, correct INTEGER)")
        con.commit()
        con.close()

    @staticmethod
    def available() -> bool:
        return True

    def due(self) -> bool:
        return not self.busy and time.time() - self.last_run >= self.interval * 60

    def run_async(self, snapshot: dict):
        if self.busy:
            return
        self.busy, self.last_run = True, time.time()
        threading.Thread(target=self._run, args=(snapshot,), daemon=True).start()

    def _run(self, snapshot: dict):
        try:
            self.latest = self.analyse(snapshot) or self.latest
        except Exception as e:  # noqa: BLE001
            log.warning("headline analyst failed: %s", e)
        finally:
            self.busy = False

    def analyse(self, snapshot: dict, headlines: list[dict] | None = None) -> dict | None:
        hl = headlines if headlines is not None else fetch_headlines(self.queries)
        if not hl:
            return None
        out = analyse(hl)
        out["stations"] = {k: v for k, v in FETCHED.items() if v}
        mins_to = snapshot.get("minutes_to_event")
        mins_since = snapshot.get("minutes_since_event")
        if (mins_to is not None and mins_to <= 15) or (mins_since is not None and mins_since <= 10):
            out["trade_conditions"] = "avoid"
        elif out["headlines"] >= 8 and out["bias"] == "neutral":
            out["trade_conditions"] = "caution"
        else:
            out["trade_conditions"] = "normal"
        out["time"] = datetime.now(timezone.utc).isoformat()
        out["price"] = snapshot.get("price")
        con = sqlite3.connect(self.db)
        con.execute("INSERT OR REPLACE INTO analyst_calls (time, price, bias, confidence, conditions, summary, payload) "
                    "VALUES (?,?,?,?,?,?,?)", (out["time"], out["price"], out["bias"], out["confidence"],
                                               out["trade_conditions"], out["summary"], json.dumps(out, default=str)))
        con.commit()
        con.close()
        log.info("headlines: %s %.2f %s — %s", out["bias"], out["confidence"], out["trade_conditions"], out["summary"])
        return out

    def track_record(self, price_now: float | None = None, now: datetime | None = None) -> dict:
        con = sqlite3.connect(self.db)
        now = now or datetime.now(timezone.utc)
        if price_now is not None:
            for t, p0, bias in con.execute("SELECT time, price, bias FROM analyst_calls WHERE correct IS NULL").fetchall():
                if p0 and datetime.fromisoformat(t) <= now - timedelta(minutes=60) and bias != "neutral":
                    ok = int((price_now - p0) * (1 if bias == "up" else -1) > 0)
                    con.execute("UPDATE analyst_calls SET price_after=?, correct=? WHERE time=?", (price_now, ok, t))
            con.commit()
        rows = con.execute("SELECT correct FROM analyst_calls WHERE correct IS NOT NULL ORDER BY time DESC LIMIT 40").fetchall()
        con.close()
        n = len(rows)
        return dict(graded=n, hit_rate=round(sum(r[0] for r in rows) / n, 3) if n else None)
