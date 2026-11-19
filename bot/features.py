"""Indicators, multi-timeframe alignment and the per-M1-bar feature matrix.

Look-ahead rule: every row is stamped with the M1 bar's OPEN time t but is only known at t+1min
(bar close). Higher-timeframe values are joined on *close time*, so an M5/M15/H1 bar is visible only
once it has fully closed.
"""
from __future__ import annotations

import numpy as np
import pandas as pd


# ------------------------------------------------------------------------------ indicators
def ema(s: pd.Series, n: int) -> pd.Series:
    return s.ewm(span=n, adjust=False, min_periods=n).mean()


def wilder(s: pd.Series, n: int) -> pd.Series:
    return s.ewm(alpha=1 / n, adjust=False, min_periods=n).mean()


def rsi(close: pd.Series, n: int = 14) -> pd.Series:
    d = close.diff()
    up, dn = wilder(d.clip(lower=0), n), wilder(-d.clip(upper=0), n)
    return 100 - 100 / (1 + up / dn.replace(0, np.nan))


def true_range(df: pd.DataFrame) -> pd.Series:
    pc = df["close"].shift()
    return pd.concat([df["high"] - df["low"], (df["high"] - pc).abs(), (df["low"] - pc).abs()], axis=1).max(axis=1)


def atr(df: pd.DataFrame, n: int = 14) -> pd.Series:
    return wilder(true_range(df), n)


def adx(df: pd.DataFrame, n: int = 14) -> pd.Series:
    up, dn = df["high"].diff(), -df["low"].diff()
    plus = pd.Series(np.where((up > dn) & (up > 0), up, 0.0), index=df.index)
    minus = pd.Series(np.where((dn > up) & (dn > 0), dn, 0.0), index=df.index)
    tr = wilder(true_range(df), n)
    pdi, mdi = 100 * wilder(plus, n) / tr, 100 * wilder(minus, n) / tr
    dx = 100 * (pdi - mdi).abs() / (pdi + mdi).replace(0, np.nan)
    return wilder(dx, n)


def macd(close: pd.Series, f=12, s=26, sig=9):
    line = ema(close, f) - ema(close, s)
    signal = ema(line, sig)
    return line, signal, line - signal


def bb_pctb(close: pd.Series, n=20, k=2.0) -> pd.Series:
    m, sd = close.rolling(n).mean(), close.rolling(n).std()
    return (close - (m - k * sd)) / (2 * k * sd).replace(0, np.nan)


def stoch(df: pd.DataFrame, n=14, d=3):
    ll, hh = df["low"].rolling(n).min(), df["high"].rolling(n).max()
    k = 100 * (df["close"] - ll) / (hh - ll).replace(0, np.nan)
    return k, k.rolling(d).mean()


def obv(df: pd.DataFrame) -> pd.Series:
    return (np.sign(df["close"].diff()).fillna(0) * df["tick_volume"]).cumsum()


def slope(s: pd.Series, n: int) -> pd.Series:
    """OLS slope over the last n points (vectorised with a convolution; NaN until n points exist)."""
    x = np.arange(n) - (n - 1) / 2
    w = x / (x ** 2).sum()
    v = s.to_numpy(float)
    out = np.full(len(v), np.nan)
    if len(v) >= n:
        out[n - 1:] = np.convolve(v, w[::-1], mode="valid")
    return pd.Series(out, index=s.index)


def session_vwap(df: pd.DataFrame, reset_hour_utc: int = 22) -> pd.Series:
    """VWAP anchored at the daily gold session open (22:00 UTC), weighted by tick volume."""
    tp = (df["high"] + df["low"] + df["close"]) / 3
    day = (df.index - pd.Timedelta(hours=reset_hour_utc)).floor("D")
    v = df["tick_volume"].clip(lower=1)
    return (tp * v).groupby(day).cumsum() / v.groupby(day).cumsum()


# ---------------------------------------------------------------------------- resampling
def resample(m1: pd.DataFrame, minutes: int) -> pd.DataFrame:
    g = m1.resample(f"{minutes}min", label="left", closed="left")
    out = pd.DataFrame({"open": g["open"].first(), "high": g["high"].max(), "low": g["low"].min(),
                        "close": g["close"].last(), "tick_volume": g["tick_volume"].sum(),
                        "spread": g["spread"].mean()})
    return out.dropna(subset=["close"])


def align_closed(m1_index: pd.DatetimeIndex, htf: pd.DataFrame, minutes: int, prefix: str) -> pd.DataFrame:
    """Join HTF columns onto M1 rows, using only HTF bars closed by the M1 bar's close."""
    h = htf.copy()
    h.index = h.index + pd.Timedelta(minutes=minutes)  # close time
    left = pd.DataFrame(index=m1_index + pd.Timedelta(minutes=1))  # M1 close time
    out = pd.merge_asof(left, h.add_prefix(prefix), left_index=True, right_index=True, direction="backward")
    out.index = m1_index
    return out


# ---------------------------------------------------------------------------- sessions
def session_flags(idx: pd.DatetimeIndex) -> pd.DataFrame:
    h = idx.hour + idx.minute / 60
    return pd.DataFrame({
        "hour": idx.hour, "dow": idx.dayofweek,
        "sess_asia": ((h >= 0) & (h < 7)).astype(int),
        "sess_london": ((h >= 7) & (h < 16)).astype(int),
        "sess_ny": ((h >= 13) & (h < 21)).astype(int),
    }, index=idx)


def minutes_to_next_event(idx: pd.DatetimeIndex, events: pd.DatetimeIndex | None, cap: float = 1440) -> pd.Series:
    """Signed-agnostic distance (min) to the next high-impact event; negative = minutes since the last one
    when it is closer than the next one within 15 min."""
    if events is None or len(events) == 0:
        return pd.Series(cap, index=idx)
    ev = np.sort(events.values.astype("datetime64[ns]"))
    t = idx.values.astype("datetime64[ns]")
    pos = np.searchsorted(ev, t, side="left")
    nxt = np.where(pos < len(ev), (ev[np.minimum(pos, len(ev) - 1)] - t) / np.timedelta64(1, "m"), cap)
    prv = np.where(pos > 0, (t - ev[np.maximum(pos - 1, 0)]) / np.timedelta64(1, "m"), cap)
    val = np.where(prv <= 15, -prv, nxt)
    return pd.Series(np.clip(val, -cap, cap), index=idx)


# ------------------------------------------------------------------------------- build
def build_features(m1: pd.DataFrame, cfg=None, correlated: dict[str, pd.Series] | None = None,
                   micro: pd.DataFrame | None = None, events: pd.DatetimeIndex | None = None) -> pd.DataFrame:
    """Return the M1 feature/state frame used by both strategy rules and the ML model."""
    n_atr = cfg.strategy.atr_period if cfg else 14
    m1 = m1.copy()
    if m1["spread"].isna().all():
        m1["spread"] = cfg.backtest.spread_cost if cfg else 0.10
    m1["spread"] = m1["spread"].ffill().bfill()
    c = m1["close"]
    f = pd.DataFrame(index=m1.index)
    for col in ("open", "high", "low", "close", "tick_volume", "spread"):
        f[col] = m1[col]

    # --- M1 technicals
    f["rsi14"] = rsi(c, 14)
    f["macd"], f["macd_sig"], f["macd_hist"] = macd(c)
    f["atr1"] = atr(m1, n_atr)
    f["bb_pctb"] = bb_pctb(c)
    f["vwap"] = session_vwap(m1)
    f["vwap_dev_atr"] = (c - f["vwap"]) / f["atr1"]
    f["obv_slope"] = slope(obv(m1), 10) / m1["tick_volume"].rolling(50).mean()
    f["adx1"] = adx(m1, 14)
    f["stoch_k"], f["stoch_d"] = stoch(m1)
    f["ret1"] = c.pct_change()
    f["ret5"] = c.pct_change(5)
    f["spread_avg20"] = m1["spread"].rolling(cfg.risk.spread_avg_bars if cfg else 20, min_periods=1).mean()
    f["spread_ratio"] = m1["spread"] / f["spread_avg20"]
    f["vol_ratio"] = m1["tick_volume"] / m1["tick_volume"].rolling(20).mean()

    # --- higher timeframes (closed bars only)
    m5, m15, h1 = resample(m1, 5), resample(m1, 15), resample(m1, 60)
    m5x = pd.DataFrame({"atr": atr(m5, n_atr), "close": m5["close"],
                        "swing_hi": m5["high"].rolling(cfg.strategy.fib_swing_bars_m5 if cfg else 48).max(),
                        "swing_lo": m5["low"].rolling(cfg.strategy.fib_swing_bars_m5 if cfg else 48).min()})
    fast, slow = (cfg.strategy.m15_ema_fast, cfg.strategy.m15_ema_slow) if cfg else (50, 200)
    lb = cfg.strategy.m15_slope_lookback if cfg else 3
    e_fast, e_slow = ema(m15["close"], fast), ema(m15["close"], slow)
    m15x = pd.DataFrame({"ema_fast": e_fast, "ema_slow": e_slow,
                         "ema_fast_slope": e_fast - e_fast.shift(lb), "ema_slow_slope": e_slow - e_slow.shift(lb),
                         "adx": adx(m15, 14)})
    h1_atr = atr(h1, n_atr)
    h1x = pd.DataFrame({"high": h1["high"], "low": h1["low"], "adx": adx(h1, 14), "atr": h1_atr,
                        "atr_pct200": h1_atr.rolling(200, min_periods=50).rank(pct=True)})
    f = f.join(align_closed(f.index, m5x, 5, "m5_"))
    # "typical" volatility (24 h median): the survival rule must not trip on a single news spike
    f["m5_atr_typ"] = f["m5_atr"].rolling(1440, min_periods=60).median()
    f = f.join(align_closed(f.index, m15x, 15, "m15_"))
    f = f.join(align_closed(f.index, h1x, 60, "h1_"))

    # --- regime
    atr_pct = f["m5_atr"].rolling(200 * 5, min_periods=200).rank(pct=True)
    f["vol_regime"] = np.select([atr_pct < 1 / 3, atr_pct < 2 / 3], [0, 1], 2)
    f["trend_strength"] = f["m15_adx"]
    er = (c - c.shift(30)).abs() / c.diff().abs().rolling(30).sum()  # Kaufman efficiency ratio
    f["momentum_regime"] = (er > 0.3).astype(int)  # 1 = momentum, 0 = mean-reversion

    # --- session / calendar
    f = f.join(session_flags(f.index))
    f["min_to_event"] = minutes_to_next_event(f.index, events)

    # --- microstructure from ticks (live / when tick history is available)
    if micro is not None and not micro.empty:
        f = f.join(micro[["ofi", "trade_dir_ratio", "tick_count"]], how="left")
    else:
        f["ofi"], f["trade_dir_ratio"], f["tick_count"] = np.nan, np.nan, np.nan

    # --- other markets: 1- and 5-minute % change of closed bars (no look-ahead)
    left = pd.DataFrame(index=f.index + pd.Timedelta(minutes=1))
    for name, s in (correlated or {}).items():
        if s is None or len(s) == 0 or name.startswith("_"):
            continue
        s = s[~s.index.duplicated()].sort_index().astype(float)
        for k in (1, 5):
            close_t = pd.DataFrame({"v": s.pct_change(k, fill_method=None).values * 1e4},  # basis points
                                   index=s.index + pd.Timedelta(minutes=1))
            f[f"{name}_chg{k}"] = pd.merge_asof(left, close_t, left_index=True, right_index=True, direction="backward",
                                                tolerance=pd.Timedelta(minutes=10))["v"].values
    # float32 halves memory (years of M1 bars); prices/spread stay float64 for exact P&L maths
    keep64 = {"open", "high", "low", "close", "spread", "vwap", "m5_swing_hi", "m5_swing_lo", "h1_high", "h1_low",
              "m15_ema_fast", "m15_ema_slow"}
    for col in f.columns:
        if col not in keep64 and f[col].dtype == np.float64:
            f[col] = f[col].astype(np.float32)
    return f


MODEL_FEATURES = [
    "rsi14", "macd", "macd_sig", "macd_hist", "atr1", "bb_pctb", "vwap_dev_atr", "obv_slope", "adx1",
    "stoch_k", "stoch_d", "ret1", "ret5", "spread", "spread_ratio", "tick_volume", "vol_ratio",
    "ofi", "trade_dir_ratio", "hour", "dow", "min_to_event", "sess_asia", "sess_london", "sess_ny",
    "vol_regime", "trend_strength", "momentum_regime", "m5_atr", "h1_atr_pct200",
]  # + every "<market>_chg1/_chg5" context column present (see model_columns)


def model_columns(f: pd.DataFrame) -> list[str]:
    ctx = [c for c in f.columns if c.endswith(("_chg1", "_chg5")) and c not in MODEL_FEATURES]
    return [c for c in MODEL_FEATURES if c in f.columns] + sorted(ctx)


def make_label(f: pd.DataFrame, horizon_m5_bars: int = 5) -> pd.Series:
    """Binary label: 1 if close 5 M5 bars (25 min) after this bar's close is higher, else 0. NaN at the tail."""
    h = horizon_m5_bars * 5
    fut = f["close"].shift(-h)
    y = (fut > f["close"]).astype(float)
    return y.where(fut.notna())
