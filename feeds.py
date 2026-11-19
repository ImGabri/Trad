"""Optional macro and scheduled-news providers. Only release-time data reach the strategy."""
from __future__ import annotations

import json
import os
import re
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

import pandas as pd

from strategy import DAY

BLS = "https://www.bls.gov/schedule/news_release/bls.ics"
FRED = "https://api.stlouisfed.org/fred/series/observations"
SERIES = ("DGS2", "DGS10", "DFII10", "T10YIE", "DTWEXBGS", "VIXCLS")
EVENT_COLS = ["event_id", "known_ms", "event_ms", "name", "active"]
MACRO_COLS = ["series", "observed_ms", "known_ms", "value"]
MAJOR = ("consumer price index", "employment situation", "producer price index", "job openings",
         "employment cost index", "import and export price")


def get_bytes(url):
    req = Request(url, headers={"User-Agent": "PUPrimeDemoResearch/1.0", "Accept": "*/*"})
    with urlopen(req, timeout=15) as resp:
        return resp.read()


def cached(path, columns):
    return pd.read_csv(path) if path.is_file() else pd.DataFrame(columns=columns)


def save_csv(path, frame):
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf8", newline="", dir=path.parent, delete=False) as f:
        frame.to_csv(f, index=False)
        tmp = f.name
    os.replace(tmp, path)


def error_label(exc):
    return f"HTTP {exc.code}" if isinstance(exc, HTTPError) else type(exc).__name__


def parse_bls(content):
    if "BEGIN:VCALENDAR" not in content:
        raise ValueError("No BLS iCalendar data.")
    text = re.sub(r"\r?\n[ \t]", "", content)
    result = {}
    for block in re.findall(r"BEGIN:VEVENT(.*?)END:VEVENT", text, flags=re.S):
        fields = {}
        for line in block.strip().splitlines():
            key, sep, value = line.strip().partition(":")
            if sep:
                fields[key.split(";", 1)[0].upper()] = (key, value)
        name = fields.get("SUMMARY", ("", ""))[1].replace("\\,", ",")
        field, value = fields.get("DTSTART", ("", ""))
        if not any(k in name.lower() for k in MAJOR) or "T" not in value:
            continue
        fmt = "%Y%m%dT%H%M%S" if len(value.rstrip("Z")) == 15 else "%Y%m%dT%H%M"
        instant = datetime.strptime(value.rstrip("Z"), fmt)
        # BLS identifies floating times as Eastern on its release calendar.
        zone = timezone.utc if value.endswith("Z") or "TZID=UTC" in field.upper() else ZoneInfo("America/New_York")
        ms = int(instant.replace(tzinfo=zone).timestamp()*1000)
        uid = fields.get("UID", ("", f"{name}-{value}"))[1]
        active = int(fields.get("STATUS", ("", ""))[1].upper() != "CANCELLED")
        result["bls-" + uid] = (ms, name, active)
    if not result:
        raise ValueError("No timed BLS releases found.")
    return result


def calendar_update(old, new, now, through):
    last = old.sort_values("known_ms").drop_duplicates("event_id", keep="last").set_index("event_id")
    rows = []
    for key, (at, name, active) in new.items():
        previous = last.loc[key] if key in last.index else None
        if previous is None or (previous.event_ms, previous.active, previous["name"]) != (at, active, name):
            rows.append((key, now, at, name, active))
    for key, row in last.iterrows():
        if key not in new and row.active and now <= row.event_ms <= through:
            rows.append((key, now, row.event_ms, row["name"], 0))
    fresh = pd.DataFrame(rows, columns=EVENT_COLS)
    return pd.concat([old, fresh], ignore_index=True) if not old.empty else fresh


def sync_feeds(folder, history_days):
    folder = Path(folder)
    now = pd.Timestamp.now(tz="UTC")
    now_ms = now.value // 1_000_000
    notes = {}
    for provider in ("bls", "te"):
        key = os.environ.get("TE_API_KEY")
        if provider == "te" and not key:
            notes[provider] = "optional key absent"
            continue
        path = folder / f"{provider}_calendar.csv"
        try:
            if provider == "bls":
                records = parse_bls(get_bytes(BLS).decode("utf-8-sig"))
                through = max(t[0] for t in records.values())
            else:
                end = now + pd.Timedelta(days=35)
                url = ("https://api.tradingeconomics.com/calendar/country/united%20states/"
                       f"{now.date()}/{end.date()}?" + urlencode({"c": key, "importance": 3, "f": "json"}))
                data = json.loads(get_bytes(url))
                if not isinstance(data, list):
                    raise ValueError("Calendar unavailable.")
                records = {}
                for item in data:
                    if item.get("CalendarId") and item.get("Date") and str(item.get("DateSpan", "0")) == "0":
                        stamp = pd.to_datetime(item["Date"], utc=True).value // 1_000_000
                        records["te-" + str(item["CalendarId"])] = (stamp, str(item["Event"]), 1)
                through = end.normalize().value // 1_000_000
            received_ms = pd.Timestamp.now(tz="UTC").value // 1_000_000
            save_csv(path, calendar_update(cached(path, EVENT_COLS), records, received_ms, through))
            notes[provider] = f"{len(records)} timed listings"
        except Exception as exc:
            notes[provider] = f"unavailable ({error_label(exc)}), kept prior calendar"
    key = os.environ.get("FRED_API_KEY")
    if not key:
        notes["FRED"] = "optional key absent"
        return notes
    path = folder / "macro.csv"
    frames = []
    old = cached(path, MACRO_COLS)
    if not old.empty:
        frames.append(old)
    for name in SERIES:
        params = {"series_id": name, "api_key": key, "file_type": "json", "output_type": 4,
                  "realtime_start": "1776-07-04", "realtime_end": str(now.date()),
                  "observation_start": str((now-pd.Timedelta(days=history_days+30)).date())}
        try:
            items = json.loads(get_bytes(FRED + "?" + urlencode(params)))["observations"]
            rows = []
            for item in items:
                if item["value"] == ".":
                    continue
                observed = pd.Timestamp(item["date"], tz="UTC").value // 1_000_000
                vintage = pd.Timestamp(item["realtime_start"], tz="UTC").value // 1_000_000
                # A US vintage date provides no exact intraday release time.
                known = max(observed, vintage) + DAY + 6*3_600_000
                rows.append((name, observed, known, float(item["value"])))
            if rows:
                frames.append(pd.DataFrame(rows, columns=MACRO_COLS))
            notes[name] = f"{len(rows)} first-release observations"
        except Exception as exc:
            notes[name] = f"unavailable ({error_label(exc)}), kept prior values"
    if frames:
        combined = pd.concat(frames, ignore_index=True)
        save_csv(path, combined.drop_duplicates(["series", "observed_ms", "known_ms"]).sort_values(["series","known_ms"]))
    return notes


def load_context(folder):
    folder = Path(folder)
    frames = [cached(folder / f"{name}_calendar.csv", EVENT_COLS) for name in ("bls", "te")]
    manual = folder / "events_manual.csv"
    if manual.is_file():
        d = pd.read_csv(manual)
        for src, dest in (("known_utc", "known_ms"), ("event_utc", "event_ms")):
            d[dest] = pd.to_datetime(d[src], utc=True, errors="raise").map(lambda t: t.value // 1_000_000)
        if not d.active.isin([0, 1]).all() or d.event_id.isna().any():
            raise ValueError("Manual calendar needs event_id and active as 0 or 1 on every row.")
        frames.append(d[EVENT_COLS])
    frames = [f for f in frames if not f.empty]
    events = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame(columns=EVENT_COLS)
    return cached(folder / "macro.csv", MACRO_COLS), events
