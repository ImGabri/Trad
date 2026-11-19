"""Free multi-year 1-minute history from public Hugging Face datasets (OANDA candles uploaded by `bunqi`).

Monthly parquet files, e.g. 1/OANDA_XAUUSD_Gold_1_2026-11-01_to_2026-11-30.parquet with columns
time (epoch s, UTC, bar open), open, high, low, close (MID prices), volume (tick count).
Files are cached in data/hf_cache/<dataset>/ and only the newest two months are re-fetched, so the
history keeps growing on your PC and later runs are fast. Third-party upload: provenance/licence are not
documented by the uploader — used here for personal research/backtesting only.
"""
from __future__ import annotations

import json
import logging
import re
import time
import urllib.request
from datetime import date, datetime, timezone
from pathlib import Path

import pandas as pd

log = logging.getLogger("history")
API = "https://huggingface.co/api/datasets/{repo}/tree/main/1"
FILE = "https://huggingface.co/datasets/{repo}/resolve/main/{path}"
MONTH = re.compile(r"_1_(\d{4})-(\d{2})-01_to_")


def _get(url: str, timeout: int = 120, retries: int = 4) -> bytes:
    last = None
    for k in range(retries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "xau-scalper/2"})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read()
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(3 * (k + 1))
    raise RuntimeError(f"download failed: {url}: {last}")


def list_months(repo: str) -> list[tuple[int, int, str]]:
    tree = json.loads(_get(API.format(repo=repo)))
    out = []
    for f in tree:
        m = MONTH.search(f.get("path", ""))
        if f.get("type") == "file" and f["path"].endswith(".parquet") and m:
            out.append((int(m.group(1)), int(m.group(2)), f["path"]))
    return sorted(out)


def download(repo: str, cache_dir: Path, start: date, say=print) -> pd.DataFrame:
    """All 1-minute bars from `start` → latest. Returns UTC-indexed OHLC + volume (mid prices)."""
    cache = Path(cache_dir) / repo.replace("/", "__")
    cache.mkdir(parents=True, exist_ok=True)
    months = [m for m in list_months(repo) if (m[0], m[1]) >= (start.year, start.month)]
    now = datetime.now(timezone.utc)
    recent = {(now.year, now.month), ((now.year if now.month > 1 else now.year - 1), (now.month - 1) or 12)}
    frames, fetched = [], 0
    for y, mth, path in months:
        local = cache / Path(path).name
        if not local.exists() or (y, mth) in recent:
            local.write_bytes(_get(FILE.format(repo=repo, path=path)))
            fetched += 1
            if fetched % 12 == 0:
                say(f"  {repo}: {fetched} monthly files downloaded …")
        frames.append(pd.read_parquet(local))
    if not frames:
        return pd.DataFrame()
    df = pd.concat(frames, ignore_index=True)
    df.index = pd.DatetimeIndex(pd.to_datetime(df.pop("time"), unit="s", utc=True), name="time")
    df = df[~df.index.duplicated(keep="last")].sort_index()
    df = df[df.index < pd.Timestamp(now).floor("min") - pd.Timedelta(minutes=1)]  # drop the unfinished bar
    bad = (df["high"] < df["low"]) | (df["close"] <= 0)
    say(f"  {repo}: {len(df):,} bars {df.index.min():%Y-%m-%d} → {df.index.max():%Y-%m-%d} "
        f"({fetched} files fetched, rest from cache)")
    return df[~bad].astype({"open": float, "high": float, "low": float, "close": float})


def to_broker_bid(mid: pd.DataFrame, offset: float, spread: float | None) -> pd.DataFrame:
    """Convert OANDA mid candles to the broker's bid candles (offset ≈ −half the broker spread)."""
    out = mid[["open", "high", "low", "close"]] + offset
    out["tick_volume"] = mid["volume"].astype(float)
    out["spread"] = spread if spread is not None else float("nan")
    return out
