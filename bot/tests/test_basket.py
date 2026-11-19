"""🧺 Basket mode: candle-direction signal and the live runner's legs / shared stop / shared target."""
import importlib
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT)); sys.path.insert(0, str(ROOT / "tests"))

from synthetic import synthetic_m1  # noqa: E402
import fake_mt5  # noqa: E402


def test_basket_signal_follows_last_candles():
    from config import load_config
    from strategy import basket_signal
    idx = pd.date_range("2026-11-17 08:00", periods=60, freq="1min", tz="UTC")
    o = np.full(60, 4000.0); c = o.copy()
    c[:30] = o[:30] + 1.0      # 30 rising minutes → six rising M5 candles
    c[30:] = o[30:] - 1.0      # then falling
    m1 = pd.DataFrame({"open": o, "high": np.maximum(o, c), "low": np.minimum(o, c), "close": c, "tick_volume": 1.0,
                       "spread": 0.32}, index=idx)
    cfg = load_config(ROOT / "config.yaml", use_tuned=False, overrides={"basket": {"candles_tf": "m5", "candles": 3}})
    s = basket_signal(m1, cfg)
    assert s[(s.index.minute % 5) != 4].eq(0).all()          # M5 signals only at M5 closes
    assert s.loc["2026-11-17 08:14"] == 1 and s.loc["2026-11-17 08:54"] == -1
    cfg_m1 = load_config(ROOT / "config.yaml", use_tuned=False, overrides={"basket": {"candles_tf": "m1", "candles": 3, "follow": False}})
    s1 = basket_signal(m1, cfg_m1)
    assert s1.iloc[5] == -1 and s1.iloc[40] == 1             # fade mode flips the direction


def test_live_basket_legs_share_stop_and_target(tmp_path):
    m1 = synthetic_m1(days=40, seed=5)
    mod = fake_mt5.install(m1, start_bar=20000)
    for name in ("data", "execution", "live"):
        if name in sys.modules:
            importlib.reload(sys.modules[name])
    from config import load_config
    from features import build_features
    from ai_model import DirectionModel
    import live
    cfg = load_config(ROOT / "config.yaml", use_tuned=False, overrides={
        "strategy": {"mode": "basket"}, "sessions": {"asia_enabled": True, "no_entry_windows": []},
        "basket": {"candles_tf": "m1", "candles": 2, "step_atr": 0.2, "max_entries": 3, "tp_atr": 1.0, "sl_atr": 3.0,
                   "hold_minutes": 600, "equity_per_001": 50}, "risk": {"max_daily_trades": 500}})
    cfg.broker.server_utc_offset_hours = 2
    DirectionModel.fit(build_features(m1.iloc[:20000], cfg), cfg, rounds=40).save(tmp_path / "data" / "model")
    n = {"i": 0}
    def keep():
        n["i"] += 1
        return n["i"] < 700
    r = live.Runner(cfg, tmp_path, "demo", keep)
    r.events = pd.DatetimeIndex([], tz="UTC"); r.last_cal = 1e18
    live.time.sleep = lambda s: None
    r.run()
    fake = mod._fake
    opens = [o for o in fake.orders if o["action"] == 1 and "position" not in o]
    assert opens, "basket never opened"
    # never more legs open at once than max_entries, and all open legs share one stop and one target
    assert len(fake.positions) <= 3
    if len(fake.positions) > 1:
        legs = list(fake.positions.values())
        assert len({round(p.sl, 2) for p in legs}) == 1 and len({round(p.tp, 2) for p in legs}) == 1
    sl_moves = [o for o in fake.orders if o["action"] == 6]
    assert any("tp" in o for o in sl_moves) or len(opens) == len({o.get("comment") for o in opens})
    trades = pd.read_sql_query("SELECT * FROM trades", r.store.conn)
    assert (trades.reason_open == "basket").all() and trades.pnl.notna().sum() >= 1
