import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from backtest import holdout_range, run_backtest, run_with_baselines, summary_table, walk_forward_windows  # noqa: E402
from config import load_config  # noqa: E402
from data import Store, tick_microstructure  # noqa: E402
from features import align_closed, build_features, make_label, resample  # noqa: E402
from risk import min_required_equity, position_size  # noqa: E402
from strategy import compute_signals, news_phase, session_state  # noqa: E402
from synthetic import synthetic_m1  # noqa: E402


@pytest.fixture(scope="module")
def cfg():
    return load_config(ROOT / "config.yaml")


@pytest.fixture(scope="module")
def m1():
    return synthetic_m1(days=21)


@pytest.fixture(scope="module")
def feats(m1, cfg):
    return build_features(m1, cfg)


def test_htf_alignment_has_no_lookahead(m1):
    m5 = resample(m1, 5)
    al = align_closed(m1.index, m5[["close"]], 5, "m5_")
    t = m1.index[1000]
    seen = al.loc[t, "m5_close"]
    # the M5 bar visible at M1 bar t must have closed by t+1min
    src_open = m5.index[m5["close"] == seen][0]
    assert src_open + pd.Timedelta(minutes=5) <= t + pd.Timedelta(minutes=1)


def test_label_horizon(feats):
    y = make_label(feats, 5)
    assert y.iloc[:-25].notna().all() and y.iloc[-25:].isna().all()


def test_position_sizing_and_survival():
    # $10k, SL $3 -> risk $50 -> 50/(3*100+2) = 0.16 lots
    assert position_size(10_000, 3.0, 100, 0.5, commission_per_side=1.0) == 0.16
    # tiny account: 0.01 lot would risk more than 0.5% -> skip
    assert position_size(200, 3.0, 100, 0.5) == 0.0
    req = min_required_equity(3000, 3.0, 100, 500)
    assert req == pytest.approx(3000 * 100 * 0.01 / 500 + 3.0)


def test_sessions(cfg):
    idx = pd.DatetimeIndex(["2025-03-07 21:30", "2025-03-07 20:00", "2025-03-09 22:30", "2025-03-10 08:00"], tz="UTC")
    s = session_state(idx, cfg)
    assert s["entry_blocked"].tolist() == [True, False, True, False]
    assert s["must_flat"].iloc[0] and s["session"].iloc[3] == "london"


def test_news_phases(cfg):
    ev = pd.DatetimeIndex(["2025-03-07 13:30"], tz="UTC")
    # decision at bar close = index + 1 min
    idx = pd.DatetimeIndex(["2025-03-07 13:14", "2025-03-07 13:27", "2025-03-07 13:40", "2025-03-07 14:00"], tz="UTC")
    p = news_phase(idx, ev, cfg)["news_phase"].tolist()
    assert p == ["pre", "event", "post", "none"]


def test_backtest_runs_and_respects_limits(feats, cfg):
    sig = compute_signals(feats, cfg)
    res = run_backtest(feats, sig, cfg)
    tr = res["trades"]
    m = res["metrics"]
    assert np.isfinite(m["final_equity"])
    if len(tr):
        assert tr.groupby(tr.open_time.dt.floor("D")).size().max() <= cfg.risk.max_daily_trades
        assert tr.hold_min.max() <= cfg.strategy.time_stop_minutes
        assert (tr.lots >= 0.01).all()
        # never more than max_concurrent overlapping
        ev = pd.concat([pd.Series(1, tr.open_time), pd.Series(-1, tr.close_time + pd.Timedelta(minutes=1))]).sort_index()
        assert ev.cumsum().max() <= cfg.risk.max_concurrent
        # no open position after Friday 21:00
        fri = tr.close_time[(tr.close_time.dt.dayofweek == 4) & (tr.close_time.dt.hour >= 21)]
        assert (fri.dt.hour * 60 + fri.dt.minute <= 21 * 60).all()


def test_baselines(feats, cfg):
    sig = compute_signals(feats, cfg)
    res = run_with_baselines(feats, sig, cfg)
    tab = summary_table(res)
    assert set(tab.index) == {"strategy", "ema_cross", "random", "buy_and_hold"}


def test_walk_forward(m1):
    idx = pd.date_range("2022-01-01", "2025-12-31", freq="D", tz="UTC")
    folds = list(walk_forward_windows(idx))
    ho_start, _ = holdout_range(idx)
    assert folds and all(te_end <= ho_start for *_, te_end in folds)
    assert all(tr_end == te_start for _, tr_end, te_start, _ in folds)


def test_store_roundtrip(tmp_path, m1):
    st = Store(tmp_path / "x.db")
    st.save_bars("XAUUSD", "M1", m1.iloc[:500])
    back = st.load_bars("XAUUSD", "M1")
    assert len(back) == 500 and back.index[0] == m1.index[0]


def test_tick_microstructure():
    idx = pd.date_range("2025-03-03 08:00", periods=120, freq="1s", tz="UTC")
    bid = 2900 + np.cumsum(np.r_[0, np.ones(119) * 0.01])
    t = pd.DataFrame({"bid": bid, "ask": bid + 0.1}, index=idx)
    mic = tick_microstructure(t)
    assert mic["trade_dir_ratio"].iloc[-1] == 1.0 and mic["ofi"].iloc[0] > 0


def test_empty_sqlite_table_does_not_poison_dtypes(tmp_path, cfg):
    """Regression (v2.0.4 crash): empty SQLite query → object columns → concat → 'unorderable types' in obv()."""
    st = Store(tmp_path / "x.db")
    m = synthetic_m1(days=4)
    st.save_bars("XAUUSD.s", "M1", m)
    df = st.load_bars("XAUUSD.s", "M1")
    extra = st.load_bars("XAUUSD", "M1")  # nothing imported under the plain name
    assert len(extra) == 0 and all(t == np.float64 for t in extra.dtypes)
    merged = pd.concat([extra[~extra.index.isin(df.index)], df]).sort_index()
    assert all(t == np.float64 for t in merged.dtypes)
    f = build_features(merged, cfg)
    sig = compute_signals(f, cfg)
    assert np.isfinite(f["obv_slope"].iloc[-1]) and set(sig["rule_signal"].unique()) <= {-1, 0, 1}


def test_ensure_model_installs_pretrained(tmp_path, monkeypatch):
    import main
    (tmp_path / "assets" / "pretrained").mkdir(parents=True)
    for n in ("model.txt", "model.json", "m15.txt", "m15.json"):
        (tmp_path / "assets" / "pretrained" / n).write_text("x")
    monkeypatch.setattr(main, "APP", tmp_path)
    monkeypatch.setattr(main, "DATA", tmp_path / "data")
    assert main.ensure_model() and (tmp_path / "data" / "model" / "m15.txt").exists()
    (tmp_path / "data" / "model" / "model.txt").write_text("mine")
    assert main.ensure_model() and (tmp_path / "data" / "model" / "model.txt").read_text() == "mine"  # never overwrites


def test_turbo_both_sells_breakdowns_without_lookahead():
    import numpy as np, pandas as pd
    from strategy import turbo_signal
    idx = pd.date_range("2026-01-05", periods=60 * 24 * 6, freq="1min", tz="UTC")
    px = 4000 - np.arange(len(idx)) * 0.02 + np.sin(np.arange(len(idx)) / 200) * 3   # steady downtrend
    f = pd.DataFrame({"open": px, "high": px + 0.3, "low": px - 0.3, "close": px}, index=idx)
    long_only = turbo_signal(f)
    both = turbo_signal(f, direction="both")
    assert (long_only == -1).sum() == 0 and (both == -1).sum() > 0
    cut = len(idx) // 2   # appending future bars must not change past signals
    assert turbo_signal(f.iloc[:cut], direction="both").equals(both.iloc[:cut])


def test_confidence_sizing_scales_risk_in_perfect_only():
    from config import load_config
    from risk import risk_pct_for
    t = load_config(ROOT / "config.yaml", use_tuned=False, overrides={"strategy": {"mode": "turbo"}})
    base = t.risk.risk_per_trade_pct
    assert risk_pct_for(t, 0.56) == base * 1.5 and risk_pct_for(t, 0.44) == base * 0.5
    assert risk_pct_for(t, None) == base and risk_pct_for(t, float("nan")) == base
    s = load_config(ROOT / "config.yaml", use_tuned=False, overrides={"strategy": {"mode": "scalp"}})
    assert risk_pct_for(s, 0.9) == s.risk.risk_per_trade_pct


def test_h4_regime_blocks_buys_in_a_bearish_4h_market():
    import numpy as np, pandas as pd
    from config import load_config
    from strategy import compute_signals, closed_trend
    idx = pd.date_range("2026-01-05", periods=60 * 24 * 30, freq="1min", tz="UTC")
    x = np.arange(len(idx))
    px = 4000 + np.where(x < len(x) // 2, x * 0.01, (len(x) // 2) * 0.01 - (x - len(x) // 2) * 0.01) \
        + np.sin(x / 90) * 4 + np.sin(x / 700) * 9
    from features import build_features
    m1 = pd.DataFrame({"open": px, "high": px + 0.4, "low": px - 0.4, "close": px, "tick_volume": 1.0, "spread": 0.32},
                      index=idx)
    base = {"strategy": {"mode": "turbo", "turbo_lookbacks": [12, 24, 48]}}
    f = build_features(m1, load_config(ROOT / "config.yaml", use_tuned=False, overrides=base))
    plain = compute_signals(f, load_config(ROOT / "config.yaml", use_tuned=False, overrides=base))["rule_signal"]
    base["strategy"]["turbo_regime"] = "h4"
    h4 = compute_signals(f, load_config(ROOT / "config.yaml", use_tuned=False, overrides=base))["rule_signal"]
    reg = closed_trend(f, 240, 20, 50)
    assert (h4 != 0).sum() > 0 and (h4 != 0).sum() <= (plain != 0).sum()
    assert (reg[h4.to_numpy() > 0] > 0).all()            # every buy happens in a bullish H4 market
    f2 = f.copy(); f2["h4_regime"] = -1.0                 # live column: bearish → no buys at all
    assert (compute_signals(f2, load_config(ROOT / "config.yaml", use_tuned=False, overrides=base))["rule_signal"] > 0).sum() == 0
