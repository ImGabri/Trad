"""Market analyst: "how does the market look and what will probably happen next?"

1. M15Analyst — a LightGBM model that reads the 15-minute chart (trend, momentum, volatility, position in the
   day's range), the hourly trend, other markets and the news calendar, and predicts the next 60 minutes
   (UP/DOWN probability). Trained walk-forward, so backtests only ever see out-of-sample forecasts;
   retrained every weekend on all saved data (it trains on itself as the data grows).

2. Free headline analyst (news_local.py, no API key) — reads the latest gold / Fed / dollar headlines from
   Google News RSS every 15 minutes, scores them with a gold-specific lexicon and returns bias, confidence and
   trade conditions (normal / caution / avoid). Every call is graded 60 minutes later.

Both feed the entry decision: a strongly opposing outlook vetoes a trade; "avoid" pauses new entries.
"""
from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pandas as pd

from features import adx, atr, bb_pctb, ema, macd, resample, rsi

log = logging.getLogger("analyst")
HORIZON_BARS = 4  # 4 × M15 = the next 60 minutes


# ============================================================================ M15 model
def m15_frame(m1: pd.DataFrame, correlated: dict | None = None, events: pd.DatetimeIndex | None = None) -> pd.DataFrame:
    """Features per CLOSED 15-minute bar (index = bar open time)."""
    b = resample(m1, 15)
    c = b["close"]
    a = atr(b, 14)
    x = pd.DataFrame(index=b.index)
    for k in (1, 2, 4, 8, 16):
        x[f"r{k}"] = c.pct_change(k, fill_method=None) * 1e4
    x["rsi"] = rsi(c, 14)
    x["macd_h"] = macd(c)[2] / a
    x["atr_bps"] = a / c * 1e4
    x["atr_rel"] = a / a.rolling(96, min_periods=20).mean()
    x["pctb"] = bb_pctb(c)
    x["adx"] = adx(b, 14)
    e20, e50 = ema(c, 20), ema(c, 50)
    x["ema20_d"] = (c - e20) / a
    x["ema50_d"] = (c - e50) / a
    x["ema20_s"] = (e20 - e20.shift(4)) / a
    # hourly trend, visible to an M15 bar only once the H1 bar has CLOSED (merge on close times — no look-ahead)
    h1 = resample(m1, 60)["close"]
    h1_s = (ema(h1, 20) - ema(h1, 20).shift(3)).rename("v").to_frame()
    h1_s.index = h1_s.index + pd.Timedelta(minutes=60)
    left = pd.DataFrame(index=b.index + pd.Timedelta(minutes=15))
    x["h1_trend"] = pd.merge_asof(left, h1_s, left_index=True, right_index=True, direction="backward")["v"].values / a
    day = (b.index - pd.Timedelta(hours=22)).floor("D")
    hi, lo = b["high"].groupby(day).cummax(), b["low"].groupby(day).cummin()
    x["day_pos"] = (c - lo) / (hi - lo).replace(0, np.nan)
    x["body"] = (b["close"] - b["open"]) / a
    x["wick_up"] = (b["high"] - b[["open", "close"]].max(axis=1)) / a
    x["wick_dn"] = (b[["open", "close"]].min(axis=1) - b["low"]) / a
    x["vol_rel"] = b["tick_volume"] / b["tick_volume"].rolling(96, min_periods=20).mean()
    x["hour"], x["dow"] = b.index.hour, b.index.dayofweek
    if events is not None and len(events):
        t = (b.index + pd.Timedelta(minutes=15)).values.astype("datetime64[ns]")
        ev = np.sort(events.values.astype("datetime64[ns]"))
        pos = np.searchsorted(ev, t)
        nxt = np.where(pos < len(ev), (ev[np.minimum(pos, len(ev) - 1)] - t) / np.timedelta64(1, "m"), 1e4)
        prv = np.where(pos > 0, (t - ev[np.maximum(pos - 1, 0)]) / np.timedelta64(1, "m"), 1e4)
        x["min_to_event"], x["min_since_event"] = np.clip(nxt, 0, 1e4), np.clip(prv, 0, 1e4)
    for name, s in (correlated or {}).items():
        if s is None or len(s) == 0 or name.startswith("_"):
            continue
        s15 = s[~s.index.duplicated()].sort_index().resample("15min", label="left", closed="left").last()
        x[f"{name}_r1"] = s15.pct_change(1, fill_method=None).reindex(b.index) * 1e4
        x[f"{name}_r4"] = s15.pct_change(4, fill_method=None).reindex(b.index) * 1e4
    x["close"] = c
    return x.astype("float32").assign(close=c.astype("float64"))


def m15_label(x: pd.DataFrame) -> pd.Series:
    fut = x["close"].shift(-HORIZON_BARS)
    return (fut > x["close"]).astype(float).where(fut.notna())


class M15Analyst:
    PARAMS = dict(objective="binary", learning_rate=0.03, num_leaves=15, min_child_samples=200,
                  feature_fraction=0.8, bagging_fraction=0.8, bagging_freq=1, lambda_l2=5.0, verbose=-1, seed=11)

    def __init__(self, booster=None, features=None, meta=None):
        self.booster, self.features, self.meta = booster, features or [], meta or {}

    @classmethod
    def fit(cls, x: pd.DataFrame, rounds: int = 400) -> "M15Analyst":
        import lightgbm as lgb
        y = m15_label(x)
        feats = [c for c in x.columns if c != "close" and x[c].notna().mean() > 0.5]
        ok = y.notna()
        X, y = x.loc[ok, feats], y[ok]
        cut = int(len(X) * 0.85)
        dtr = lgb.Dataset(X.iloc[:cut], y.iloc[:cut])
        dva = lgb.Dataset(X.iloc[cut:], y.iloc[cut:], reference=dtr)
        b = lgb.train(cls.PARAMS, dtr, rounds, valid_sets=[dva], callbacks=[lgb.early_stopping(40, verbose=False)])
        from ai_model import auc
        pv = b.predict(X.iloc[cut:], num_iteration=b.best_iteration)
        return cls(b, feats, dict(rows=len(X), best_iteration=int(b.best_iteration or rounds),
                                  valid_auc=auc(y.iloc[cut:].to_numpy(), pv),
                                  trained_at=datetime.now(timezone.utc).isoformat()))

    def predict(self, x: pd.DataFrame) -> pd.Series:
        return pd.Series(self.booster.predict(x.reindex(columns=self.features),
                                              num_iteration=self.meta.get("best_iteration")), index=x.index)

    def save(self, folder: Path):
        folder.mkdir(parents=True, exist_ok=True)
        self.booster.save_model(str(folder / "m15.txt"))
        (folder / "m15.json").write_text(json.dumps(dict(features=self.features, **self.meta), indent=2, default=str))

    @classmethod
    def load(cls, folder: Path) -> "M15Analyst | None":
        try:
            import lightgbm as lgb
            meta = json.loads((folder / "m15.json").read_text())
            return cls(lgb.Booster(model_file=str(folder / "m15.txt")), meta.pop("features"), meta)
        except (FileNotFoundError, ValueError):
            return None


def walk_forward_m15(x: pd.DataFrame, months: int = 6, min_train_months: int = 12, say=print) -> pd.Series:
    """Out-of-sample P(up in the next 60 min) per M15 bar: expanding train, 6-month test blocks."""
    from ai_model import auc
    out = []
    start = x.index[0] + pd.DateOffset(months=min_train_months)
    while start < x.index[-1]:
        end = start + pd.DateOffset(months=months)
        tr, te = x[x.index < start], x[(x.index >= start) & (x.index < end)]
        if len(tr) > 5000 and len(te) > 200:
            m = M15Analyst.fit(tr)
            p = m.predict(te)
            y = m15_label(te)
            ok = y.notna()
            say(f"  M15 analyst {start:%Y-%m} → {end:%Y-%m}: AUC {auc(y[ok].to_numpy(), p[ok].to_numpy()):.3f}")
            out.append(p)
        start = end
    return pd.concat(out) if out else pd.Series(dtype=float)


def outlook_for_m1(p15: pd.Series, m1_index: pd.DatetimeIndex) -> pd.DataFrame:
    """Map M15 forecasts onto M1 rows, visible only after the M15 bar closed (no look-ahead)."""
    from features import align_closed
    al = align_closed(m1_index, pd.DataFrame({"p": p15}), 15, "o_")
    p = al["o_p"].to_numpy(float)
    return pd.DataFrame({"outlook_dir": np.where(np.isnan(p), 0, np.where(p >= 0.5, 1, -1)),
                         "outlook_conf": np.where(np.isnan(p), 0.0, np.maximum(p, 1 - p))}, index=m1_index)


def chart_snapshot(x15: pd.DataFrame, p_up: float | None, events_next: list[str], price: float) -> dict:
    """Compact numeric description of the latest 15-minute chart for the news agent."""
    r = x15.iloc[-1]
    last8 = x15["close"].iloc[-9:].round(2).tolist()
    return dict(time=str(x15.index[-1] + pd.Timedelta(minutes=15)), price=round(price, 2), last_8_m15_closes=last8,
                ret_15m_bps=round(float(r["r1"]), 1), ret_1h_bps=round(float(r["r4"]), 1),
                ret_4h_bps=round(float(r["r16"]), 1), rsi_14=round(float(r["rsi"]), 1), adx=round(float(r["adx"]), 1),
                atr_bps=round(float(r["atr_bps"]), 1), position_in_day_range=round(float(r["day_pos"]), 2),
                hourly_trend=round(float(r["h1_trend"]), 2),
                model_p_up_next_60min=None if p_up is None else round(p_up, 3), upcoming_events=events_next[:5])
