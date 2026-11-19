"""Drive the live Runner against the fake MT5 for a few hundred bars."""
import importlib
import shutil
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT)); sys.path.insert(0, str(ROOT / "tests"))

from synthetic import synthetic_m1  # noqa: E402
import fake_mt5  # noqa: E402

LOOSE = {"strategy": {"mode": "scalp", "m15_adx_min": 0, "level_tolerance_atr": 50, "m1_trigger_bars": 1,
                      "require_rising_volume": False, "confidence_threshold": 0.5},
         "sessions": {"asia_min_confidence": 0.5}}


def test_runner_trades_and_kill(tmp_path):
    m1 = synthetic_m1(days=40, seed=3)
    mod = fake_mt5.install(m1, start_bar=20000)
    for name in ("data", "execution", "live"):
        if name in sys.modules:
            importlib.reload(sys.modules[name])
    from config import load_config
    from features import build_features
    from ai_model import DirectionModel
    import live
    cfg = load_config(ROOT / "config.yaml", overrides=LOOSE)
    cfg.broker.server_utc_offset_hours = 2
    f = build_features(m1.iloc[:20000], cfg)
    model = DirectionModel.fit(f, cfg, rounds=60)
    model.meta["backtest_win_rate"] = 45
    model.save(tmp_path / "data" / "model")
    from analyst import M15Analyst, m15_frame
    M15Analyst.fit(m15_frame(m1.iloc[:20000])).save(tmp_path / "data" / "model")

    n = {"i": 0}
    def keep():
        n["i"] += 1
        if n["i"] == 400:
            (tmp_path / "data" / "STOP").touch()
        return n["i"] < 500
    r = live.Runner(cfg, tmp_path, "demo", keep)
    r.events = pd.DatetimeIndex([], tz="UTC"); r.last_cal = 1e18  # no network in tests
    live.time.sleep = lambda s: None
    r.run()
    fake = mod._fake
    opened = [o for o in fake.orders if o["action"] == 1 and "position" not in o]
    assert opened, "runner never entered a trade"
    assert all(o["sl"] and o["tp"] for o in opened), "every order must carry SL and TP"
    assert not fake.positions, "kill switch must flatten everything"
    trades = pd.read_sql_query("SELECT * FROM trades", r.store.conn)
    assert len(trades) == len(opened) and trades.pnl.notna().sum() >= 1
    assert (tmp_path / "data" / "status.json").exists()
    assert r.risk.hard_stop  # kill latches the halt
