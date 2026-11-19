"""Rule layer (§3): M15 bias → M5 key-level setup → M1 momentum trigger. Plus session & news state.

Produces per-M1-bar columns used identically by the backtester and the live loop:
  bias (+1/-1/0), at_level (bool), trigger (+1/-1/0), rule_signal (+1/-1/0)
  session ("london","newyork","asia","off"), entry_blocked (bool), news_phase ("none","pre","event","post")
"""
from __future__ import annotations

from datetime import time as dtime

import numpy as np
import pandas as pd


def _t(s: str) -> dtime:
    h, m = s.split(":")
    return dtime(int(h), int(m))


def m15_bias(f: pd.DataFrame, cfg) -> pd.Series:
    s = cfg.strategy
    trend = f["m15_adx"] > s.m15_adx_min
    long_ = (f["m15_ema_fast"] > f["m15_ema_slow"]) & (f["m15_ema_fast_slope"] > 0) & (f["m15_ema_slow_slope"] >= 0) & trend
    short = (f["m15_ema_fast"] < f["m15_ema_slow"]) & (f["m15_ema_fast_slope"] < 0) & (f["m15_ema_slow_slope"] <= 0) & trend
    return pd.Series(np.select([long_, short], [1, -1], 0), index=f.index)


def key_levels(f: pd.DataFrame, bias: pd.Series, cfg) -> pd.DataFrame:
    """VWAP, prior closed H1 high/low, and fib retracements of the M5 swing in the bias direction."""
    hi, lo = f["m5_swing_hi"], f["m5_swing_lo"]
    rng = hi - lo
    lv = {"vwap": f["vwap"], "h1_high": f["h1_high"], "h1_low": f["h1_low"]}
    for r in cfg.strategy.fib_levels:
        # long bias: buy the pullback from the high; short: sell the bounce from the low
        lv[f"fib{int(r * 1000)}"] = np.where(bias > 0, hi - r * rng, np.where(bias < 0, lo + r * rng, np.nan))
    return pd.DataFrame(lv, index=f.index)


def at_key_level(f: pd.DataFrame, levels: pd.DataFrame, cfg) -> pd.Series:
    tol = cfg.strategy.level_tolerance_atr * f["m5_atr"]
    # touched during the bar: level within [low - tol, high + tol]
    near = [(levels[c] >= f["low"] - tol) & (levels[c] <= f["high"] + tol) for c in levels]
    return pd.concat(near, axis=1).any(axis=1)


def m1_trigger(f: pd.DataFrame, cfg) -> pd.Series:
    n = cfg.strategy.m1_trigger_bars
    body = np.sign(f["close"] - f["open"])
    up = body.rolling(n).min() > 0
    dn = body.rolling(n).max() < 0
    if cfg.strategy.require_rising_volume:
        v = f["tick_volume"]
        rising = pd.concat([v.shift(i) > v.shift(i + 1) for i in range(n - 1)], axis=1).all(axis=1)
        up, dn = up & rising, dn & rising
    return pd.Series(np.select([up, dn], [1, -1], 0), index=f.index)


def session_state(idx: pd.DatetimeIndex, cfg) -> pd.DataFrame:
    s = cfg.sessions
    dow = idx.dayofweek
    mins = np.asarray(idx.hour * 60 + idx.minute)

    def within(a, b):
        a, b = _t(a), _t(b)
        return (mins >= a.hour * 60 + a.minute) & (mins < b.hour * 60 + b.minute)

    london, ny, asia = within(*s.london), within(*s.newyork), within(*s.asia)
    # label by primary window (London > NY > Asia)
    sess = np.where(london, "london", np.where(ny, "newyork", np.where(asia, "asia", "off")))
    blocked = np.zeros(len(idx), bool)
    for w in s.no_entry_windows:
        a, b = _t(w.start), _t(w.end)
        blocked |= (dow == w.day) & (mins >= a.hour * 60 + a.minute) & (mins < b.hour * 60 + b.minute)
    closed = market_closed(idx, cfg)
    if not s.asia_enabled:
        blocked |= sess == "asia"
    flat_mins = _t(s.flat_by.time)
    must_flat = (dow == s.flat_by.day) & (mins >= flat_mins.hour * 60 + flat_mins.minute)
    return pd.DataFrame({"session": sess, "entry_blocked": blocked | closed | must_flat,
                         "market_closed": closed, "must_flat": must_flat}, index=idx)


def market_closed(idx: pd.DatetimeIndex, cfg) -> np.ndarray:
    o, c = cfg.sessions.week_open, cfg.sessions.week_close
    ot, ct = _t(o.time), _t(c.time)
    dow, mins = idx.dayofweek, idx.hour * 60 + idx.minute
    after_close = ((dow == c.day) & (mins >= ct.hour * 60 + ct.minute)) | (dow == 5)
    before_open = (dow == o.day) & (mins < ot.hour * 60 + ot.minute)
    return np.asarray(after_close | before_open)


def news_phase(idx: pd.DatetimeIndex, events: pd.DatetimeIndex | None, cfg) -> pd.DataFrame:
    """Phase relative to the nearest high-impact event and that event's id (for one-scalp-per-event)."""
    n = len(idx)
    phase = np.full(n, "none", dtype=object)
    ev_id = np.full(n, -1)
    if events is None or len(events) == 0:
        return pd.DataFrame({"news_phase": phase, "event_id": ev_id}, index=idx)
    nc = cfg.news
    ev = np.sort(events.values.astype("datetime64[ns]"))
    t = idx.values.astype("datetime64[ns]") + np.timedelta64(1, "m")  # decisions happen at bar close
    pos = np.searchsorted(ev, t)
    for cand in (pos - 1, pos):
        ok = (cand >= 0) & (cand < len(ev))
        c = np.clip(cand, 0, len(ev) - 1)
        dm = (t - ev[c]) / np.timedelta64(1, "m")  # minutes relative to event (+ after)
        pre = ok & (dm >= -nc.pre_block_min[0]) & (dm < -nc.pre_block_min[1])
        evw = ok & (dm >= -nc.event_window_min) & (dm < nc.event_window_min)
        post = ok & (dm >= nc.post_window_min[0]) & (dm < nc.post_window_min[1])
        # precedence: event > pre > post
        for mask, name in ((post, "post"), (pre, "pre"), (evw, "event")):
            upd = mask & ((phase == "none") | (name == "event") | ((name == "pre") & (phase == "post")))
            phase[upd] = name
            ev_id[upd] = c[upd]
    return pd.DataFrame({"news_phase": phase, "event_id": ev_id}, index=idx)


def compute_signals(f: pd.DataFrame, cfg, events: pd.DatetimeIndex | None = None) -> pd.DataFrame:
    out = pd.DataFrame(index=f.index)
    out["bias"] = m15_bias(f, cfg)
    lv = key_levels(f, out["bias"], cfg)
    out["at_level"] = at_key_level(f, lv, cfg)
    out["trigger"] = m1_trigger(f, cfg)
    out["rule_signal"] = np.where((out["bias"] != 0) & out["at_level"] & (out["trigger"] == out["bias"]),
                                  out["bias"], 0)
    out = out.join(session_state(f.index, cfg)).join(news_phase(f.index, events, cfg))
    return out
