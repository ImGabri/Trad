"""Free multi-year XAUUSD M1 history from Dukascopy's public datafeed (runs on your PC).

Each trading day is one LZMA-compressed file of 1-minute BID candles:
  https://datafeed.dukascopy.com/datafeed/XAUUSD/YYYY/MM-1/DD/BID_candles_min_1.bi5
Records are big-endian (uint32 seconds-from-midnight-UTC, open, close, low, high as uint32 / 1000, float32 volume).
Spread is left empty so backtests apply your account's spread (config backtest.spread_cost; PUPrime
Standard ≈ $0.32). Prices are Dukascopy's, not PUPrime's — close enough for training; PUPrime's own MT5
bars always take priority where both exist.
"""
from __future__ import annotations

import logging
import lzma
import struct
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta

import numpy as np
import pandas as pd

log = logging.getLogger("dukascopy")
URL = "https://datafeed.dukascopy.com/datafeed/{sym}/{y:04d}/{m:02d}/{d:02d}/BID_candles_min_1.bi5"
POINT = {"XAUUSD": 1000.0}
REC = struct.Struct(">5if")


def _day(sym: str, d: date, retries: int = 4) -> pd.DataFrame | None:
    url = URL.format(sym=sym, y=d.year, m=d.month - 1, d=d.day)
    for k in range(retries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "xau-scalper/2"})
            with urllib.request.urlopen(req, timeout=30) as r:
                raw = r.read()
            break
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return None
            time.sleep(1.5 * (k + 1))
        except Exception:  # noqa: BLE001
            time.sleep(1.5 * (k + 1))
    else:
        log.warning("failed %s", url)
        return None
    if not raw:
        return None
    buf = lzma.decompress(raw)
    n = len(buf) // REC.size
    arr = np.frombuffer(buf[: n * REC.size], dtype=np.dtype([("t", ">i4"), ("o", ">i4"), ("c", ">i4"), ("l", ">i4"),
                                                            ("h", ">i4"), ("v", ">f4")]))
    p = POINT.get(sym, 1000.0)
    idx = pd.Timestamp(d, tz="UTC") + pd.to_timedelta(arr["t"].astype("int64"), unit="s")
    df = pd.DataFrame({"open": arr["o"] / p, "high": arr["h"] / p, "low": arr["l"] / p, "close": arr["c"] / p,
                       "tick_volume": arr["v"].astype(float)}, index=pd.DatetimeIndex(idx, name="time"))
    return df[df["tick_volume"] > 0]  # Dukascopy writes flat zero-volume candles for closed minutes


def download(start: date, end: date, sym: str = "XAUUSD", workers: int = 8, say=print) -> pd.DataFrame:
    days = [start + timedelta(i) for i in range((end - start).days + 1)]
    days = [d for d in days if d.weekday() != 5]  # Saturdays never trade
    out, done = [], 0
    with ThreadPoolExecutor(workers) as ex:
        for df in ex.map(lambda d: _day(sym, d), days):
            done += 1
            if df is not None and len(df):
                out.append(df)
            if done % 100 == 0:
                say(f"  Dukascopy {sym}: {done}/{len(days)} days")
    if not out:
        return pd.DataFrame()
    df = pd.concat(out).sort_index()
    df = df[~df.index.duplicated()]
    df["spread"] = np.nan
    bad = (df["high"] < df["low"]) | (df["close"] <= 0)
    return df[~bad]
