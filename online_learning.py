"""Online learning from real-time public market data.

Polls key-free public finance endpoints for recent intraday quotes of XAUUSD
and its context markets, runs the same signal pipeline as the broker-fed
runner on those public quotes, tracks every online signal until its stop,
target or time exit resolves on subsequent public prices, and updates the
side-specific logistic weights from each resolved outcome (learning.py).

Everything here is off the order path: the demo runner keeps trading on broker
quotes, network failures are swallowed and logged, and no account credentials
are ever sent online. State (weights, open signals) is persisted in
data/online_learning.json so learning survives restarts.
"""
from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

import numpy as np
import pandas as pd

import learning
from strategy import (MINUTE, DIRECTION, candidate_features, event_mask,
                      features, predict, quote_frame, quote_triggers,
                      spread_ok, stop_distance)

YAHOO = ("https://query1.finance.yahoo.com/v8/finance/chart/{symbol}?"
         "range=2d&interval=1m&includePrePost=true&events=div,splits")

SYMBOLS = {
    "gold": "GC=F",
    "silver": "SI=F",
    "dollar_index": "DX-Y.NYB",
    "sp500": "^GSPC",
    "nasdaq100": "^IXIC",
    "wti": "CL=F",
    "eurusd": "EURUSD=X",
    "vix": "^VIX",
}

SYNTHETIC_SPREAD = {"gold": 0.10, "silver": 0.02, "default": 0.05}

STATE_PATH_NAME = "online_learning.json"
REFRESH_SECONDS = 60
FETCH_TIMEOUT = 12
HISTORY_MINUTES = 240  # enough public context for 60-minute trend features
# Key-free public quotes can be delayed; accept recent data without pretending
# it is an executable broker quote. Order entry always remains on MT5 prices.
MAX_PUBLIC_AGE_MINUTES = 30


def _now_ms():
    return time.time_ns() // 1_000_000


def _atomic_write_json(path, obj):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    import os
    import tempfile
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf8", dir=path.parent,
                                     delete=False) as f:
        json.dump(obj, f, indent=2, allow_nan=False)
        tmp = f.name
    os.replace(tmp, path)


def _fetch_json(url):
    req = Request(url, headers={"User-Agent": "PUPrimeDemoResearch/1.0",
                                "Accept": "application/json"})
    with urlopen(req, timeout=FETCH_TIMEOUT) as resp:
        return json.loads(resp.read().decode("utf-8"))


def fetch_bars(symbol):
    """Recent intraday bars for one Yahoo symbol. ms = minute-start epoch ms.

    Returns None when the public source is unavailable, so the runner never
    waits on the network.
    """
    try:
        data = _fetch_json(YAHOO.format(symbol=symbol))
    except (HTTPError, URLError, OSError, ValueError):
        return None
    result = (data or {}).get("chart", {}).get("result")
    if not result or result[0] is None or not result[0].get("timestamp"):
        return None
    res = result[0]
    stamps = res["timestamp"]
    quote = (res.get("indicators") or {}).get("quote", [{}])[0]
    closes = quote.get("close")
    if not stamps or closes is None:
        return None
    frame = pd.DataFrame({
        "ms": np.asarray(stamps, dtype="int64") * 1000,
        "open": quote.get("open"),
        "high": quote.get("high"),
        "low": quote.get("low"),
        "close": closes,
    })
    frame = frame.dropna(subset=["close"]).sort_values("ms")
    frame = frame.drop_duplicates("ms", keep="last")
    frame = frame.loc[frame.ms > 0]
    cutoff = _now_ms() - HISTORY_MINUTES * MINUTE
    frame = frame.loc[frame.ms >= cutoff]
    for col in ("open", "high", "low", "close"):
        frame[col] = pd.to_numeric(frame[col], errors="coerce")
    frame = frame.dropna().reset_index(drop=True)
    return frame if not frame.empty else None


def _bar_ticks(bars, symbol):
    """Turn 1-minute public bars into two-sided quote ticks."""
    spread = float(SYNTHETIC_SPREAD.get(symbol, SYNTHETIC_SPREAD["default"]))
    rows = []
    for _, r in bars.iterrows():
        m = int(r.ms)
        points = ((m, float(r.open)), (m + 20_000, float(r.high)),
                  (m + 40_000, float(r.low)), (m + 59_000, float(r.close)))
        half = spread / 2
        for t, mid in points:
            rows.append((t, mid - half, mid + half))
    if not rows:
        return quote_frame({"ms": [], "bid": [], "ask": []})
    data = pd.DataFrame(rows, columns=["ms", "bid", "ask"])
    return quote_frame({"ms": data.ms.to_numpy(),
                        "bid": data.bid.to_numpy(),
                        "ask": data.ask.to_numpy()})


class OnlineLearner:
    """Learn side-specific adjustments from public real-time gold prices.

    Parameters
    ----------
    folder : str | Path
        Working folder; state is saved to folder/online_learning.json.
    model : dict
        Frozen classifier from strategy.fit_model (feature names, weights ...).
    settings : dict
        Strategy settings block from settings.json.
    """

    def __init__(self, folder, model, settings):
        self.folder = Path(folder)
        self.model = model
        self.s = settings
        self.path = self.folder / STATE_PATH_NAME
        self.lock = threading.Lock()
        self.cache = {}          # symbol -> bars DataFrame
        self.states = learning.blank()  # side -> {"weights", "closed"}
        self.signals = {}        # id -> open signal dict
        self._next_id = 0
        self._last_refresh = 0.0
        self._last_scanned_ms = 0
        self._thread = None
        self._stop = None
        self.notes = {}          # symbol -> fetch status for diagnostics
        self._load()

    # ------------------------------------------------------------------ state
    def _load(self):
        try:
            obj = json.loads(self.path.read_text(encoding="utf8"))
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            return
        states = obj.get("states")
        if isinstance(states, dict):
            for side in ("BUY", "SELL"):
                entry = states.get(side)
                if isinstance(entry, dict) and "weights" in entry:
                    w = entry["weights"]
                    if len(w) == len(learning.INPUTS):
                        self.states[side]["weights"] = [float(v) for v in w]
                    self.states[side]["closed"] = int(entry.get("closed", 0))
        self._next_id = int(obj.get("next_id", 0))
        open_signals = obj.get("signals") or {}
        now = _now_ms()
        for sid, sig in open_signals.items():
            try:
                sig["entry_index"] = None
                sig["prices"] = None
                if not sig.get("resolved") and sig.get("expires", 0) > now:
                    self.signals[int(sid)] = sig
                    self._next_id = max(self._next_id, int(sid) + 1)
            except (ValueError, TypeError):
                continue

    def save(self):
        with self.lock:
            obj = {"states": {side: dict(val) for side, val in self.states.items()},
                   "next_id": self._next_id,
                   "signals": {str(k): {kk: vv for kk, vv in v.items()
                                        if kk not in ("prices", "entry_index")}
                               for k, v in self.signals.items()},
                   "updated_utc": pd.Timestamp.now(tz="UTC").isoformat()}
        _atomic_write_json(self.path, obj)

    # ------------------------------------------------------------------ public feed
    def refresh(self):
        """Fetch the latest public bars once (blocking, bounded by timeout)."""
        fresh = {}
        for key, symbol in SYMBOLS.items():
            try:
                bars = fetch_bars(symbol)
            except Exception as exc:  # network/parse problems stay local
                bars = None
                self.notes[key] = f"unavailable ({type(exc).__name__})"
            if bars is not None and not bars.empty:
                fresh[key] = bars
                stamp = (pd.Timestamp(int(bars.ms.iloc[-1]), unit="ms")
                         .tz_localize("UTC").isoformat())
                self.notes[key] = f"{len(bars)} bars through {stamp}"
            else:
                self.notes.setdefault(key, "no public data")
        with self.lock:
            self.cache = fresh
            self._last_refresh = time.monotonic()
        return dict(self.notes)

    def _background(self):
        while not self._stop.is_set():
            try:
                self.refresh()
            except Exception as exc:  # pragma: no cover - defensive
                self.notes["background"] = f"stopped ({exc})"
            self._stop.wait(REFRESH_SECONDS)

    def start(self):
        """Start the background public-feed poller (daemon, never blocks trading)."""
        if self._thread is not None:
            return
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._background, name="trad-online-feed",
                                        daemon=True)
        self._thread.start()

    def stop(self):
        if self._stop is not None:
            self._stop.set()
        self.save()

    # ------------------------------------------------------------------ signal pipeline
    def _context_frame(self, bars_by_symbol, asof_ms):
        """Build the markets frame the shared feature pipeline expects."""
        if not bars_by_symbol:
            return pd.DataFrame(columns=["series", "known_ms", "value"])
        times = np.sort(np.unique(np.concatenate([b.ms.to_numpy()
                                                  for b in bars_by_symbol.values()])))
        rows = []
        for series, bars in bars_by_symbol.items():
            close = bars.set_index("ms").close.sort_index()
            values = close.reindex(close.index.union(times)).ffill().reindex(times)
            for t, value in zip(times, values):
                if pd.notna(value):
                    rows.append({"series": series, "known_ms": int(t),
                                 "value": float(value)})
        if not rows:
            return pd.DataFrame(columns=["series", "known_ms", "value"])
        return pd.DataFrame(rows)

    def scan(self, asof_ms=None, events=None):
        """Score the newest public impulse and open a signal when warranted.

        Returns the emitted decision dict or None. Never raises.
        """
        try:
            return self._scan(asof_ms, events)
        except Exception as exc:  # keep the order path healthy
            self.notes["scan"] = f"error ({type(exc).__name__})"
            return None

    def _scan(self, asof_ms=None, events=None):
        with self.lock:
            cache = dict(self.cache)
            states = {side: dict(val) for side, val in self.states.items()}
        gold = cache.get("gold")
        if gold is None or gold.empty:
            return None
        asof_ms = int(asof_ms or _now_ms())
        ticks = _bar_ticks(gold, "gold")
        latest_ms = int(ticks.ms.iloc[-1]) if not ticks.empty else 0
        if not latest_ms or latest_ms <= self._last_scanned_ms:
            return None
        if latest_ms <= asof_ms - MAX_PUBLIC_AGE_MINUTES * MINUTE:
            return None  # public data is too old to learn from safely
        self._last_scanned_ms = latest_ms
        bars = features(ticks, self._context_frame(cache, asof_ms), None,
                        asof_ms=asof_ms)
        if bars.empty:
            return None
        points = quote_triggers(ticks, self.s)
        if points.empty:
            return None
        eligible = candidate_features(bars, points, self.s)
        if eligible.empty:
            return None
        row = eligible.tail(1)
        if events is not None and not events.empty:
            if bool(event_mask(np.array([int(row.index[-1])]), events, self.s)[0]):
                return None  # scheduled news window: stay out
        prob, _ = predict(self.model, row)
        classes = self.model["classes"]
        x = row.iloc[0]
        ctx = (float(x.tick_spread_fraction), float(x.tick_impulse_spreads),
               float(x.quote_imbalance_1m))
        side, scores, vectors = learning.side_scores(
            classes, prob[0], states, ctx, self.s["min_model_score"])
        if not side:
            return None  # scored, but below threshold: no signal
        atr = float(x.atr_usd_oz)
        distance = stop_distance(atr, self.s)
        if not spread_ok(float(x.tick_spread), distance, self.s):
            return None
        trigger_ms = int(row.index[-1])
        with self.lock:
            if any(not s.get("resolved") and s["trigger_ms"] == trigger_ms
                   for s in self.signals.values()):
                return None  # already emitted for this minute
        latest = ticks.loc[ticks.ms <= trigger_ms]
        mid = float((latest.bid.iloc[-1] + latest.ask.iloc[-1]) / 2)
        entry = float(mid)
        vector = vectors.get(side) if isinstance(vectors, dict) else None
        vector = np.array([1.0, 0.0, 0.0, 0.0, 0.0]) if vector is None \
            else np.asarray(vector, dtype=float)
        signal = {
            "id": self._next_id,
            "side": int(side),
            "trigger_ms": trigger_ms,
            "base": float(prob[0, classes.index(side)]),
            "score": float(scores.get(side, prob[0, classes.index(side)])),
            "entry": entry,
            "sl": float(entry - side * distance),
            "tp": float(entry + side * distance * self.s["target_multiple"]),
            "expires": trigger_ms + int(self.s["hold_seconds"]) * 1000,
            "inputs": vector.tolist(),
            "resolved": False,
            "net": None,
            "resolved_ms": None,
            "reason": None,
        }
        with self.lock:
            self.signals[signal["id"]] = signal
            self._next_id += 1
            open_count = len(self.signals)
        stamp = (pd.Timestamp(trigger_ms, unit="ms")
                 .tz_localize("UTC").isoformat())
        self.notes["signal"] = (f"{DIRECTION[side]} score {signal['score']:.3f} "
                                f"at {stamp}")
        return {"mode": "online", "side": DIRECTION[side], "score": signal["score"],
                "base": signal["base"], "trigger_ms": trigger_ms, "entry": entry,
                "sl": signal["sl"], "tp": signal["tp"], "open_signals": open_count}



    # ------------------------------------------------------------------ outcomes
    def resolve(self):
        """Resolve any open signals against the latest public prices."""
        with self.lock:
            open_ids = [sid for sid, sig in self.signals.items()
                        if not sig.get("resolved")]
            cache = dict(self.cache)
        if not open_ids:
            return []
        gold = cache.get("gold")
        if gold is None or gold.empty:
            return []
        prices = gold.close.to_numpy(dtype=float)
        times = gold.ms.to_numpy(dtype="int64")
        results = []
        for sid in open_ids:
            with self.lock:
                sig = self.signals.get(sid)
                if sig is None or sig.get("resolved"):
                    continue
            start = int(np.searchsorted(times, sig["trigger_ms"], side="right")) - 1
            if start < 0:
                continue
            side = sig["side"]
            moves = side * (prices[start:] - sig["entry"])
            stop_hits = np.flatnonzero(moves <= -sig["entry"] + sig["sl"])
            target_hits = np.flatnonzero(moves >= sig["tp"] - sig["entry"])
            time_indices = np.flatnonzero(times[start:] > sig["expires"])
            at = None
            reason = None
            if len(stop_hits) and (not len(target_hits) or stop_hits[0] <= target_hits[0]) \
               and (not len(time_indices) or stop_hits[0] <= time_indices[0]):
                at, reason = int(stop_hits[0]), "stop"
            elif len(target_hits) and (not len(time_indices) or target_hits[0] <= time_indices[0]):
                at, reason = int(target_hits[0]), "target"
            elif len(time_indices):
                at, reason = int(time_indices[0]), "time"
            if at is None:
                continue
            gross = float(moves[at])
            friction = 2 * self.s["slippage_usd_oz_each_side"] + \
                self.s["commission_roundtrip_usd_oz"]
            net = round(gross - friction, 6)
            end_ms = int(times[start + at])
            with self.lock:
                target_state = self.signals.get(sid)
                if target_state is None or target_state.get("resolved"):
                    continue
                target_state["resolved"] = True
                target_state["resolved_ms"] = end_ms
                target_state["reason"] = reason
                target_state["net"] = net
                self.states[sig["side"] == 1 and "BUY" or "SELL"], before, after = \
                    self._apply_update(sig, net)
            results.append({"id": sid, "side": DIRECTION[side], "reason": reason,
                            "gross_usd_oz": round(gross, 4),
                            "net_usd_oz": net, "trigger_ms": sig["trigger_ms"],
                            "resolved_ms": end_ms,
                            "held_seconds": round((end_ms - sig["trigger_ms"]) / 1000, 3),
                            "before": before, "after": after})
        self.save()
        return results

    def _apply_update(self, sig, net):
        side_label = "BUY" if sig["side"] == 1 else "SELL"
        base = float(sig["base"])
        inputs = np.asarray(sig["inputs"], dtype=float)
        before, after = learning.update(self.states[side_label], base, inputs, net)
        return self.states[side_label], before, after

    # ------------------------------------------------------------------ engine hook
    def tick(self, asof_ms=None, events=None):
        """One service-cycle tick: refresh feed if due, scan, resolve, learn.

        Safe to call on the trading thread; all network access is bounded and
        failures are swallowed. Returns dict with scan/resolve summaries.
        """
        report = {"feed": dict(self.notes), "scan": None, "resolved": []}
        due = (self._thread is None
               and (self._last_refresh == 0.0
                    or time.monotonic() - self._last_refresh >= REFRESH_SECONDS))
        if due:
            try:
                report["feed"] = self.refresh()
            except Exception as exc:  # pragma: no cover - defensive
                report["feed"] = {"error": f"{type(exc).__name__}: {exc}"}
        try:
            report["scan"] = self.scan(asof_ms, events)
        except Exception as exc:  # pragma: no cover - defensive
            report["scan"] = {"error": f"{type(exc).__name__}: {exc}"}
        try:
            report["resolved"] = self.resolve()
        except Exception as exc:  # pragma: no cover - defensive
            report["resolved"] = [{"error": f"{type(exc).__name__}: {exc}"}]
        return report

    def status(self):
        with self.lock:
            open_s = [sid for sid, s in self.signals.items() if not s.get("resolved")]
            closed = [s for s in self.signals.values() if s.get("resolved")]
        return {
            "updated_utc": pd.Timestamp.now(tz="UTC").isoformat(),
            "states": {side: dict(val) for side, val in self.states.items()},
            "open_signals": len(open_s),
            "resolved_signals": len(closed),
            "net_usd_oz": round(sum(float(s.get("net") or 0) for s in closed), 4),
            "feed": dict(self.notes),
        }
