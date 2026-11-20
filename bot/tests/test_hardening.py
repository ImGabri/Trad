"""v3 bug-audit fixes: signal parsing / dedupe / grading horizon, MT5 hiccups, the time offset, dashboard input."""
import importlib
import math
import shutil
import sys
import time
from pathlib import Path
from types import SimpleNamespace as NS

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT)); sys.path.insert(0, str(ROOT / "tests"))

from synthetic import synthetic_m1  # noqa: E402
import fake_mt5  # noqa: E402


def _runner(tmp_path, style="signalmax5", sig_over=None, monkeypatch=None):
    """A live.Runner on the fake MT5, set up as run() would, without its loop (no model training, no network)."""
    m1 = synthetic_m1(days=12, seed=5)
    mod = fake_mt5.install(m1, start_bar=14000)
    for name in ("data", "execution", "live"):
        if name in sys.modules:
            importlib.reload(sys.modules[name])
    import signals
    monkeypatch.setattr(signals, "fetch_channel", lambda ch, pages=1, **kw: [])
    import live, ui
    from config import load_config
    ov = {k: dict(v) if isinstance(v, dict) else v for k, v in ui.STYLES[style].items()}
    ov["signals"] = dict(ov.get("signals", {}), **(sig_over or {"channels": ["c"]}))
    cfg = load_config(ROOT / "config.yaml", use_tuned=False, overrides=ov)
    cfg.broker.server_utc_offset_hours = 2
    (tmp_path / "data" / "model").mkdir(parents=True)
    for f in (ROOT / "assets" / "pretrained").iterdir():
        shutil.copy(f, tmp_path / "data" / "model" / f.name)
    r = live.Runner(cfg, tmp_path, "demo", keep_running=lambda: False)
    r.conn = sys.modules["data"].MT5Connection(cfg); r.conn.connect()
    r.gold = "XAUUSD"; r.info = mod.symbol_info("XAUUSD"); r.dev_points = 30; r.acct_usd = 1.0
    r.events = pd.DatetimeIndex([], tz="UTC"); r.last_cal = 1e18
    r.last_feat = pd.Series({"m5_atr": 2.0, "h4_regime": 0.0})
    r._sig_gate = dict(t=time.time() + 1e6, must_flat=False, closed=False, spread_avg=np.nan, atr=2.0)
    return r, mod


def test_parser_ignores_other_instruments_results_and_number_fragments():
    from signals import parse_post
    T = "2026-11-20T10:00:00+00:00"
    for text in ("ETHUSD BUY ZONE: 3850 - 3820\nTarget 3950", "US30 SELL ZONE 44100 - 44200",
                 "BTCUSD BUY NOW 98500\nSL 97000\nTP 101000\n\nJoin our gold VIP group",
                 "XAUJPY BUY NOW 590000\nSL 589000\nTP 595000", "XAUEUR BUY NOW 3600\nSL 3590\nTP 3620",
                 "GOLD BUY 3900 TP1 3910 hit ✅ SL moved to entry 3900",
                 "Close all gold buys now at 3912, SL 3900 TP 3925 cancelled",
                 "#DAX: Target Is Down! Short!\nTRADE: SHORT\nENTRY: 24,110.59\nSTOP LOSS: 24,230.67\nTAKE PROFIT: 23,697.96\n🌸GOLD/"):
        assert parse_post("c", 1, T, text) == [], text
    s = parse_post("c", 2, T, "XAUUSD SELL NOW 3950\nSL 3958\nTP 3940\n(silver looks weak too)")
    assert len(s) == 1 and (s[0].side, s[0].zone_lo, s[0].sl) == (-1, 3950.0, 3958.0)   # gold named first → a gold post
    z = parse_post("c", 3, T, "BUY ZONE: 3,880.50 - 3,885.20\nSELL ZONE: 3,990 - 3,995")
    assert [(x.side, x.zone_lo, x.zone_hi, x.n) for x in z] == [(1, 3880.5, 3885.2, 0), (-1, 3990.0, 3995.0, 1)]


def test_edited_post_is_updated_in_place_and_traded_once(tmp_path, monkeypatch):
    r, mod = _runner(tmp_path, monkeypatch=monkeypatch)
    import signals
    px = mod.symbol_info_tick("XAUUSD").ask
    t = (pd.Timestamp.now(tz="UTC") - pd.Timedelta(minutes=2)).isoformat()
    for entry in (px + 1.0, px - 1.5):            # the admin corrects the entry of post #555
        monkeypatch.setattr(signals, "fetch_channel", lambda ch, pages=1, _e=entry, **kw: [
            dict(id=555, time=t, text=f"XAUUSD BUY NOW {_e:.2f}\nSL {px - 6:.2f}\nTP {px + 12:.2f}")])
        r.sbook.poll()
        r._signal_step(immediate=True)
    assert len(r.sbook.signals) == 1 and abs(r.sbook.signals[0].zone_lo - (px - 1.5)) < 0.01
    assert len([o for o in mod._fake.orders if "sig" in str(o.get("comment", ""))]) == 1


def test_stale_order_posts_and_far_stops_are_never_traded(tmp_path, monkeypatch):
    r, mod = _runner(tmp_path, style="signalhyper", monkeypatch=monkeypatch)
    from signals import parse_post
    px = mod.symbol_info_tick("XAUUSD").ask
    now = pd.Timestamp.now(tz="UTC")
    r.sbook.signals = (parse_post("c", 1, (now - pd.Timedelta(minutes=40)).isoformat(),                  # 40 min old
                                  f"XAUUSD BUY NOW {px:.2f}\nSL {px - 6:.2f}\nTP {px + 12:.2f}")
                       + parse_post("c", 2, (now - pd.Timedelta(minutes=1)).isoformat(),                   # stop 4 % away
                                    f"XAUUSD BUY NOW {px:.2f}\nSL {px * 0.96:.2f}\nTP {px * 1.02:.2f}"))
    r._signal_step(immediate=True)
    assert not [o for o in mod._fake.orders if "sig" in str(o.get("comment", ""))]
    assert {"c:1:0:-", "c:2:0:-"} <= set(r.state["signals_done"])          # decided once, never re-tried


def test_poll_counts_new_signals_when_the_store_is_full(tmp_path, monkeypatch):
    import signals
    from signals import Signal, SignalBook
    now = pd.Timestamp.now(tz="UTC")
    monkeypatch.setattr(signals, "fetch_channel", lambda ch, pages=1, **kw: [
        dict(id=99999, time=now.isoformat(), text="XAUUSD BUY NOW 3900-3899\nSL 3893\nTP1 3912")])
    b = SignalBook(tmp_path / "s", ["chan"], 20, 100)
    b.signals = [Signal("old", i, (now - pd.Timedelta(days=10) + pd.Timedelta(minutes=i)).isoformat(), 1, 3000.0 + i,
                        3001.0 + i, sl=2990.0 + i, kind="order") for i in range(6000)]
    assert b.poll() == 1 and b.active(now + pd.Timedelta(seconds=1))[0].post_id == 99999
    assert len(b.signals) <= 5000 and b.poll() == 0                         # same post again: nothing new


def test_regrade_waits_until_every_lane_is_over(tmp_path):
    from signals import Signal, SignalBook
    t0 = pd.Timestamp("2026-03-03 08:00", tz="UTC")
    idx = pd.date_range(t0, periods=40 * 60, freq="1min")
    px = np.r_[np.full(9 * 60, 3000.0), np.linspace(3000, 3060, 31 * 60)]
    o = np.r_[px[0], px[:-1]]
    bars = pd.DataFrame({"open": o, "high": np.maximum(o, px) + .2, "low": np.minimum(o, px) - .2, "close": px}, index=idx)
    b = SignalBook(tmp_path / "s", ["c"], 20, 100, grade_kw=dict(max_away=5.0, hold_h=8.0, valid_h=4.0))
    b.graded = pd.DataFrame()
    b.lanes = ["max", "hyper"]
    b.signals = [Signal("c", 1, t0.isoformat(), 1, 2999.5, 3000.5, sl=2990.0, kind="order")]
    b.regrade(bars[bars.index <= t0 + pd.Timedelta(hours=9)], 0.32)
    assert b.graded.empty                                                   # the 24 h lane is not over yet
    b.regrade(bars, 0.32)
    assert len(b.graded) == 1 and b.graded["n"].iloc[0] == 0


def test_mt5_not_answering_keeps_the_trade_notes(tmp_path, monkeypatch):
    r, mod = _runner(tmp_path, monkeypatch=monkeypatch)
    import execution as ex
    fill = ex.open_market("XAUUSD", 1, 0.01, 8.0, 16.0, 30, "xau-v2 sig c")
    r._set_meta(fill.ticket, dict(atr=2.0, open_ts=time.time() - 2 * 3600, best=fill.price, side=1, kind="signal", conf=0,
                                  tp=fill.price + 16, sl=fill.price - 8, hold_min=480, channel="c", lots=0.01))
    real = ex.mt5.positions_get
    monkeypatch.setattr(ex.mt5, "positions_get", lambda symbol=None: None)  # one IPC hiccup
    r._manage_positions()
    monkeypatch.setattr(ex.mt5, "positions_get", real)
    r._manage_positions()
    m = r._pos_meta()[fill.ticket]
    assert m["kind"] == "signal" and m["hold_min"] == 480


def test_signal_trades_keep_the_channel_levels(tmp_path, monkeypatch):
    r, mod = _runner(tmp_path, style="signalhyper", monkeypatch=monkeypatch)
    import execution as ex
    fill = ex.open_market("XAUUSD", 1, 0.01, 8.0, 16.0, 30, "xau-v2 sig c")
    sl0 = mod._fake.positions[fill.ticket].sl
    r._set_meta(fill.ticket, dict(atr=0.5, open_ts=time.time(), best=fill.price - 50, side=1, kind="signal", conf=0,
                                  tp=fill.price + 16, sl=sl0, hold_min=1440, channel="c", lots=0.01))
    monkeypatch.setattr(r, "_phase_now", lambda: "pre")                  # news ahead + far in profit (trail trigger)
    r._manage_positions()
    assert mod._fake.positions[fill.ticket].sl == sl0


def test_refresh_offset_corrects_a_weekend_guess(monkeypatch):
    import data
    cfg = NS(broker=NS(server_utc_offset_hours="auto"))
    c = data.MT5Connection.__new__(data.MT5Connection)
    c.cfg, c.server_offset, c.generation, c._rates_cache = cfg, pd.Timedelta(hours=2).to_pytimedelta(), 0, {"x": 1}
    c._offset_provisional, c._offset_check = True, (0.0, None)
    clock = {"msc": 1_000}
    monkeypatch.setattr(data.mt5, "symbol_info_tick", lambda s: NS(time=int(time.time()) + 3 * 3600, time_msc=clock["msc"]),
                        raising=False)
    assert c.refresh_offset("XAUUSD") is False                              # first look: remembers the tick clock
    c._offset_check = (c._offset_check[0] - 61, c._offset_check[1])
    assert c.refresh_offset("XAUUSD") is False                              # tick clock did not move: market not live
    clock["msc"] += 5_000
    c._offset_check = (c._offset_check[0] - 61, c._offset_check[1])
    assert c.refresh_offset("XAUUSD") is True and c.server_offset.total_seconds() == 3 * 3600
    assert c.generation == 1 and c._rates_cache == {}


def test_rater_keeps_live_grades_across_restarts(tmp_path):
    from signals import RATER_COLS, SignalRater
    seed = ROOT / "assets" / "signal_rater_train.csv"
    n_seed = len(pd.read_csv(seed))
    live = tmp_path / "rated_live.csv"
    rows = pd.DataFrame([dict(time=f"2026-11-{d:02d}T10:00:00+00:00", ch="newch", lane="max", usd=2.0,
                              **{k: 0.5 for k in RATER_COLS}) for d in range(1, 11)])
    rows.to_csv(live, index=False)
    r = SignalRater(seed, live)
    assert len(r.rows) == n_seed + 10 and "max" in r.models
    assert math.isclose(r.ch_recent("newch", "max"), 2.0)
    (tmp_path / "bad.csv").write_text("")                                   # empty / damaged file → just skipped
    assert len(SignalRater(seed, tmp_path / "bad.csv").rows) == n_seed


def test_dashboard_input_is_validated(tmp_path, monkeypatch):
    import ui
    assert ui._json_serialize({"a": float("nan"), "b": [np.float64("inf"), 1.5]}) == {"a": None, "b": [None, 1.5]}
    assert ui._num("", float, 0, 10) is None and ui._num("nan") is None and ui._num("3,5", float, 0, 10) == 3.5
    assert ui._num("999", int, 1, 500) == 500
    monkeypatch.setattr(ui, "APP", tmp_path)
    (tmp_path / ".env").write_text("MT5_LOGIN=1\nMT5_PASSWORD=secret\nMT5_SERVER=PUPrime-Demo\n")
    ui.action("mt5", {"login": "2", "password": "", "server": ""})
    env = (tmp_path / ".env").read_text()
    assert "MT5_PASSWORD=secret" in env and "MT5_LOGIN=2" in env            # an empty box never erases the password
    monkeypatch.setattr(ui.JOB, "start", lambda *a, **k: False)
    assert ui.action("reset", {}).startswith("Busy")


def test_retrain_rules_never_touch_preset_strategies(tmp_path):
    import yaml
    import config
    import ui
    (tmp_path / "data").mkdir()
    shutil.copy(ROOT / "config.yaml", tmp_path / "config.yaml")
    perfect_exits = {"strategy": {"tp_atr": 40.0, "sl_atr": 3.0, "time_stop_minutes": 900, "trail_trigger_atr": 99.0},
                     "risk": {"max_concurrent": 1, "max_daily_trades": 300}, "analyst": {"veto_conf": 0.0},
                     "sessions": {"asia_enabled": True}}   # what a retrain under Signal AI ×2 saved before v3.0.1
    scalp_tuning = {"strategy": {"tp_atr": 1.25, "sl_atr": 1.0, "time_stop_minutes": 15}, "_tuning": {"mode": "scalp"}}

    def eff(style, tuned):
        (tmp_path / "data" / "user.yaml").write_text(yaml.safe_dump(ui.STYLES[style]))
        (tmp_path / "data" / "tuned.yaml").write_text(yaml.safe_dump(tuned))
        config._YAML_CACHE.clear()
        c = config.load_config(tmp_path / "config.yaml")
        return c.strategy.tp_atr, c.strategy.time_stop_minutes, c.risk.max_concurrent
    def plain(style):
        (tmp_path / "data" / "tuned.yaml").unlink(missing_ok=True)
        (tmp_path / "data" / "user.yaml").write_text(yaml.safe_dump(ui.STYLES[style]))
        config._YAML_CACHE.clear()
        c = config.load_config(tmp_path / "config.yaml")
        return c.strategy.tp_atr, c.strategy.time_stop_minutes, c.risk.max_concurrent
    for style in ("signalai2x", "signalai", "perfect", "hypermax5", "signalhyper", "active", "basket"):
        assert eff(style, perfect_exits) == plain(style), style      # a breakout-made tuning applies nowhere
    assert eff("active", scalp_tuning)[:2] == (1.25, 15)                 # a real scalping tuning: scalping styles only
    assert eff("signalai", scalp_tuning) == plain("signalai")


def test_status_pulse_keeps_the_heartbeat_fresh(tmp_path):
    import threading
    from monitor import Status, app_version
    st = Status(tmp_path / "status.json")
    st.update(state="starting")
    first = Status.read(tmp_path / "status.json")
    assert first["version"] == app_version()
    stop = threading.Event()
    th = threading.Thread(target=st.pulse, args=(stop,), kwargs=dict(every=0.05), daemon=True)
    th.start()
    time.sleep(0.3)
    stop.set(); th.join(1)
    beat = Status.read(tmp_path / "status.json")
    assert beat["heartbeat"] > first["heartbeat"] and beat["state"] == "starting" and not th.is_alive()


def test_new_dashboard_takes_over_from_an_older_one(tmp_path, monkeypatch):
    import json as _json
    import socket
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    import ui
    monkeypatch.setattr(ui, "HANDOVER", tmp_path / "dashboard.json")

    def free_port():
        with socket.socket() as so:
            so.bind(("127.0.0.1", 0))
            return so.getsockname()[1]
    # 1) an older dashboard (no handover support) keeps its port: the new one serves on the next free port
    class Old(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200); self.end_headers(); self.wfile.write(b'{"app":"xau-scalper"}')
        def log_message(self, *a):
            pass
    p = free_port()
    old = ThreadingHTTPServer(("127.0.0.1", p), Old)
    threading.Thread(target=old.serve_forever, daemon=True).start()
    try:
        port, srv = ui._claim_port(first=p, count=4)
        assert port != p and srv is not None
        srv.server_close()
    finally:
        old.shutdown(); old.server_close()
    # 2) a dashboard of another version that supports the handover closes and the new one takes the same port
    p = free_port()
    monkeypatch.setattr(ui.Handler, "handover", "s3cret")
    monkeypatch.setattr(ui, "PORT", p)                                   # Host-header check of the serving dashboard
    prev = ui._Server(("127.0.0.1", p), ui.Handler)
    threading.Thread(target=prev.serve_forever, daemon=True).start()
    monkeypatch.setattr(ui, "_quit", lambda: (prev.shutdown(), prev.server_close()))
    (tmp_path / "dashboard.json").write_text(_json.dumps(dict(port=p, secret="s3cret")))
    monkeypatch.setattr(ui, "_this_install", lambda d: False)            # it answers as a different version
    port, srv = ui._claim_port(first=p, count=2)
    try:
        assert port == p and srv is not None
    finally:
        srv.server_close()
    # 3) the same install already serving: just reuse it
    monkeypatch.setattr(ui, "_this_install", lambda d: True)
    again = ui._Server(("127.0.0.1", p), ui.Handler)
    threading.Thread(target=again.serve_forever, daemon=True).start()
    try:
        assert ui._claim_port(first=p, count=2) == (p, None)
    finally:
        again.shutdown(); again.server_close()
