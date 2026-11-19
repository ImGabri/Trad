"""Equity take-profit: exit at +1 % of equity unless the AI expects more (then lock profit and ride)."""
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backtest import run_backtest  # noqa: E402
from config import load_config  # noqa: E402


def scenario(n=40):
    idx = pd.date_range("2025-03-04 09:00", periods=n, freq="1min", tz="UTC")  # Tuesday, London
    # flat at 3000 for 5 bars, then +$0.40 per bar
    close = np.r_[np.full(5, 3000.0), 3000.0 + 0.4 * np.arange(1, n - 4)]
    open_ = np.r_[close[0], close[:-1]]
    f = pd.DataFrame({"open": open_, "high": np.maximum(open_, close) + 0.05, "low": np.minimum(open_, close) - 0.05,
                      "close": close, "spread": 0.32, "spread_ratio": 1.0, "m5_atr": 4.0}, index=idx)
    sig = pd.DataFrame({"rule_signal": 0, "bias": 1, "session": "london", "entry_blocked": False, "must_flat": False,
                        "news_phase": "none", "event_id": -1}, index=idx)
    entry = pd.Series(0, index=idx)
    entry.iloc[4] = 1  # buy decided at bar 4 close, filled at bar 5 open
    return f, sig, entry


def cfg(**strategy):
    c = load_config(ROOT / "config.yaml", use_tuned=False, overrides={
        "backtest": {"initial_equity": 58.0, "slippage_pips": 0},
        "risk": {"min_lot_max_risk_pct": 10, "daily_loss_limit_pct": 50},
        "strategy": dict(dict(equity_tp_pct=1.0, equity_tp_hold_conf=0.55, equity_tp_lock_frac=0.5,
                              time_stop_minutes=60, trail_trigger_atr=99, tp_atr=50), **strategy)})
    return c


def test_exits_at_one_percent_without_ai():
    f, sig, entry = scenario()
    res = run_backtest(f, sig, cfg(), entry_signal=entry)
    tr = res["trades"]
    assert len(tr) == 1 and tr.reason.iloc[0] == "equity_tp"
    # 1 % of $58 = $0.58 on 0.01 lot (1 oz) → exit $0.58 above the entry (ask) price
    assert abs(tr.pnl.iloc[0] - 0.58) < 0.05


def test_ai_keeps_the_winner_running_then_exits_when_it_stops_agreeing():
    f, sig, _ = scenario()
    sig["rule_signal"] = 0
    sig.iloc[4, sig.columns.get_loc("rule_signal")] = 1
    ai_dir = pd.Series(1, index=f.index)
    ai_conf = pd.Series(0.9, index=f.index)
    ai_conf.iloc[20:] = 0.51  # AI loses conviction from bar 20
    res = run_backtest(f, sig, cfg(confidence_threshold=0.5), ai_dir=ai_dir, ai_conf=ai_conf)
    tr = res["trades"]
    assert len(tr) == 1 and tr.reason.iloc[0] == "equity_tp"
    assert tr.pnl.iloc[0] > 2.0, "held well past the 1 % level while the AI agreed"
    assert tr.close_time.iloc[0] == f.index[20], "left on the first bar the AI stopped agreeing"


def test_volatility_spike_does_not_latch_the_survival_stop():
    f, sig, entry = scenario(60)
    f["m5_atr"] = 4.0
    f.iloc[30:35, f.columns.get_loc("m5_atr")] = 40.0   # news spike: 10× normal volatility
    f["m5_atr_typ"] = 4.0                              # typical (24 h median) volatility unchanged
    entry.iloc[:] = 0
    entry.iloc[40] = 1                                 # a normal trade after the spike
    res = run_backtest(f, sig, cfg(), entry_signal=entry)
    assert res["stop_reason"] is None, "a spike alone must not trigger the survival hard stop"
    assert len(res["trades"]) == 1
