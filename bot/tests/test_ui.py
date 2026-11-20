import json
import socket
import sys
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import ui  # noqa: E402


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def call(port, path, body=None, token=None, host=None):
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", data=None if body is None else json.dumps(body).encode(),
                                 method="GET" if body is None else "POST")
    if token:
        req.add_header("X-Token", token)
    if host:
        req.add_header("Host", host)
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def test_dashboard_server(tmp_path, monkeypatch):
    port = free_port()
    monkeypatch.setattr(ui, "PORT", port)
    monkeypatch.setattr(ui, "DATA", tmp_path)
    ui.Handler.token = "tok123"
    srv = ThreadingHTTPServer(("127.0.0.1", port), ui.Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        code, html = call(port, "/")
        assert code == 200 and b"tok123" in html and b"__TOKEN__" not in html
        code, st = call(port, "/api/state")
        st = json.loads(st)
        assert code == 200 and st["background"]["background"] == "off" and "settings" in st
        assert st["settings"]["equity_tp_pct"] == 0.0 and st["style"] == "turbo"  # Turbo is the default
        # actions need the session token …
        assert call(port, "/api/stop", {}, token=None)[0] == 403
        assert call(port, "/api/stop", {}, token="wrong")[0] == 403
        # … and a local Host header (DNS-rebinding protection)
        assert call(port, "/api/state", host="evil.example:80")[0] == 403
        code, msg = call(port, "/api/settings", {"equity_tp_pct": 1.5, "max_daily_trades": 9999}, token="tok123")
        assert code == 200 and b"Saved" in msg
        saved = (tmp_path / "user.yaml").read_text()
        assert "equity_tp_pct: 1.5" in saved and "max_daily_trades: 500" in saved  # clamped
    finally:
        srv.shutdown()


def test_style_presets_are_complete_and_detected(tmp_path, monkeypatch):
    import yaml
    from config import load_config
    monkeypatch.setattr(ui, "DATA", tmp_path)
    ui._write_user({"risk": {"risk_per_trade_pct": 9}})                 # an old manual tweak …
    ui._write_user(ui.STYLES["perfect"], replace=True)                   # … is cleared by choosing a style
    assert "risk_per_trade_pct" not in (tmp_path / "user.yaml").read_text()
    for style, mode, tf in (("extreme", "basket", "m1"), ("hyper", "basket", "m1"), ("basket", "basket", "m5"), ("perfect", "turbo", None), ("both", "turbo", None), ("hyperperfect", "turbo", None), ("hyperh4", "turbo", None), ("hypermax", "turbo", None), ("hypermax5", "turbo", None)):
        ui._write_user(ui.STYLES[style], replace=True)
        (tmp_path / "config.yaml").write_text((ROOT / "config.yaml").read_text())
        (tmp_path / "data").mkdir(exist_ok=True)
        (tmp_path / "data" / "user.yaml").write_text((tmp_path / "user.yaml").read_text())
        cfg = load_config(tmp_path / "config.yaml", use_tuned=False)
        assert cfg.strategy.mode == mode
        if tf:
            assert cfg.basket.candles_tf == tf and cfg.risk.max_daily_trades >= 100000
        else:
            assert cfg.strategy.sl_atr == 3.0 and cfg.strategy.tp_atr == 40.0 and not cfg.strategy.ai_gate
        assert bool(getattr(cfg.basket, "independent", False)) == (style == "hyper")
        assert str(getattr(cfg.strategy, "turbo_direction", "long")) == ("both" if style == "both" else "long") or tf
        if style == "hyperperfect":
            assert list(cfg.strategy.turbo_lookbacks) == [12, 24, 48] and cfg.risk.max_concurrent == 3
        if style == "hyperh4":
            assert cfg.strategy.turbo_regime == "h4" and cfg.risk.max_concurrent == 3
        if style == "hypermax5":
            assert cfg.risk.max_concurrent == 5 and list(cfg.strategy.turbo_extras) == ["turtle20", "prevday", "prevweek"]
        if style == "hypermax":
            assert list(cfg.strategy.turbo_extras) == ["turtle20", "prevday", "prevweek"] and cfg.risk.max_concurrent == 3
