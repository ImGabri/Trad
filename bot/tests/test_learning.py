import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from data import Store  # noqa: E402
from learning import OUTCOME_FEATURES, CostModel, OutcomeModel  # noqa: E402


def samples(n, informative, seed=0):
    rng = np.random.default_rng(seed)
    X = {k: rng.normal(size=n) for k in OUTCOME_FEATURES}
    # wins depend on p_side only when informative
    p = 1 / (1 + np.exp(-3 * X["p_side"])) if informative else np.full(n, 0.45)
    win = rng.random(n) < p
    df = pd.DataFrame(X)
    df["win"] = win.astype(int)
    df["r"] = np.where(win, 0.8, -1.0)
    df["weight"] = 1.0
    df["source"] = "sim"
    df["time"] = pd.date_range("2024-01-01", periods=n, freq="h").astype(str)
    return df


def test_gate_turns_on_only_when_it_helps_out_of_sample():
    on = OutcomeModel.build(samples(2000, True), say=lambda *a: None)
    assert on.enabled and on.meta["val_r_kept"] > on.meta["val_r_all"]
    allowed_good, _ = on.allows({k: (3.0 if k == "p_side" else 0.0) for k in OUTCOME_FEATURES})
    allowed_bad, _ = on.allows({k: (-3.0 if k == "p_side" else 0.0) for k in OUTCOME_FEATURES})
    assert allowed_good and not allowed_bad
    off = OutcomeModel.build(samples(2000, False), say=lambda *a: None)
    assert not off.enabled and off.allows({k: 0.0 for k in OUTCOME_FEATURES})[0]


def test_outcome_model_roundtrip(tmp_path):
    m = OutcomeModel.build(samples(600, True), say=lambda *a: None)
    m.save(tmp_path)
    m2 = OutcomeModel.load(tmp_path)
    assert m2.enabled == m.enabled and abs(m2.threshold - m.threshold) < 1e-12


def test_cost_model_learns_spread_profile_and_slippage(tmp_path):
    st = Store(tmp_path / "x.db")
    t = pd.date_range("2026-11-02", periods=60_000, freq="10s", tz="UTC")
    bid = np.full(len(t), 4000.0)
    spread = np.where(t.hour == 22, 0.80, 0.32)  # rollover widening
    st.log_spreads("XAUUSD.s", pd.DataFrame({"bid": bid, "ask": bid + spread}, index=t))
    for k in range(25):
        st.record_trade(ticket=k, symbol="XAUUSD.s", side="BUY", lots=0.01, slippage=0.03)
    c = CostModel.learn(st)
    assert c.spread_how is not None and abs(c.slippage_usd - 0.03) < 1e-9
    hw = pd.DatetimeIndex(["2026-11-02 22:30", "2026-11-02 10:00"], tz="UTC")
    s = c.spread_for(hw, 0.32)
    assert abs(s[0] - 0.80) < 1e-9 and abs(s[1] - 0.32) < 1e-9


def test_trade_memory_learns_to_skip_losing_situations(tmp_path):
    from memory import TradeMemory, situation
    m = TradeMemory(tmp_path / "memory.json", min_n=10, k=5, explore=4)
    bad = situation("hyper", 1, 3, 2.0, 1.0)      # BUY · asia · wild
    good = situation("hyper", -1, 14, 1.0, 1.0)   # SELL · overlap · normal
    assert bad == "hyper|BUY|asia|wild" and good == "hyper|SELL|overlap|normal"
    for _ in range(12):
        m.record(bad, -0.5)
        m.record(good, +0.4)
    looks = [m.check(bad)[0] for _ in range(8)]
    assert looks.count(True) == 2                 # blocked, but re-tested every 4th time
    assert m.check(good)[0]
    m2 = TradeMemory(tmp_path / "memory.json", min_n=10, k=5)   # survives a restart
    assert m2.b[bad]["n"] == 12 and m2.summary("hyper")["blocked"] == 1
