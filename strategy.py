"""Gold quote features and the local, explainable three-way classifier."""
from __future__ import annotations

import numpy as np
import pandas as pd
from sklearn.linear_model import LogisticRegression

MINUTE = 60_000
DAY = 1_440 * MINUTE
DIRECTION = {-1: "SELL", 0: "WAIT", 1: "BUY"}


def quote_frame(raw):
    """Preserve tick order within a millisecond. Each row is an executable quote."""
    data = pd.DataFrame(raw)
    if not {"ms", "bid", "ask"}.issubset(data):
        raise ValueError("Quote data must have ms, bid and ask columns.")
    data = data.loc[:, ["ms", "bid", "ask"]].apply(pd.to_numeric, errors="coerce")
    valid = np.isfinite(data).all(axis=1) & (data.ms > 0) & (data.bid > 0) & (data.ask >= data.bid)
    data = data.loc[valid].astype({"ms": "int64", "bid": "float64", "ask": "float64"})
    if not data.ms.is_monotonic_increasing:
        data = data.sort_values("ms", kind="stable")
    return data.reset_index(drop=True)


def _minute_bars(ticks):
    mid = (ticks.ask + ticks.bid) / 2
    move = mid.diff().where(ticks.ms.diff() <= 5000, 0).fillna(0)
    quotes = pd.DataFrame({
        "mid": mid, "spread": ticks.ask - ticks.bid, "signed": np.sign(move),
        "updates": move.ne(0).astype(int), "squared": (move / mid).pow(2), "ms": ticks.ms,
    })
    bucket = ticks.ms // MINUTE * MINUTE + MINUTE
    bars = quotes.groupby(bucket, sort=True).agg(
        open=("mid", "first"), high=("mid", "max"), low=("mid", "min"), close=("mid", "last"),
        last_spread=("spread", "last"), avg_spread=("spread", "mean"), max_spread=("spread", "max"),
        signed=("signed", "sum"), updates=("updates", "sum"), squared=("squared", "sum"),
        quote_count=("ms", "size"), last_quote=("ms", "last"),
    )
    bars.index.name = "decision_ms"
    return bars


def _segment(b):
    c = b.close
    prev = c.shift()
    tr = pd.concat([b.high - b.low, (b.high - prev).abs(), (b.low - prev).abs()], axis=1).max(axis=1)
    atr = tr.rolling(14).mean()
    unit = atr.where(atr > 0)
    x = pd.DataFrame(index=b.index)
    for n in (1, 3, 5, 15, 60):
        x[f"gold_change_{n}m_atr"] = c.diff(n) / unit
    for n in (5, 15, 60):
        x[f"gold_mean_gap_{n}m_atr"] = (c - c.rolling(n).mean()) / unit
    x["gold_body_atr"] = (c - b.open) / unit
    x["gold_range_atr"] = (b.high - b.low) / unit
    x["range_location_15m"] = (c - b.low.rolling(15).min()) / (
        b.high.rolling(15).max() - b.low.rolling(15).min()).replace(0, np.nan)
    up = c.diff().clip(lower=0).rolling(14).sum()
    down = (-c.diff()).clip(lower=0).rolling(14).sum()
    x["rsi_14_scaled"] = (up - down) / (up + down).replace(0, np.nan)
    x["atr_basis_points"] = 10_000 * atr / c
    x["tick_volatility_bps"] = 10_000 * np.sqrt(b.squared)
    x["volatility_ratio_5m_30m"] = b.squared.rolling(5).mean() / b.squared.rolling(30).mean().replace(0, np.nan)
    x["spread_last_atr"] = b.last_spread / unit
    x["spread_mean_atr"] = b.avg_spread / unit
    x["spread_max_atr"] = b.max_spread / unit
    x["spread_vs_15m"] = b.last_spread / b.avg_spread.rolling(15).mean().replace(0, np.nan)
    x["quote_rate_vs_15m"] = b.quote_count / b.quote_count.rolling(15).mean()
    x["quote_imbalance_1m"] = b.signed / b.updates.clip(lower=1)
    x["quote_imbalance_5m"] = b.signed.rolling(5).sum() / b.updates.rolling(5).sum().clip(lower=1)
    x["quote_age_seconds"] = (b.index - b.last_quote) / 1000
    phase = 2 * np.pi * (b.index % DAY) / DAY
    x["utc_sin"], x["utc_cos"] = np.sin(phase), np.cos(phase)
    x["utc_weekday"] = pd.to_datetime(b.index, unit="ms", utc=True).dayofweek
    x["atr_usd_oz"] = atr
    return x


def features(ticks, markets=None, macro=None, asof_ms=None):
    """Index is the UTC decision instant. The boundary tick belongs to the next bar."""
    if ticks.empty:
        raise ValueError("No valid two-sided quotes.")
    cutoff = int(ticks.ms.iloc[-1]) if asof_ms is None else int(asof_ms)
    bars = _minute_bars(ticks)
    bars = bars.loc[bars.index <= cutoff]
    if bars.empty:
        return pd.DataFrame()
    segments = bars.index.to_series().diff().ne(MINUTE).cumsum()
    x = pd.concat([_segment(group) for _, group in bars.groupby(segments)]).sort_index()
    if markets is not None and not markets.empty:
        for name, group in markets.groupby("series"):
            group = group.sort_values("known_ms").drop_duplicates("known_ms", keep="last").set_index("known_ms")
            close = group.value.where(group.value > 0)
            for minutes in (1, 5, 15, 60):
                prior = close.reindex(close.index - minutes * MINUTE).to_numpy()
                returns = (close / prior - 1) * 10_000
                x[f"{name}_return_{minutes}m_bps"] = returns.reindex(x.index, method="ffill", tolerance=2*MINUTE)
    if macro is not None and not macro.empty:
        for name, group in macro.groupby("series"):
            g = group.sort_values(["known_ms", "observed_ms"]).drop_duplicates("known_ms", keep="last")
            g = g.loc[g.observed_ms.eq(g.observed_ms.cummax())].set_index("known_ms")
            level = g.value.reindex(x.index, method="ffill", tolerance=10*DAY)
            observed = g.observed_ms.reindex(x.index, method="ffill", tolerance=10*DAY)
            fresh = (x.index - observed) <= 10*DAY
            x[f"{name}_level"] = level.where(fresh)
            x[f"{name}_change"] = g.value.diff().reindex(x.index, method="ffill", tolerance=10*DAY).where(fresh)
    return x.replace([np.inf, -np.inf], np.nan).loc[lambda a: a.atr_usd_oz.gt(0) & a.gold_change_15m_atr.notna()]


def fit_model(x, y):
    y = np.asarray(y, dtype=int)
    if len(x) < 150 or not {-1, 1}.issubset(set(y)):
        raise ValueError("Training needs at least 150 eligible quote triggers and examples of both directions. Capture more dates.")
    # Fit every imputation, scale and feature selection on the training slice only.
    used = x.columns[(x.count() >= max(30, int(0.10 * len(x)))) & (x.nunique(dropna=True) > 1)]
    data = x[used]
    median = data.median()
    scale = data.fillna(median).std(ddof=0).clip(lower=1e-9)
    z = ((data.fillna(median) - median) / scale).clip(-6, 6)
    clf = LogisticRegression(C=0.15, solver="lbfgs", max_iter=500).fit(z, y)
    weight, bias = clf.coef_, clf.intercept_
    if len(clf.classes_) == 2:
        weight = np.vstack([np.zeros_like(weight[0]), weight[0]])
        bias = np.array([0.0, bias[0]])
    return {"features": list(used), "median": median.tolist(), "scale": scale.tolist(),
            "classes": clf.classes_.tolist(), "weight": weight.tolist(), "bias": bias.tolist()}


def quote_triggers(ticks, s):
    """First qualifying impulse of each sign per short bucket, timestamped as it arrives."""
    spread = ticks.ask - ticks.bid
    mid = (ticks.bid + ticks.ask)/2
    jump = mid.diff()
    gap = ticks.ms.diff()
    active = gap.between(0, 2000) & jump.abs().ge(np.maximum(s["min_jump_usd_oz"], s["min_jump_spreads"]*spread))
    t = pd.DataFrame({"ms": ticks.ms, "tick_impulse_spreads": jump/spread.clip(lower=.01),
                      "tick_gap_log_ms": np.log1p(gap.clip(lower=0)), "tick_spread": spread,
                      "tick_spread_change": spread.diff()}).loc[active]
    t = t.drop_duplicates("ms", keep="first")
    keys = pd.DataFrame({"bucket":t.ms // s["trigger_bucket_ms"], "side":np.sign(t.tick_impulse_spreads)})
    return t.loc[~keys.duplicated()].set_index("ms")


def candidate_features(bars, points, s):
    if points.empty or bars.empty:
        return pd.DataFrame()
    src = bars.reindex(points.index // MINUTE * MINUTE)
    src.index = points.index
    x = src.join(points)
    x = x.loc[x.atr_usd_oz.notna() & x.quote_age_seconds.le(s["entry_window_seconds"])].copy()
    distance = np.maximum(s["minimum_stop_usd_oz"], s["stop_atr"] * x.atr_usd_oz)
    x["tick_spread_fraction"] = x.tick_spread / distance
    return x.loc[x.tick_spread.le(s["max_spread_usd_oz"]) & x.tick_spread_fraction.le(s["max_spread_fraction_of_stop"])]


def predict(model, x):
    raw = x.reindex(columns=model["features"]).to_numpy(dtype=float)
    median, scale = np.array(model["median"]), np.array(model["scale"])
    z = np.clip((np.where(np.isfinite(raw), raw, median) - median) / scale, -6, 6)
    logits = z @ np.array(model["weight"]).T + np.array(model["bias"])
    exp = np.exp(logits - logits.max(axis=1, keepdims=True))
    return exp / exp.sum(axis=1, keepdims=True), z


def stop_distance(atr, s):
    return max(s["minimum_stop_usd_oz"], s["stop_atr"] * float(atr))


def spread_ok(spread, distance, s):
    return 0 <= spread <= min(s["max_spread_usd_oz"], s["max_spread_fraction_of_stop"] * distance)


def event_mask(times, events, s):
    """Support event revisions/cancellations without showing the revised schedule early."""
    times = np.asarray(times, dtype=np.int64)
    blocked = np.zeros(len(times), dtype=bool)
    if events is None or events.empty:
        return blocked
    lead = (60*s["news_before_minutes"] + s["hold_seconds"]) * 1000
    lag = s["news_after_minutes"] * MINUTE
    for _, revisions in events.groupby("event_id"):
        revisions = revisions.sort_values("known_ms").drop_duplicates("known_ms", keep="last")
        end_times = list(revisions.known_ms.iloc[1:]) + [np.iinfo(np.int64).max]
        for row, until in zip(revisions.itertuples(), end_times):
            if row.active:
                begin = max(row.known_ms, row.event_ms - lead)
                end = min(until, row.event_ms + lag + 1)
                blocked |= (times >= begin) & (times < end)
    return blocked
