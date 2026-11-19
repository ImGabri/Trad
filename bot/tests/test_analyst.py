import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace as NS

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT)); sys.path.insert(0, str(ROOT / "tests"))

import analyst  # noqa: E402
from analyst import M15Analyst, NewsAgent, chart_snapshot, m15_frame, m15_label, outlook_for_m1  # noqa: E402
from synthetic import synthetic_m1  # noqa: E402


def test_m15_analyst_trains_predicts_and_never_looks_ahead(tmp_path):
    m1 = synthetic_m1(days=60)
    x = m15_frame(m1)
    assert m15_label(x).iloc[-4:].isna().all()
    m = M15Analyst.fit(x)
    m.save(tmp_path)
    m2 = M15Analyst.load(tmp_path)
    p = m2.predict(x.iloc[-50:])
    assert p.between(0, 1).all()
    ol = outlook_for_m1(p, m1.index[-800:])
    # an M15 bar's forecast must not be visible before that bar has closed
    first_bar = p.index[0]
    assert (ol.loc[ol.index < first_bar + pd.Timedelta(minutes=14), "outlook_conf"] == 0).all()


class FakeClient:
    def __init__(self, payload):
        self.payload = payload
        self.calls = []
        self.beta = NS(messages=NS(create=self.create))

    def create(self, **kw):
        self.calls.append(kw)
        return NS(stop_reason="end_turn", content=[NS(type="text", text=json.dumps(self.payload))])


def test_news_agent_structured_outlook_and_self_grading(tmp_path, monkeypatch):
    payload = dict(bias="up", confidence=0.72, trade_conditions="normal", summary="Soft CPI weakens the dollar.",
                   drivers=["CPI below forecast"], risks=["FOMC speakers"])
    fake = FakeClient(payload)
    monkeypatch.setitem(sys.modules, "anthropic", NS(Anthropic=lambda: fake))
    agent = NewsAgent(tmp_path)
    out = agent.analyse({"price": 4000.0})
    assert out["bias"] == "up" and out["confidence"] == 0.72
    call = fake.calls[0]
    assert call["model"] == "claude-opus-5-5" and call["output_config"]["format"]["type"] == "json_schema"
    assert call["tools"][0]["type"] == "web_search_20260209"
    # graded 60+ minutes later: price went up → correct
    later = datetime.now(timezone.utc) + timedelta(minutes=61)
    tr = agent.track_record(4005.0, now=later)
    assert tr["graded"] == 1 and tr["hit_rate"] == 1.0
    # the track record is fed back into the next prompt
    agent.analyse({"price": 4005.0})
    assert '"hit_rate": 1.0' in fake.calls[1]["messages"][0]["content"]


def test_vetoes(monkeypatch):
    from config import load_config
    from strategy import decide_entry
    cfg = load_config(ROOT / "config.yaml", use_tuned=False)
    base = dict(rule=1, ai_dir=1, conf=0.6, has_ai=True, session="london", blocked=False, phase="none", event_id=-1,
                news_used=set(), spread_ratio=1.0, atr_m5=4.0, slippage_usd=0.0, bias=1)
    assert decide_entry(cfg, **base)[0] == 1
    assert decide_entry(cfg, **base, outlook_dir=-1, outlook_conf=0.7)[0] == 0
    assert decide_entry(cfg, **base, outlook_dir=1, outlook_conf=0.7)[0] == 1
    assert decide_entry(cfg, **base, news={"bias": "down", "confidence": 0.8, "trade_conditions": "normal"})[0] == 0
    assert decide_entry(cfg, **base, news={"bias": "up", "confidence": 0.8, "trade_conditions": "avoid"})[0] == 0


def test_chart_snapshot_is_compact():
    x = m15_frame(synthetic_m1(days=10))
    snap = chart_snapshot(x.dropna(), 0.61, ["2026-11-20 13:30 CPI"], 4000.0)
    assert len(json.dumps(snap)) < 1500 and snap["model_p_up_next_60min"] == 0.61


def test_m15_features_do_not_change_when_the_future_changes():
    """Look-ahead guard: features of bar t must be identical whatever happens after bar t closes."""
    m1 = synthetic_m1(days=20)
    x_full = m15_frame(m1)
    t = x_full.index[-300]
    cut = m1[m1.index < t + pd.Timedelta(minutes=15)]          # data only up to bar t's close
    x_cut = m15_frame(cut)
    cols = [c for c in x_full.columns if c != "close"]
    a, b = x_full.loc[t, cols].astype(float), x_cut.loc[t, cols].astype(float)
    assert np.allclose(a.fillna(-9), b.fillna(-9), rtol=1e-4, atol=1e-4), (a - b)[~np.isclose(a, b)]


def test_turbo_mode_signal_and_gate(tmp_path, monkeypatch):
    from config import load_config
    from strategy import compute_signals, decide_entry, turbo_signal
    import ui
    m1 = synthetic_m1(days=20)
    s = turbo_signal(m1)
    assert set(s.unique()) <= {0, 1} and s.sum() > 0
    hrs = s[s == 1].index.floor("60min")
    assert not hrs.duplicated().any()                      # at most one entry per hour
    cfg = load_config(ROOT / "config.yaml", use_tuned=False, overrides=ui.STYLES["turbo"])
    sig = compute_signals(m1.assign(**{c: 0.0 for c in ()}).pipe(lambda d: __import__("features").build_features(d, cfg)), cfg)
    assert (sig["rule_signal"] >= 0).all()                 # long-only
    base = dict(rule=1, ai_dir=-1, conf=0.5, has_ai=True, session="london", blocked=False, phase="none", event_id=-1,
                news_used=set(), spread_ratio=1.0, atr_m5=4.0, slippage_usd=0.0, bias=-1)
    assert decide_entry(cfg, **base)[0] == 1               # AI gate off in Turbo: the breakout alone decides
    monkeypatch.setattr(ui, "DATA", tmp_path)
    ui._write_user(ui.STYLES["turbo"])
    assert "mode: turbo" in (tmp_path / "user.yaml").read_text()
