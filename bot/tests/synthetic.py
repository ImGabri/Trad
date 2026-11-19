"""Synthetic gold-like M1 bars for offline tests (random walk with regimes, weekday sessions only)."""
import numpy as np
import pandas as pd


def synthetic_m1(days=30, start="2025-03-02 22:00", seed=1, price=2900.0):
    rng = np.random.default_rng(seed)
    idx = pd.date_range(start, periods=days * 1440, freq="1min", tz="UTC")
    dow, mins = idx.dayofweek, idx.hour * 60 + idx.minute
    open_ = ~((dow == 5) | ((dow == 4) & (mins >= 22 * 60)) | ((dow == 6) & (mins < 22 * 60)))
    idx = idx[open_]
    n = len(idx)
    vol = 0.35 * np.where((idx.hour >= 7) & (idx.hour < 21), 1.3, 0.6)
    drift = np.repeat(rng.normal(0, 0.03, n // 240 + 1), 240)[:n]  # trending stretches
    ret = drift + rng.standard_normal(n) * vol
    close = price + np.cumsum(ret)
    open_p = np.r_[price, close[:-1]]
    hi = np.maximum(open_p, close) + np.abs(rng.normal(0, 0.15, n))
    lo = np.minimum(open_p, close) - np.abs(rng.normal(0, 0.15, n))
    tv = rng.poisson(60 * vol, n).astype(float) + 1
    spread = np.clip(rng.normal(0.10, 0.02, n), 0.05, 0.4)
    return pd.DataFrame({"open": open_p, "high": hi, "low": lo, "close": close, "tick_volume": tv,
                         "spread": spread}, index=pd.DatetimeIndex(idx, name="time"))
