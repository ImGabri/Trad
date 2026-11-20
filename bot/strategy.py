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
    tol = (cfg.strategy.level_tolerance_atr * f["m5_atr"]).to_numpy()
    # touched during the bar: level within [low - tol, high + tol] (numpy: same comparisons, no per-level frames)
    lo, hi = f["low"].to_numpy() - tol, f["high"].to_numpy() + tol
    near = np.zeros(len(f), bool)
    for c in levels:
        v = levels[c].to_numpy()
        near |= (v >= lo) & (v <= hi)
    return pd.Series(near, index=f.index)


def m1_trigger(f: pd.DataFrame, cfg) -> pd.Series:
    n = cfg.strategy.m1_trigger_bars
    body = np.sign(f["close"] - f["open"])
    up = body.rolling(n).min() > 0
    dn = body.rolling(n).max() < 0
    if cfg.strategy.require_rising_volume:
        v = f["tick_volume"]
        rising = pd.concat([v.shift(i) > v.shift(i + 1) for i in range(n - 1)], axis=1).all(axis=1)
        up, dn = up & rising, dn & rising
    # VOLUME CONFIRMATION: require above-median volume for trend triggers (only when the multiplier is > 1;
    # the rolling median is only computed then)
    mult = getattr(cfg.strategy, "vol_confirm_mult", 1.0)
    vol_ok = (f["tick_volume"] > f["tick_volume"].rolling(20).median() * mult) if mult > 1.0 else True
    up = up & vol_ok
    dn = dn & vol_ok
    
    # --- 15min trend filter: align M1 trigger with 15min bias
    # If 15min EMA slope is available, use it to filter fakeouts
    if "m15_ema_fast_slope" in f.columns and "m15_ema_slow_slope" in f.columns:
        m15_bias = np.where(
            (f["m15_ema_fast_slope"] > 0) & (f["m15_ema_slow_slope"] > 0), 1,
            np.where(
                (f["m15_ema_fast_slope"] < 0) & (f["m15_ema_slow_slope"] < 0), -1, 0
            )
        )
        # Only allow upward triggers when 15min bias is bullish, etc.
        up = up & (m15_bias != -1)  # filter short-bias upward triggers
        dn = dn & (m15_bias != 1)   # filter long-bias downward triggers
    
    return pd.Series(np.select([up, dn], [1, -1], 0), index=f.index)


def session_state(idx: pd.DatetimeIndex, cfg) -> pd.DataFrame:
    if cfg.strategy.skip_market_open_check:
        # Skip all session/market checks for uninterrupted 24/7 trading
        n = len(idx)
        return pd.DataFrame({
            "session": np.full(n, "off"),  # Always "off" when skipping checks
            "entry_blocked": np.zeros(n, bool),
            "market_closed": np.zeros(n, bool),
            "must_flat": np.zeros(n, bool),
        }, index=idx)
    
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
    if cfg.strategy.skip_market_open_check:
        return np.zeros(len(idx), bool)
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


def _closed_h1(f: pd.DataFrame) -> pd.DataFrame:
    """H1 high / low / close resampled from the M1 rows (bars with no M1 rows dropped)."""
    g = f[["high", "low", "close"]].resample("60min", label="left", closed="left")
    return pd.DataFrame({"high": g["high"].max(), "low": g["low"].min(), "close": g["close"].last()}).dropna()


def turbo_signal(f: pd.DataFrame, lookback_h: int = 24, ema_fast: int = 20, ema_slow: int = 50,
                 direction: str = "long", h1: pd.DataFrame | None = None) -> pd.Series:
    """🚀 Turbo: buy the first M1 close above the highest high of the last 24 CLOSED H1 bars, only while the closed-H1
    EMA20 > EMA50 trend is up; at most one signal per clock hour. `direction="both"` also SELLS the first close below
    the lowest low of the last 24 H1 bars while EMA20 < EMA50 (research: better in 2024–26, blew €100 in 2019–23).
    Decided at the M1 close (no look-ahead: H1 values join on H1 close time).
    `h1`: the same frame `_closed_h1(f)` precomputed (compute_signals shares it between the lookbacks)."""
    idx = f.index
    h1 = _closed_h1(f) if h1 is None else h1
    ef = h1.close.ewm(span=ema_fast, adjust=False, min_periods=ema_fast).mean()
    es = h1.close.ewm(span=ema_slow, adjust=False, min_periods=ema_slow).mean()
    x = pd.DataFrame({"up": np.sign(ef - es), "hi": h1.high.rolling(lookback_h).max(), "lo": h1.low.rolling(lookback_h).min()})
    x.index = x.index + pd.Timedelta(minutes=60)
    a = pd.merge_asof(pd.DataFrame(index=idx + pd.Timedelta(minutes=1)), x, left_index=True, right_index=True,
                      direction="backward")
    a.index = idx
    above = (f["close"] > a["hi"]).astype(int)
    first = (above == 1) & (above.shift(1, fill_value=0) == 0) & (a["up"] > 0)
    sig = pd.Series(0, index=idx, dtype=int)
    sig[first] = 1
    if str(direction).lower() == "both":
        below = (f["close"] < a["lo"]).astype(int)
        first_s = (below == 1) & (below.shift(1, fill_value=0) == 0) & (a["up"] < 0)
        sig[first_s & ~first] = -1
    hours = pd.Series(idx.floor("60min"), index=idx)[sig != 0]
    drop = hours.index[hours.duplicated()]            # at most one entry per clock hour
    sig.loc[drop] = 0
    return sig


def basket_signal(f: pd.DataFrame, cfg) -> pd.Series:
    """🧺 Basket: direction from the last N candles — all N up → +1 (buy), all N down → −1 (sell); `follow: false`
    fades them instead. On M5 candles the signal is only given at each M5 close. No look-ahead (closed candles only)."""
    b = cfg.basket
    n = int(getattr(b, "candles", 3))
    idx = f.index
    if str(getattr(b, "candles_tf", "m5")).lower() == "m1":
        body = np.sign(f["close"] - f["open"])
        r = body.rolling(n)
        s = pd.Series(np.where(r.min() > 0, 1, np.where(r.max() < 0, -1, 0)), index=idx)
    else:
        m5 = f[["open", "close"]].resample("5min", label="left", closed="left").agg({"open": "first", "close": "last"}).dropna()
        body = np.sign(m5["close"] - m5["open"])
        r = body.rolling(n)
        s5 = pd.Series(np.where(r.min() > 0, 1, np.where(r.max() < 0, -1, 0)), index=m5.index + pd.Timedelta(minutes=5))
        al = pd.merge_asof(pd.DataFrame(index=idx + pd.Timedelta(minutes=1)), s5.rename("v").to_frame(),
                           left_index=True, right_index=True, direction="backward")["v"].fillna(0).to_numpy()
        s = pd.Series(np.where((idx.minute % 5) == 4, al, 0), index=idx)
    tr = str(getattr(b, "trend", "") or "").lower()
    if tr in ("m15", "h1"):  # directional: only trade WITH the closed-bar trend
        t = closed_trend(f, 15 if tr == "m15" else 60, *((50, 200) if tr == "m15" else (20, 50)))
        want = -t if str(getattr(b, "entry", "follow")).lower() == "dip" else t   # dip: candle against the trend
        return pd.Series(np.where((s.to_numpy() == want) & (t != 0), t, 0), index=idx).astype(int)
    return (s * (1 if getattr(b, "follow", True) else -1)).astype(int)


def closed_trend(f: pd.DataFrame, tf_min: int, fast: int, slow: int, close: pd.Series | None = None) -> np.ndarray:
    """+1 / −1 / 0 = EMA(fast) vs EMA(slow) on CLOSED tf_min-minute bars, aligned to each M1 bar's close.
    `close` may be a longer, coarser close series (e.g. live H1 bars) to compute the same trend from more history."""
    c = (f["close"] if close is None else close).resample(f"{tf_min}min", label="left", closed="left").last().dropna()
    t = np.sign(c.ewm(span=fast, adjust=False, min_periods=slow).mean() - c.ewm(span=slow, adjust=False, min_periods=slow).mean())
    t.index = t.index + pd.Timedelta(minutes=tf_min)
    a = pd.merge_asof(pd.DataFrame(index=f.index + pd.Timedelta(minutes=1)), t.rename("t").to_frame(),
                      left_index=True, right_index=True, direction="backward")["t"]
    return a.fillna(0).to_numpy()


def _first_per_key(ev: pd.Series, key) -> pd.Series:
    return ev & (ev.astype(int).groupby(key).cumsum() == 1)


def prev_level_break(f: pd.DataFrame, period: str = "D", direction: str = "both", h4: np.ndarray | None = None,
                     src: pd.DataFrame | None = None) -> pd.Series:
    """Break of the previous COMPLETED day's (UTC day) or week's (Sunday 22:00 UTC week) high/low by an M1 close; first
    break only, one entry per day/week. `h4`: only in the H4 trend direction. `src`: a longer bar history (e.g. live H1
    bars) to read the previous period's levels from; default = f itself. Research: agent session study (2019–2026)."""
    idx = f.index
    shift_h = 2 if period == "W" else 0
    def keys(ix):
        t = ix + pd.Timedelta(hours=shift_h)
        day = t.normalize()
        return day - pd.to_timedelta(t.dayofweek, unit="D") if period == "W" else day
    own = src is None
    src = f if own else src
    minbars = (1500 if period == "W" else 300) if own else (25 if period == "W" else 5)  # M1 vs H1 bars per full period
    k_src = keys(src.index)
    g = src.groupby(k_src)
    D = pd.DataFrame({"hi": g["high"].max(), "lo": g["low"].min(), "n": g.size()})
    full = D.n >= minbars
    ph = D.hi.where(full).ffill().shift(1); pl = D.lo.where(full).ffill().shift(1)
    key = keys(idx)
    if not own:  # live: the current (partial) period may not be in src yet → extend
        allk = D.index.union(pd.DatetimeIndex(key.unique()).sort_values())  # = np.unique(key), without an object sort
        ph = D.hi.where(full).reindex(allk).ffill().shift(1); pl = D.lo.where(full).reindex(allk).ffill().shift(1)
    PH = pd.Series(ph.reindex(key).to_numpy(), index=idx); PL = pd.Series(pl.reindex(key).to_numpy(), index=idx)
    c = f["close"]; up, dn = c > PH, c < PL
    if direction == "long":
        sig = pd.Series(np.where(_first_per_key(up, key), 1, 0), index=idx)
    else:
        ev = _first_per_key(up | dn, key)
        sig = pd.Series(np.where(ev & up, 1, np.where(ev & dn, -1, 0)), index=idx)
    if h4 is not None:
        sig = sig.where(np.sign(sig.to_numpy()) == np.asarray(h4), 0)
    return sig.astype(int)


def turtle_breakout(f: pd.DataFrame, days: int = 20, src: pd.DataFrame | None = None) -> pd.Series:
    """Turtle System-1 style, buy-only: first M1 close above the highest high of the previous `days` CLOSED UTC daily
    bars, once per hour. `src`: longer bar history (live H1 bars) for the daily highs; default = f."""
    src = f if src is None else src
    d1 = src["high"].resample("1440min", label="left", closed="left").max().dropna()
    hi = d1.rolling(days).max()
    hi.index = hi.index + pd.Timedelta(minutes=1440)   # known once the day closed
    a = pd.merge_asof(pd.DataFrame(index=f.index + pd.Timedelta(minutes=1)), hi.rename("hi").to_frame(),
                      left_index=True, right_index=True, direction="backward")["hi"].set_axis(f.index)
    c = f["close"]
    s = pd.Series(np.where((c > a) & (c.shift(1) <= a.shift(1)), 1, 0), index=f.index)
    hour = pd.Series(f.index.floor("60min"), index=f.index)
    keep = ~(hour.where(s != 0).duplicated()) | (s == 0)
    return s.where(keep, 0).astype(int)


def compute_signals(f: pd.DataFrame, cfg, events: pd.DatetimeIndex | None = None,
                    htf: pd.DataFrame | None = None) -> pd.DataFrame:
    """`htf`: optional longer H1 bar history (live) for daily/weekly levels; backtests derive them from f."""
    out = pd.DataFrame(index=f.index)
    out["bias"] = m15_bias(f, cfg)
    lv = key_levels(f, out["bias"], cfg)
    out["at_level"] = at_key_level(f, lv, cfg)
    out["trigger"] = m1_trigger(f, cfg)
    out["rule_signal"] = np.where((out["bias"] != 0) & out["at_level"] & (out["trigger"] == out["bias"]),
                                  out["bias"], 0)
    mode = str(getattr(cfg.strategy, "mode", "scalp")).lower()
    if mode == "turbo":
        d = str(getattr(cfg.strategy, "turbo_direction", "long"))
        sig = None
        h1 = _closed_h1(f)  # resampled once, shared by every lookback
        for lb in (getattr(cfg.strategy, "turbo_lookbacks", None) or [24]):   # ⚡ Hyper Perfect: 12 + 24 + 48 h
            z = turbo_signal(f, lookback_h=int(lb), direction=d, h1=h1)
            sig = z if sig is None else sig.where(sig != 0, z)
        reg = None
        if str(getattr(cfg.strategy, "turbo_regime", "") or "").lower() == "h4":
            # 4-hour bull/bear identifier (EMA20 vs EMA50 on closed H4 bars): buys only in a bullish H4 market,
            # sells (if enabled) only in a bearish one. Live passes the value in column h4_regime (from H1 history).
            reg = f["h4_regime"].to_numpy() if "h4_regime" in f else closed_trend(f, 240, 20, 50)
            sig = sig.where(((sig > 0) & (reg > 0)) | ((sig < 0) & (reg < 0)), 0)
        extras = [str(x).lower() for x in (getattr(cfg.strategy, "turbo_extras", None) or [])]
        if extras:  # 🎯🔥 Hyper Max add-ons (agent research 2019–2026), merged after the hourly breakouts
            src = htf if htf is not None and len(htf) else None   # live: longer H1 history for daily / weekly levels
            if reg is None:
                reg = closed_trend(f, 240, 20, 50)
            for x in extras:
                if x == "turtle20":
                    z = turtle_breakout(f, 20, src)
                elif x == "prevday":
                    z = prev_level_break(f, "D", "both", h4=reg)
                elif x == "prevweek":
                    z = prev_level_break(f, "W", "long", src=src)
                else:
                    continue
                sig = sig.where(sig != 0, z)
        out["rule_signal"] = sig
    elif mode == "basket":
        out["rule_signal"] = basket_signal(f, cfg)
    elif mode == "signals":  # 📡 Signal Hyper: no own entries — only the Telegram channel signals trade
        out["rule_signal"] = 0
    elif mode == "ai":  # 🤖 Hyper AI: no rules at all — the AI alone opens trades (strategy.ai_only_min_conf), both ways
        out["rule_signal"] = 0
        out["bias"] = 0
    out = out.join(session_state(f.index, cfg)).join(news_phase(f.index, events, cfg))
    for col in ("session", "news_phase"):  # categoricals: ~8× smaller than Python string objects
        out[col] = out[col].astype("category")
    out["bias"] = out["bias"].astype(np.int8)
    out["rule_signal"] = out["rule_signal"].astype(np.int8)
    out["trigger"] = out["trigger"].astype(np.int8)
    return out


def decide_entry(cfg, *, outlook_dir: int = 0, outlook_conf: float = 0.0, news: dict | None = None, **kw):
    """Rules + M1 AI decide; the market analyst (15-min outlook + optional news agent) can veto."""
    side, slm, tpm, kind, why = _decide_entry(cfg, **kw)
    an = getattr(cfg, "analyst", None)
    if side == 0 or an is None or not getattr(an, "enabled", True):
        return side, slm, tpm, kind, why
    veto = float(getattr(an, "veto_conf", 0) or 0)
    if veto and outlook_dir == -side and outlook_conf >= veto:
        return 0, 0, 0, "", f"15-min outlook disagrees ({outlook_conf:.2f})"
    if news:
        if news.get("trade_conditions") == "avoid":
            return 0, 0, 0, "", "news agent: avoid trading now"
        nv = float(getattr(an, "news_veto_conf", 0.65) or 0)
        nd = {"up": 1, "down": -1}.get(news.get("bias"), 0)
        if nv and nd == -side and float(news.get("confidence", 0)) >= nv:
            return 0, 0, 0, "", f"news agent expects {news.get('bias')} ({news.get('confidence'):.2f})"
    return side, slm, tpm, kind, why


def _decide_entry(cfg, *, rule: int, ai_dir: int, conf: float, has_ai: bool, session: str, blocked: bool,
                 phase: str, event_id: int, news_used: set, spread_ratio: float, atr_m5: float,
                 slippage_usd: float, bias: int = 0, ai_only_min_conf: float | None = None) -> tuple[int, float, float, str, str]:
    """Single source of truth for entry gating (backtest + live).
    Returns (side, sl_atr_mult, tp_atr_mult, kind, reason). side 0 = no entry, reason says why."""
    st, rk, nw = cfg.strategy, cfg.risk, cfg.news
    if blocked:
        return 0, 0, 0, "", "session_block"
    if not np.isfinite(atr_m5) or atr_m5 <= 0:
        return 0, 0, 0, "", "no_atr"
    if not np.isfinite(spread_ratio) or spread_ratio > rk.spread_filter_mult:
        return 0, 0, 0, "", "spread_filter"
    if slippage_usd > rk.max_slippage_pips * cfg.instrument.pip_size:
        return 0, 0, 0, "", "slippage_cap"
    if phase == "pre":
        return 0, 0, 0, "", "news_pre"
    if phase == "event":
        if (has_ai and event_id not in news_used and conf >= nw.news_scalp_min_conf and ai_dir != 0
                and spread_ratio < nw.news_scalp_spread_mult):
            return int(ai_dir), nw.news_scalp_sl_atr, nw.news_scalp_tp_atr, "news", "news_scalp"
        return 0, 0, 0, "", "news_event"
    thr = st.confidence_threshold
    if session == "asia" and not cfg.strategy.skip_market_open_check:
        thr = max(thr, cfg.sessions.asia_min_confidence)
    if rule == 0:
        # fast mode: very confident AI may enter without the full rule setup (never against the M15 bias)
        ai_only = getattr(st, "ai_only_min_conf", 0) if ai_only_min_conf is None else ai_only_min_conf
        if has_ai and ai_only and ai_dir != 0 and conf >= max(ai_only, thr) and bias in (0, ai_dir):
            slm = st.sl_atr * (nw.post_sl_mult if phase == "post" else 1.0)
            return int(ai_dir), slm, st.tp_atr, "ai", "ai_only"
        return 0, 0, 0, "", "no_setup"
    if not getattr(st, "ai_gate", True):  # Turbo: the breakout rule alone decides
        slm = st.sl_atr * (nw.post_sl_mult if phase == "post" else 1.0)
        return int(rule), slm, st.tp_atr, "normal", "setup"
    if ai_dir != rule:
        return 0, 0, 0, "", "ai_disagrees"
    if conf < thr:
        return 0, 0, 0, "", "low_confidence"
    slm = st.sl_atr * (nw.post_sl_mult if phase == "post" else 1.0)
    return int(rule), slm, st.tp_atr, "normal", "setup"
