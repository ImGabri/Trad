"""Runtime optimisations must not change any value: cached MT5 windows == full downloads, fast resample == resample,
vectorised rule helpers == their pandas originals."""
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT)); sys.path.insert(0, str(ROOT / "tests"))

from synthetic import synthetic_m1  # noqa: E402


def test_cached_rates_equal_full_download():
    import importlib
    import fake_mt5
    m1 = synthetic_m1(days=12, seed=4)
    mod = fake_mt5.install(m1, start_bar=9000)
    import data
    importlib.reload(data)
    from config import load_config
    cfg = load_config(ROOT / "config.yaml", use_tuned=False)
    cfg.broker.server_utc_offset_hours = 2
    conn = data.MT5Connection(cfg)
    assert conn.connect()
    fake = mod._fake
    full_calls = []
    orig = conn.rates_latest
    conn.rates_latest = lambda s, tf, n: (full_calls.append(n), orig(s, tf, n))[1]

    def check(count=5001):
        got = conn.rates_latest_cached("XAUUSD", "M1", count)
        ref = orig("XAUUSD", "M1", count)
        pd.testing.assert_frame_equal(got, ref, check_exact=True, check_freq=False)
        assert got.index.dtype == ref.index.dtype

    check()
    for step in range(200):
        fake.cur += 1
        if step == 50:
            fake.cur += 5                      # several bars at once: still a tail update
        if step == 80:
            fake.cur += 30                     # gap beyond the tail → full download
        if step == 120:                        # a closed bar inside the tail was revised → full download
            fake.m1.iloc[fake.cur - 4, fake.m1.columns.get_loc("close")] += 0.25
        if step == 150:
            assert conn.connect()              # reconnect invalidates the cache
        check()
    n_full = sum(1 for n in full_calls if n == 5001)
    assert 4 <= n_full <= 10, n_full          # first + gap + revision + reconnect + periodic refreshes
    assert sum(1 for n in full_calls if n == data.RATES_TAIL) > 150
    # a window shorter than asked (history still loading) is never extended from the cache
    big = fake.cur + 100
    for _ in range(3):
        fake.cur += 1
        got = conn.rates_latest_cached("XAUUSD", "M1", big)
        pd.testing.assert_frame_equal(got, orig("XAUUSD", "M1", big), check_freq=False)
        assert len(got) == fake.cur and ("XAUUSD", "M1", big) not in conn._rates_cache


def test_fast_resample_matches_resample():
    from features import _resample_ohlc, resample
    m1 = synthetic_m1(days=20, seed=2)
    for a, b in ((0, 13000), (5003, 18000), (777, 900)):
        w = m1.iloc[a:b]
        for mins in (5, 15, 60, 240, 1440):
            pd.testing.assert_frame_equal(_resample_ohlc(w, mins), resample(w, mins)[["open", "high", "low", "close"]],
                                          check_exact=True, check_freq=False)
    w = m1.iloc[:3000].copy()
    w.iloc[10, w.columns.get_loc("high")] = np.nan   # NaN → falls back to pandas
    pd.testing.assert_frame_equal(_resample_ohlc(w, 5), resample(w, 5)[["open", "high", "low", "close"]],
                                  check_freq=False)


def test_vectorised_rule_helpers_match_pandas():
    from config import load_config
    from features import build_features
    from strategy import _closed_h1, at_key_level, compute_signals, key_levels, m15_bias, prev_level_break, turbo_signal
    import ui
    cfg = load_config(ROOT / "config.yaml", use_tuned=False, overrides=ui.STYLES["hypermax5"])
    m1 = synthetic_m1(days=20, seed=6)
    f = build_features(m1.iloc[-13000:], cfg)
    lv = key_levels(f, m15_bias(f, cfg), cfg)
    tol = cfg.strategy.level_tolerance_atr * f["m5_atr"]
    ref = pd.concat([(lv[c] >= f["low"] - tol) & (lv[c] <= f["high"] + tol) for c in lv], axis=1).any(axis=1)
    assert at_key_level(f, lv, cfg).equals(ref)
    h1 = _closed_h1(f)
    for lb in (12, 24, 48):
        assert turbo_signal(f, lookback_h=lb, h1=h1).equals(turbo_signal(f, lookback_h=lb))
    src = m1.resample("60min").agg({"open": "first", "high": "max", "low": "min", "close": "last"}).dropna()
    t = f.index + pd.Timedelta(hours=2)   # prev_level_break's weekly keys: np.unique(key) == sorted key.unique()
    key = t.normalize() - pd.to_timedelta(t.dayofweek, unit="D")
    assert pd.DatetimeIndex(np.unique(key)).equals(pd.DatetimeIndex(key.unique()).sort_values())
    w = prev_level_break(f, "W", "long", src=src)
    assert w.index.equals(f.index) and set(np.unique(w)) <= {0, 1}
    sig = compute_signals(f, cfg, htf=src)
    assert len(sig) == len(f) and sig["rule_signal"].dtype == np.int8
