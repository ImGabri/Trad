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
        assert st["settings"]["equity_tp_pct"] == 3.0
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
