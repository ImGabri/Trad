"""Offline tests for online_learning: no broker, no network (fetch_bars is stubbed)."""
import tempfile
import unittest
from unittest.mock import patch

import numpy as np
import pandas as pd

import online_learning as ol
import learning

CFG = {"hold_seconds": 180, "target_multiple": 1.4,
       "slippage_usd_oz_each_side": 0.04, "commission_roundtrip_usd_oz": 0.08,
       "min_model_score": 0.56, "stop_atr": 1.25, "minimum_stop_usd_oz": 0.70,
       "max_spread_usd_oz": 0.45, "max_spread_fraction_of_stop": 0.25,
       "entry_window_seconds": 2, "min_jump_usd_oz": 0.02, "min_jump_spreads": 0.20,
       "trigger_bucket_ms": 2000}

MODEL = {"features": ["a", "b", "c", "d", "e"], "classes": [-1, 0, 1],
         "weight": [[0.] * 5, [2.0] * 5, [0.] * 5], "bias": [0., 0., 0.],
         "median": [0.] * 5, "scale": [1.] * 5}


def _bars(count=200, start_ms=None, drift=0.02):
    if start_ms is None:
        start_ms = int(pd.Timestamp("2026-01-05", tz="UTC").value // 1_000_000)
    ms = start_ms + np.arange(count) * 60_000
    close = 2600.0 + np.cumsum(np.random.default_rng(7).normal(drift, 0.05, count))
    return pd.DataFrame({"ms": ms, "open": close - 0.1, "high": close + 0.2,
                         "low": close - 0.2, "close": close})


def _stub_pipeline(buy_proba=0.85):
    def fake_features(ticks, markets=None, macro=None, asof_ms=None):
        minute = int(ticks.ms.iloc[-1]) // 60_000 * 60_000
        idx = pd.Index([minute], name="decision_ms")
        return pd.DataFrame({"atr_usd_oz": [1.0], "tick_spread": [0.1],
                             "tick_spread_fraction": [0.13],
                             "tick_impulse_spreads": [1.0],
                             "quote_imbalance_1m": [0.5],
                             "quote_age_seconds": [0.5]}, index=idx)

    def fake_triggers(ticks, s):
        ms = int(ticks.ms.iloc[-1])
        return pd.DataFrame({"tick_impulse_spreads": [1.0], "tick_gap_log_ms": [0.0],
                             "tick_spread": [0.1], "tick_spread_change": [0.0]},
                            index=[ms])

    def fake_candidate(bars, points, s):
        src = bars.reindex(points.index // 60_000 * 60_000)
        src.index = points.index
        return src.copy()

    def fake_predict(model, x):
        proba = np.zeros((len(x), 3))
        proba[:, 2] = buy_proba   # BUY
        proba[:, 0] = (1.0 - buy_proba) / 2  # SELL
        proba[:, 1] = 1.0 - proba[:, 0] - proba[:, 2]  # WAIT
        return proba, np.zeros((len(x), 5))

    ol.features = fake_features
    ol.quote_triggers = fake_triggers
    ol.candidate_features = fake_candidate
    ol.predict = fake_predict


class OnlineLearnerCase(unittest.TestCase):
    def test_bar_ticks_are_two_sided_and_ordered(self):
        bars = _bars(10)
        ticks = ol._bar_ticks(bars, "gold")
        self.assertFalse(ticks.empty)
        self.assertTrue(ticks.ms.is_monotonic_increasing)
        self.assertTrue((ticks.ask >= ticks.bid).all())
        self.assertTrue((ticks.bid > 0).all())

    def test_fetch_bars_stubbed_returns_none_on_bad_payload(self):
        with patch.object(ol, "_fetch_json", return_value={"chart": {"result": [None]}}):
            self.assertIsNone(ol.fetch_bars("GC=F"))

    def test_state_roundtrip_keeps_weights_and_closed(self):
        with tempfile.TemporaryDirectory() as folder:
            first = ol.OnlineLearner(folder, MODEL, CFG)
            first.states["BUY"]["weights"] = [0.1, -0.2, 0.0, 0.05, 0.0]
            first.states["BUY"]["closed"] = 7
            first.signals[3] = {"id": 3, "side": 1, "trigger_ms": 1, "entry": 1.0, "sl": 0.0,
                                "tp": 2.0, "expires": 9_999_999_999_999, "inputs": [0.] * 5,
                                "resolved": False, "net": None}
            first.save()
            second = ol.OnlineLearner(folder, MODEL, CFG)
            self.assertEqual(second.states["BUY"]["weights"], [0.1, -0.2, 0.0, 0.05, 0.0])
            self.assertEqual(second.states["BUY"]["closed"], 7)
            self.assertEqual(second._next_id, 4)
            self.assertEqual(len(second.signals), 1)

    def test_scan_opens_signal_and_dedupes_within_minute(self):
        _stub_pipeline()
        with tempfile.TemporaryDirectory() as folder:
            learner = ol.OnlineLearner(folder, MODEL, CFG)
            learner.cache = {"gold": _bars()}
            now = int(_bars().ms.iloc[-1])
            first = learner.scan(asof_ms=now)
            self.assertIsNotNone(first)
            self.assertEqual(first["side"], "BUY")
            self.assertEqual(first["score"], 0.85)
            self.assertIn("sl", first) and "tp" in first
            self.assertIsNone(learner.scan(asof_ms=now))  # same minute: no duplicate
            self.assertEqual(len(learner.signals), 1)

    def test_scan_respects_min_model_score(self):
        _stub_pipeline(buy_proba=0.50)  # below min_model_score 0.56
        with tempfile.TemporaryDirectory() as folder:
            learner = ol.OnlineLearner(folder, MODEL, CFG)
            learner.cache = {"gold": _bars()}
            self.assertIsNone(learner.scan(asof_ms=int(_bars().ms.iloc[-1])))

    def test_scan_blocked_in_news_window(self):
        _stub_pipeline()
        events = pd.DataFrame({
            "event_id": ["e1"], "known_ms": [1], "event_ms": [int(_bars().ms.iloc[-1])],
            "name": ["Fed decision"], "active": [1]})
        with tempfile.TemporaryDirectory() as folder:
            learner = ol.OnlineLearner(folder, MODEL, CFG)
            learner.cache = {"gold": _bars()}
            self.assertIsNone(learner.scan(asof_ms=int(_bars().ms.iloc[-1]), events=events))

    def test_resolve_target_and_stop_update_weights(self):
        _stub_pipeline()
        with tempfile.TemporaryDirectory() as folder:
            learner = ol.OnlineLearner(folder, MODEL, CFG)
            base = _bars()
            now = int(base.ms.iloc[-1])
            learner.cache = {"gold": base}
            decision = learner.scan(asof_ms=now)
            self.assertIsNotNone(decision)
            entry, tp = decision["entry"], decision["tp"]
            sig_id = next(sid for sid, s in learner.signals.items()
                          if not s.get("resolved"))
            # price moves to target on the next bar
            follow = _bars(1, start_ms=now + 60_000).copy()
            follow.iloc[0] = [now + 60_000, entry, tp + 0.5, entry - 0.1, tp + 0.5]
            learner.cache = {"gold": pd.concat([base, follow], ignore_index=True)}
            resolved = learner.resolve()
            self.assertEqual(len(resolved), 1)
            self.assertEqual(resolved[0]["reason"], "target")
            self.assertGreater(resolved[0]["net_usd_oz"], 0)
            self.assertTrue(learner.signals[sig_id]["resolved"])
            # weights must have moved after a winning trade
            self.assertNotEqual(learner.states["BUY"]["weights"], [0.] * 5)

            # a second signal, then a sharp dive below its stop
            decision2 = learner.scan(asof_ms=now + 60_000)
            self.assertIsNotNone(decision2)
            entry2 = decision2["entry"]
            dive = _bars(1, start_ms=now + 120_000).copy()
            dive.iloc[0] = [now + 120_000, entry2, entry2 - 1.0, entry2 - 3.0, entry2 - 3.0]
            learner.cache = {"gold": pd.concat([learner.cache["gold"], dive], ignore_index=True)}
            resolved2 = learner.resolve()
            stops = [r for r in resolved2 if r["reason"] == "stop"]
            self.assertTrue(stops)
            self.assertLess(stops[0]["net_usd_oz"], 0)
            self.assertEqual(learner.states["BUY"]["closed"], 2)

    def test_time_exit_resolves_without_price_touch(self):
        _stub_pipeline()
        with tempfile.TemporaryDirectory() as folder:
            learner = ol.OnlineLearner(folder, MODEL, CFG)
            learner.cache = {"gold": _bars()}
            now = int(_bars().ms.iloc[-1])
            learner.scan(asof_ms=now)
            # advance the clock far past expiry without any further bars
            with patch.object(ol, "_now_ms", return_value=now + 10 * 60_000):
                resolved = learner.resolve()
            # no newer prices -> nothing to resolve yet
            self.assertEqual(resolved, [])

    def test_resolve_is_idempotent(self):
        _stub_pipeline()
        with tempfile.TemporaryDirectory() as folder:
            learner = ol.OnlineLearner(folder, MODEL, CFG)
            base = _bars()
            learner.cache = {"gold": base}
            decision = learner.scan(asof_ms=int(base.ms.iloc[-1]))
            follow = _bars(1, start_ms=int(base.ms.iloc[-1]) + 60_000).copy()
            follow.iloc[0] = [int(follow.iloc[0].ms), decision["entry"],
                              decision["tp"] + 0.5, decision["entry"] - 0.1,
                              decision["tp"] + 0.5]
            learner.cache = {"gold": pd.concat([base, follow], ignore_index=True)}
            learner.resolve()
            learner.resolve()  # second pass must not re-apply or double count
            self.assertEqual(learner.states["BUY"]["closed"], 1)

    def test_tick_swallows_network_failure(self):
        with tempfile.TemporaryDirectory() as folder:
            learner = ol.OnlineLearner(folder, MODEL, CFG)
            with patch.object(ol, "fetch_bars", side_effect=OSError("down")):
                report = learner.tick()
            self.assertIn("scan", report)
            self.assertIn("resolved", report)

    def test_tick_scan_error_never_raises(self):
        _stub_pipeline()
        with tempfile.TemporaryDirectory() as folder:
            learner = ol.OnlineLearner(folder, MODEL, CFG)
            learner.cache = {"gold": _bars()}
            with patch.object(ol, "predict", side_effect=ValueError("bad model")):
                report = learner.tick(asof_ms=int(_bars().ms.iloc[-1]))
            self.assertIsNone(report["scan"])
    def test_combined_learning_adds_demo_and_public_adjustments(self):
        demo = learning.blank()
        online = learning.blank()
        demo["BUY"]["weights"] = [0.1, 0.0, 0.0, 0.0, 0.0]
        online["BUY"]["weights"] = [-0.03, 0.02, 0.0, 0.0, 0.0]
        demo["BUY"]["closed"] = 4
        online["BUY"]["closed"] = 6

        states = learning.combined(demo, online)

        self.assertEqual(states["BUY"]["weights"][:2], [0.07, 0.02])
        self.assertEqual(states["BUY"]["closed"], 10)
        self.assertEqual(demo["BUY"]["weights"][0], 0.1)  # inputs are not mutated


if __name__ == "__main__":
    unittest.main()

