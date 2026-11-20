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
    T = "2026-01-01T10:00:00+00:00"
    e = parse_post("c", 5, T, "XAUUSD SELL NOW 4030\n\nTP  4027\nTP  4024\n\nSL   4032")[0]
    assert (e.side, e.zone_lo, e.sl, e.tps) == (-1, 4030.0, 4032.0, [4027.0, 4024.0])
    f = parse_post("c", 6, T, "Gold Next Buy\nTrade $3570/65 Limited\nStope Loss $3557\nTake Profit $3580\nTake Profit $3590")[0]
    assert (f.side, f.zone_lo, f.zone_hi, f.sl, f.tps) == (1, 3565.0, 3570.0, 3557.0, [3580.0, 3590.0])
    g = parse_post("c", 7, T, "GOLD Buyers In Panic! SELL!\nThe instrument tests an important psychological level 3588.0\n"
                   "Target - 3542.2\nRecommended Stop Loss - 3617.8")[0]
    assert (g.side, g.zone_lo, g.sl, g.target) == (-1, 3588.0, 3617.8, 3542.2)
    h = parse_post("c", 8, T, "ENTER: #XAUUSD long trade\nCURRENT PRICE: 3,687.738\nSTOP LOSS: 3,663.379\nTAKE PROFIT: 3,724.442")[0]
    assert (h.side, h.zone_lo, h.sl, h.target) == (1, 3687.738, 3663.379, 3724.442)
    assert parse_post("c", 9, T, "#XAUUSD SELL\n\nTP1 HIT 30+ PIPS PROFIT DONE") == []
    assert parse_post("c", 10, T, "EURUSD Buy! CURRENT PRICE: 1.09606 STOP LOSS: 1.09528 TAKE PROFIT: 1.09722") == []


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


def test_signal_ai_rates_each_lane_and_trades_the_approved_ones(tmp_path, monkeypatch):
    m1 = synthetic_m1(days=40, seed=5)
    mod = fake_mt5.install(m1, start_bar=20000)
    for name in ("data", "execution", "live"):
        if name in sys.modules:
            importlib.reload(sys.modules[name])
    import signals
    price = float(m1.close.iloc[20000])
    post = f"XAUUSD SELL NOW {price + 0.5:.2f}-{price - 0.5:.2f}\nSL {price + 6:.2f}\nTP1 {price - 9:.2f}"
    now = pd.Timestamp.now(tz="UTC").isoformat()
    monkeypatch.setattr(signals, "fetch_channel", lambda ch, pages=1, **kw: [dict(id=8, time=now, text=post)])
    rated = []
    def fake_predict(self, lane, feats, channel):      # approve the hyper lane only, with a high rating
        rated.append(lane)
        assert set(signals.RATER_COLS) - {"ch_recent"} <= set(feats)
        return 1.2 if lane == "hyper" else -0.4
    monkeypatch.setattr(signals.SignalRater, "predict", fake_predict)
    from config import load_config
    from features import build_features
    from ai_model import DirectionModel
    import live, ui
    ov = dict(ui.STYLES["signalai2x"]); ov["signals"] = dict(ov["signals"], channels=["testch"], poll_s=5)
    cfg = load_config(ROOT / "config.yaml", use_tuned=False, overrides=ov)
    cfg.broker.server_utc_offset_hours = 2
    DirectionModel.fit(build_features(m1.iloc[:20000], cfg), cfg, rounds=40).save(tmp_path / "data" / "model")
    import threading
    n = {"i": 0, "r": None}
    main = threading.main_thread()
    def keep():                              # also called by the poller thread → count only the trading loop
        if threading.current_thread() is not main or n["r"] is None:
            return n["i"] < 40                # the poller thread stops together with the trading loop
        n["i"] += 1
        if n["i"] == 3:                     # after a first closed bar (rating context) fetch the post
            with n["r"]._sig_lock:
                n["r"].sbook.poll()
            n["r"]._sig_new = True
        return n["i"] < 40
    r = live.Runner(cfg, tmp_path, "demo", keep)
    n["r"] = r
    r.events = pd.DatetimeIndex([], tz="UTC"); r.last_cal = 1e18
    real_sleep = live.time.sleep; live.time.sleep = lambda s: None
    try:
        r.run()
    finally:
        live.time.sleep = real_sleep
    assert {"max", "hyper"} <= set(rated)
    sig_orders = [o for o in mod._fake.orders if "sig" in str(o.get("comment", ""))]
    assert len(sig_orders) == 1 and "sigh" in sig_orders[0]["comment"] and sig_orders[0]["type"] == 1
    trades = pd.read_sql_query("SELECT reason_open, confidence FROM trades", r.store.conn)
    assert "signal:testch:hyper" in trades.reason_open.tolist()
