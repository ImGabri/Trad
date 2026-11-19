"""High-impact USD event calendar (§6).

Sources, merged and de-duplicated:
  0. assets/events_usd.csv — bundled, verified 2019→2026 calendar (BLS, BEA, Census, Fed, ISM official
     schedules): NFP, CPI, PPI, FOMC statement + press conference, Retail Sales, GDP, PCE, JOLTS, ISM
  1. ForexFactory weekly JSON feed (USD, impact High) → data/calendar.csv, grows every week the bot runs
  2. data/events_manual.csv — your own additions: columns time_utc,title  (ISO time, e.g. 2026-12-04T13:30:00Z)
  3. Historical approximation for backtests: US payrolls (first Friday, 08:30 New York) — FF only serves the
     current week, so older CPI/FOMC/etc. dates must come from the manual CSV if you want them in backtests.
"""
from __future__ import annotations

import json
import logging
import urllib.request
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd

log = logging.getLogger(__name__)
NY = ZoneInfo("America/New_York")


def fetch_forexfactory(cfg) -> pd.DataFrame:
    req = urllib.request.Request(cfg.news.ff_url, headers={"User-Agent": "Mozilla/5.0 xau-scalper"})
    with urllib.request.urlopen(req, timeout=20) as r:
        items = json.loads(r.read().decode("utf-8"))
    cur = {c.upper() for c in cfg.news.currencies}
    rows = []
    for it in items:
        if str(it.get("country", "")).upper() not in cur or str(it.get("impact", "")).lower() != "high":
            continue
        try:
            t = pd.Timestamp(it["date"]).tz_convert("UTC")
        except Exception:  # "All Day"/"Tentative" items have no usable time
            continue
        rows.append({"time_utc": t, "title": it.get("title", ""), "source": "forexfactory"})
    return pd.DataFrame(rows, columns=["time_utc", "title", "source"])


def nfp_dates(start: date, end: date) -> pd.DataFrame:
    rows, d = [], date(start.year, start.month, 1)
    while d <= end:
        first_fri = d + timedelta(days=(4 - d.weekday()) % 7)
        t = datetime(first_fri.year, first_fri.month, first_fri.day, 8, 30, tzinfo=NY)
        rows.append({"time_utc": pd.Timestamp(t).tz_convert("UTC"), "title": "Non-Farm Payrolls (approx.)",
                     "source": "rule"})
        d = date(d.year + (d.month == 12), d.month % 12 + 1, 1)
    return pd.DataFrame(rows)


def _read(path: Path) -> pd.DataFrame:
    if not path.exists():
        return pd.DataFrame(columns=["time_utc", "title", "source"])
    df = pd.read_csv(path)
    df["time_utc"] = pd.to_datetime(df["time_utc"], utc=True)
    if "source" not in df:
        df["source"] = "manual"
    return df[["time_utc", "title", "source"]]


def refresh_calendar(cfg, data_dir: Path) -> pd.DataFrame:
    path = data_dir / "calendar.csv"
    cal = _read(path)
    try:
        ff = fetch_forexfactory(cfg)
        cal = pd.concat([cal, ff])
        log.info("ForexFactory: %d high-impact USD events this week", len(ff))
    except Exception as e:
        log.warning("ForexFactory calendar unavailable: %s", e)
    cal = cal.drop_duplicates(subset=["time_utc", "title"]).sort_values("time_utc")
    data_dir.mkdir(parents=True, exist_ok=True)
    cal.to_csv(path, index=False)
    return cal


BUNDLED = Path(__file__).with_name("assets") / "events_usd.csv"  # verified 2019–2026 official release times


def load_events(cfg, data_dir: Path, start=None, end=None, include_rule_based=True) -> pd.DatetimeIndex:
    cal = pd.concat([_read(BUNDLED), _read(data_dir / "calendar.csv"), _read(data_dir / "events_manual.csv")])
    if len(_read(BUNDLED)):
        include_rule_based = False  # the bundled calendar already has the real payroll dates
    if include_rule_based and start is not None and end is not None:
        cal = pd.concat([cal, nfp_dates(pd.Timestamp(start).date(), pd.Timestamp(end).date())])
    if cal.empty:
        return pd.DatetimeIndex([], tz="UTC")
    t = pd.DatetimeIndex(cal["time_utc"]).sort_values()
    # payroll rule duplicates a real FF entry within a few minutes — keep one
    keep = [True] + list((t[1:] - t[:-1]) > pd.Timedelta(minutes=2))
    return t[keep]


def next_events(events: pd.DatetimeIndex, now: pd.Timestamp, hours: int = 24) -> pd.DatetimeIndex:
    return events[(events >= now) & (events <= now + pd.Timedelta(hours=hours))]
