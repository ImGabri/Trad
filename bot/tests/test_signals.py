"""📡 Telegram signal reader: parsing, the zone-trade rule, grading, and live trading of a posted signal."""
import importlib
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT)); sys.path.insert(0, str(ROOT / "tests"))

from synthetic import synthetic_m1  # noqa: E402
import fake_mt5  # noqa: E402


def test_parse_zone_and_order_posts():
    from signals import parse_post
    z = parse_post("c", 1, "2026-11-16T08:25:47+00:00", "📊LONDON ANALYSIS: XAUUSD\n\n🔴SELL ZONE: 3889.50 - 3901.29 level\n\n"
                   "Once tapped look for price to create a bearish confirmation … SELLS towards the 3820 level.")
    assert len(z) == 1 and z[0].side == -1 and (z[0].zone_lo, z[0].zone_hi, z[0].target) == (3889.5, 3901.29, 3820.0)
    a = parse_post("c", 2, "2026-11-20T07:40:30+00:00", "Aggressive BUY zone: 3542.56 - 3533.51 Level")
    assert a[0].side == 1 and a[0].aggressive and a[0].zone_lo == 3533.51
    o = parse_post("c", 3, "2026-01-01T10:00:00+00:00", "XAUUSD BUY NOW 2350-2346\nSL 2340\nTP1 2355\nTP2 2362")
    assert o[0].kind == "order" and o[0].sl == 2340 and o[0].tps == [2355, 2362]
    assert parse_post("c", 4, "2026-01-01T10:00:00+00:00", "Freakin INSANE MOVES CAUGHT LET'S GOOOO") == []


def test_zone_trade_taps_confirms_and_grades():
    from signals import Signal, zone_trade, grade, channel_stats
    idx = pd.date_range("2026-03-02 08:00", periods=200, freq="1min", tz="UTC")
    px = np.concatenate([np.linspace(3010, 2999, 30), np.linspace(2999, 3030, 170)])     # dips into the zone, then rallies
    o = np.r_[px[0], px[:-1]]
    bars = pd.DataFrame({"open": o, "high": np.maximum(o, px) + 0.2, "low": np.minimum(o, px) - 0.2, "close": px}, index=idx)
    sig = Signal("c", 1, "2026-03-02T08:00:00+00:00", 1, 2998.0, 3001.0, target=3020.0)
    r = zone_trade(sig, bars, spread=0.32)
    assert r is not None and r["reason"] == "tp" and r["usd"] > 0 and r["sl"] < 2998.0
    g = grade([sig, Signal("c", 2, "2026-03-02T08:00:00+00:00", -1, 3100.0, 3105.0)], bars)
    assert g.traded.tolist() == [True, False]           # a sell zone far above price is never tapped
    st = channel_stats(g, min_graded=1)["c"]
    assert st["traded"] == 1 and st["follow"]


def test_live_bot_trades_a_posted_order_signal(tmp_path, monkeypatch):
    m1 = synthetic_m1(days=40, seed=5)
    mod = fake_mt5.install(m1, start_bar=20000)
    for name in ("data", "execution", "live"):
        if name in sys.modules:
            importlib.reload(sys.modules[name])
    import signals
    price = float(m1.close.iloc[20000])
    post = f"XAUUSD BUY NOW {price + 0.5:.2f}-{price - 0.5:.2f}\nSL {price - 6:.2f}\nTP1 {price + 9:.2f}"
    now = pd.Timestamp.now(tz="UTC").isoformat()
    monkeypatch.setattr(signals, "fetch_channel", lambda ch, pages=1, **kw: [dict(id=7, time=now, text=post)])
    from config import load_config
    from features import build_features
    from ai_model import DirectionModel
    import live, ui
    ov = dict(ui.STYLES["signalmax5"]); ov["signals"] = {"enabled": True, "channels": ["testch"], "poll_s": 5}
    cfg = load_config(ROOT / "config.yaml", use_tuned=False, overrides=ov)
    cfg.broker.server_utc_offset_hours = 2
    DirectionModel.fit(build_features(m1.iloc[:20000], cfg), cfg, rounds=40).save(tmp_path / "data" / "model")
    n = {"i": 0}
    real_sleep = live.time.sleep
    def keep():
        n["i"] += 1
        if n["i"] == 2:              # deterministic: fetch the post now (the poller thread may be slower under load)
            with r._sig_lock:
                r.sbook.poll()
            r._sig_new = True
        return n["i"] < 30
    r = live.Runner(cfg, tmp_path, "demo", keep)
    r.events = pd.DatetimeIndex([], tz="UTC"); r.last_cal = 1e18
    live.time.sleep = lambda s: None
    try:
        r.run()
    finally:
        live.time.sleep = real_sleep
    sig_orders = [o for o in mod._fake.orders if "sig" in str(o.get("comment", ""))]
    assert sig_orders, "the posted signal was not traded"
    o = sig_orders[0]
    assert o["type"] == 0 and abs(o["sl"] - (price - 6)) < 1.5 and abs(o["tp"] - (price + 9)) < 1.5
    trades = pd.read_sql_query("SELECT reason_open FROM trades", r.store.conn)
    assert "signal:testch" in trades.reason_open.tolist()
