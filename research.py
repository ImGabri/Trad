"""Tick replay, chronological validation and market-data-only transport."""
from __future__ import annotations

import io
import json
import os
from pathlib import Path
from tempfile import NamedTemporaryFile
from zipfile import ZIP_DEFLATED, ZipFile

import numpy as np
import pandas as pd

import learning
from strategy import DAY, DIRECTION, candidate_features, event_mask, features, fit_model, predict, quote_frame, quote_triggers, spread_ok, stop_distance


def write_json(path, obj):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with NamedTemporaryFile(mode="w", encoding="utf8", dir=path.parent, delete=False) as f:
        json.dump(obj, f, indent=2, allow_nan=False)
        temp = f.name
    os.replace(temp, path)


def write_bundle(path, ticks, markets, macro, events, source):
    """Fixed member names. No account number, account history, file paths or secrets."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with NamedTemporaryFile(dir=path.parent, delete=False) as f:
        tmp = f.name
    try:
        with ZipFile(tmp, "w", compression=ZIP_DEFLATED, compresslevel=4) as z:
            z.writestr("source.json", json.dumps(source))
            for col in ("ms", "bid", "ask"):
                with z.open(col + ".npy", "w", force_zip64=True) as member:
                    np.lib.format.write_array(member, ticks[col].to_numpy(), allow_pickle=False)
            for name, frame in (("markets", markets), ("macro", macro), ("events", events)):
                if frame is not None and not frame.empty:
                    with z.open(name + ".csv", "w") as out:
                        frame.to_csv(out, index=False)
        os.replace(tmp, path)
    finally:
        Path(tmp).unlink(missing_ok=True)


def read_bundle(path):
    with ZipFile(path) as z:
        names = set(z.namelist())
        source = json.loads(z.read("source.json"))
        if source.get("format") != 1:
            raise ValueError("Unsupported market data file format.")
        raw = {c: np.load(io.BytesIO(z.read(c + ".npy")), allow_pickle=False) for c in ("ms", "bid", "ask")}
        optional = [pd.read_csv(z.open(n + ".csv")) if n + ".csv" in names else pd.DataFrame()
                    for n in ("markets", "macro", "events")]
    return quote_frame(raw), *optional, source


def fill_outcome(times, prices, entry_index, timeout_index, side, quote, distance, target, s):
    """Exit side is bid for a long and ask for a short. Keep adverse gaps at their quoted price."""
    moves = side * (prices[entry_index:timeout_index + 1] - quote)
    touches = np.flatnonzero((moves <= -distance) | (moves >= target))
    at = int(touches[0]) if len(touches) else len(moves) - 1
    gross = float(moves[at])
    reason = "time"
    if gross <= -distance:
        reason = "stop"
    elif gross >= target:
        gross, reason = target, "target"
    friction = 2*s["slippage_usd_oz_each_side"] + s["commission_roundtrip_usd_oz"]
    return gross - friction, int(times[entry_index + at]), reason


def replay(ticks, x, events, s):
    times = ticks.ms.to_numpy()
    bid, ask = ticks.bid.to_numpy(), ticks.ask.to_numpy()
    decisions = x.index.to_numpy()
    news = event_mask(decisions, events, s)
    entry_indices = np.searchsorted(times, decisions + s["latency_ms"])
    atr = x.atr_usd_oz.to_numpy()
    rows = []
    for j, (when, entry) in enumerate(zip(decisions, entry_indices)):
        if news[j] or entry >= len(times) or times[entry] - when > 1000*s["entry_window_seconds"]:
            continue
        spread = float(ask[entry] - bid[entry])
        distance = stop_distance(atr[j], s)
        if not spread_ok(spread, distance, s):
            continue
        end = int(np.searchsorted(times, times[entry] + 1000*s["hold_seconds"]))
        if end >= len(times):
            continue
        target = distance * s["target_multiple"]
        long = fill_outcome(times, bid, entry, end, 1, ask[entry], distance, target, s)
        short = fill_outcome(times, ask, entry, end, -1, bid[entry], distance, target, s)
        label = 0
        if max(long[0], short[0]) > 0 and long[0] != short[0]:
            label = 1 if long[0] > short[0] else -1
        rows.append((when, times[entry], spread, distance, *long, *short, label, max(long[1], short[1])))
    columns = ["decision_ms", "entry_ms", "entry_spread", "stop_distance",
               "long_net", "long_exit", "long_reason", "short_net", "short_exit", "short_reason",
               "label", "resolved_ms"]
    return pd.DataFrame(rows, columns=columns).set_index("decision_ms")


def make_trades(signals, replayed, s):
    available = -1
    last_day, entries_today = None, 0
    rows = []
    for when, direction, fold in signals:
        if not direction or when not in replayed.index:
            continue
        r = replayed.loc[when]
        day = int(r.entry_ms) // DAY
        if day != last_day:
            last_day, entries_today = day, 0
        if r.entry_ms < available or entries_today >= s["max_entries_utc_day"]:
            continue
        prefix = "long" if direction > 0 else "short"
        exit_ms = int(r[prefix + "_exit"])
        rows.append({"fold": fold, "decision_utc": iso(when), "entry_utc": iso(r.entry_ms),
                     "exit_utc": iso(exit_ms), "side": DIRECTION[direction],
                     "entry_spread_usd_oz": float(r.entry_spread), "exit_reason": r[prefix + "_reason"],
                     "net_usd_per_oz": float(r[prefix + "_net"])})
        entries_today += 1
        available = exit_ms + 1000*s["cooldown_seconds"]
    return pd.DataFrame(rows, columns=["fold", "decision_utc", "entry_utc", "exit_utc", "side",
                                        "entry_spread_usd_oz", "exit_reason", "net_usd_per_oz"])


def iso(ms):
    return pd.Timestamp(int(ms), unit="ms", tz="UTC").isoformat()


def metrics(trades):
    pnl = trades.net_usd_per_oz.to_numpy(float)
    equity = np.r_[0., np.cumsum(pnl)]
    losses = -pnl[pnl < 0].sum()
    return {"trades": len(pnl), "net_usd_per_oz": round(float(pnl.sum()), 4),
            "avg_net_usd_per_oz": round(float(pnl.mean()), 4) if len(pnl) else None,
            "win_rate": round(float(np.mean(pnl > 0)), 4) if len(pnl) else None,
            "profit_factor": round(float(pnl[pnl > 0].sum()/losses), 4) if losses > 0 else None,
            "closed_trade_drawdown_usd_per_oz": round(float((np.maximum.accumulate(equity)-equity).max()), 4)}


def adaptive_signals(slices, replayed, s):
    states = learning.blank()
    pending = None
    available = -1
    count, last_day = 0, None
    accepted = []
    for fold, test, model, probabilities in slices:
        contexts = test[["tick_spread_fraction", "tick_impulse_spreads", "quote_imbalance_1m"]].fillna(0).to_numpy()
        for when, p, ctx in zip(test.index, probabilities, contexts):
            if pending and when >= pending[0]:
                _, side, base, vector, net = pending
                learning.update(states[DIRECTION[side]], base, vector, net)
                pending = None
            if when < available or when not in replayed.index:
                continue
            day = when // DAY
            if day != last_day:
                count, last_day = 0, day
            if count >= s["max_entries_utc_day"]:
                continue
            side, _, vectors = learning.side_scores(model["classes"], p, states, ctx, s["min_model_score"])
            if not side:
                continue
            row = replayed.loc[when]
            prefix = "long" if side == 1 else "short"
            closed = int(row[prefix + "_exit"])
            base = float(p[model["classes"].index(side)])
            pending = (closed, side, base, vectors[side], float(row[prefix + "_net"]))
            available = closed + 1000*s["cooldown_seconds"]
            accepted.append((when, side, fold))
            count += 1
    return accepted


def train(ticks, markets, macro, events, source, s, output):
    bars = features(ticks, markets, macro)
    if len(bars) < 500:
        raise ValueError("Fewer than 500 complete minutes with adequate history. Capture more trading days.")
    x = candidate_features(bars, quote_triggers(ticks, s), s)
    if len(x) < 500:
        raise ValueError("Too few qualifying quote changes. Check the spread settings and export more history.")
    r = replay(ticks, x, events, s)
    boundaries = [int(len(x)*p) for p in (.55, .70, .85)] + [len(x)]
    signals, momentum, folds, slices = [], [], [], []
    for fold, (lo, hi) in enumerate(zip(boundaries[:-1], boundaries[1:]), start=1):
        test = x.iloc[lo:hi]
        past = r.loc[(r.index < test.index[0]) & (r.resolved_ms < test.index[0])]
        model = fit_model(x.loc[past.index], past.label)
        probabilities, _ = predict(model, test)
        slices.append((fold, test, model, probabilities))
        choice = np.array(model["classes"])[probabilities.argmax(axis=1)]
        choice[probabilities.max(axis=1) < s["min_model_score"]] = 0
        signals.extend(zip(test.index, choice, [fold]*len(test)))
        momentum.extend(zip(test.index, np.sign(test.gold_change_5m_atr.fillna(0)).astype(int), [fold]*len(test)))
        folds.append({"fold": fold, "training_rows": len(past), "last_training_outcome_utc": iso(past.resolved_ms.max()),
                      "test_from": iso(test.index[0]),
                      "test_through": iso(test.index[-1]), "test_quote_triggers": len(test)})
    trades = make_trades(adaptive_signals(slices, r, s), r, s)
    frozen = make_trades(signals, r, s)
    baseline = make_trades(momentum, r, s)
    for f in folds:
        f["ai"] = metrics(trades.loc[trades.fold.eq(f["fold"])])
    final = fit_model(x.loc[r.index], r.label)
    report = {"source": source, "quotes": len(ticks), "quote_history_utc": [iso(ticks.ms.iloc[0]), iso(ticks.ms.iloc[-1])],
              "complete_feature_minutes": len(bars), "qualifying_quote_triggers": len(x), "eligible_labeled_triggers": len(r),
              "strategy_settings": s, "walk_forward": folds, "ai": metrics(trades),
              "frozen_ai_same_costs": metrics(frozen),
              "five_minute_momentum_baseline": metrics(baseline), "flat_baseline_net_usd_per_oz": 0,
              "feature_coverage": {k: round(float(x[k].notna().mean()), 3) for k in final["features"]},
              "calendar_events_in_export": 0 if events is None or events.empty else int(events.event_id.nunique()),
              "units": "one troy ounce per trade; closed-trade drawdown; demo equity limits and lot rounding excluded"}
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    trades.to_csv(output / "ai_trades.csv", index=False)
    baseline.to_csv(output / "momentum_trades.csv", index=False)
    write_json(output / "backtest.json", report)
    write_json(output / "model.json", {"format": 1, "source": source, "strategy": s, "trained_through_ms": int(ticks.ms.iloc[-1]),
                                       "classifier": final})
    return report


def synthetic_data(days=4):
    """Deterministic plumbing fixture with one-second quotes. No market evidence."""
    rng = np.random.default_rng(21)
    ms = pd.Timestamp("2025-02-03T00:00:00Z").value // 1_000_000 + np.arange(days*86400)*1000
    i = np.arange(len(ms))
    moves = rng.normal(0, .065, len(ms)) + .007*np.sin(i/170)
    mid = 2600 + np.cumsum(moves)
    spread = np.round(.12 + .08*rng.random(len(ms)), 2)
    bid = np.round(mid - spread/2, 2)
    ticks = quote_frame({"ms": ms, "bid": bid, "ask": np.round(bid+spread, 2)})
    sample = np.arange(59, len(ms), 60)
    markets = pd.DataFrame({"known_ms": ms[sample]+1000, "series": "silver",
                            "value": 27 + np.cumsum(rng.normal(0, .01, len(sample)))})
    return ticks, markets, pd.DataFrame(), pd.DataFrame(), {"format": 1, "kind": "synthetic", "symbol": "SYNTHETIC"}
