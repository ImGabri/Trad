"""Data-driven retuning of the trading rules (TP/SL/trailing/time stop/AI threshold/trigger/level filters).

Random search over a parameter grid on out-of-sample AI predictions:
  tune window (first 70 % of the OOS period)  → pick the best configuration
  validate window (last 30 %)                 → adopt it only if it is ALSO profitable there
Scoring is in R (P&L ÷ initial risk), so results don't depend on account size.
The winner is written to data/tuned.yaml, which overrides config.yaml at start-up.
"""
from __future__ import annotations

import copy
import random
from pathlib import Path

import numpy as np
import pandas as pd
import yaml

from ai_model import signal_from_proba
from backtest import run_backtest
from config import _deep_update, _ns
from strategy import compute_signals

SPACE = {
    "strategy.tp_atr": [0.5, 0.75, 1.0, 1.25, 1.5, 2.0],
    "strategy.sl_atr": [0.5, 0.75, 1.0, 1.25, 1.5],
    "strategy.time_stop_minutes": [5, 10, 15, 20, 30],
    "strategy.trail_trigger_atr": [0.5, 0.75, 1.0, 99.0],
    "strategy.trail_distance_atr": [0.3, 0.5, 0.75],
    "strategy.confidence_threshold": [0.5, 0.55, 0.6, 0.65],
    "strategy.m1_trigger_bars": [2, 3],
    "strategy.require_rising_volume": [True, False],
    "strategy.level_tolerance_atr": [0.15, 0.25, 0.4, 0.6],
    "strategy.m15_adx_min": [15, 20, 25],
    "sessions.asia_enabled": [True, False],
}
SIGNAL_KEYS = {"strategy.m1_trigger_bars", "strategy.require_rising_volume", "strategy.level_tolerance_atr",
               "strategy.m15_adx_min", "sessions.asia_enabled"}


def _nest(flat: dict) -> dict:
    out: dict = {}
    for k, v in flat.items():
        a, b = k.split(".")
        out.setdefault(a, {})[b] = v
    return out


def _cfg_with(cfg, flat: dict):
    raw = copy.deepcopy(cfg.raw)
    _deep_update(raw, _nest(flat))
    raw.setdefault("backtest", {})["initial_equity"] = 100_000  # R-based scoring; avoid size/survival artefacts
    raw["risk"]["min_lot_max_risk_pct"] = 0
    new = _ns(raw)
    new.raw, new.secrets = raw, cfg.secrets
    return new


def score(tr: pd.DataFrame, min_trades: int) -> dict:
    if tr is None or len(tr) < min_trades:
        return dict(n=0 if tr is None else len(tr), exp_r=float("nan"), pf=float("nan"), score=-1e9)
    r = tr["r"].to_numpy()
    wins, losses = r[r > 0].sum(), -r[r <= 0].sum()
    exp_r = float(r.mean())
    t = exp_r / (r.std(ddof=1) / np.sqrt(len(r))) if r.std(ddof=1) > 0 else 0.0
    return dict(n=int(len(r)), exp_r=exp_r, pf=float(wins / losses) if losses > 0 else float("inf"),
                win_rate=float((r > 0).mean() * 100), total_r=float(r.sum()), t=float(t), score=float(t))


def optimize(f: pd.DataFrame, p_up: pd.Series, events, cfg, data_dir: Path, trials: int = 60, seed: int = 7,
             say=print) -> dict:
    p_up = p_up.dropna()
    f = f.loc[p_up.index]
    cut = f.index[int(len(f) * 0.7)]
    parts = {"tune": f.index < cut, "validate": f.index >= cut}
    d, c = signal_from_proba(p_up)
    rng = random.Random(seed)
    base = {k: _get(cfg, k) for k in SPACE}
    cands = [base] + [{k: rng.choice(v) for k, v in SPACE.items()} for _ in range(trials)]
    sig_cache: dict = {}
    results = []
    min_trades = max(20, int(len(f[parts["tune"]]) / 1440 / 5))  # ≳ 1 trade per trading week
    say(f"Optimizing {len(cands)} rule sets on {f.index[0]:%Y-%m-%d} → {cut:%Y-%m-%d}, validating to {f.index[-1]:%Y-%m-%d} …")
    for k, flat in enumerate(cands):
        cf = _cfg_with(cfg, flat)
        key = tuple(flat[x] for x in sorted(SIGNAL_KEYS))
        if key not in sig_cache:
            sig_cache[key] = compute_signals(f, cf, events)
        sig = sig_cache[key]
        m = parts["tune"]
        res = run_backtest(f[m], sig[m], cf, d[m], c[m], label=f"trial{k}")
        sc = score(res["trades"], min_trades)
        results.append((sc["score"], k, flat, sc))
        if k % 10 == 0:
            best = max(results, key=lambda x: x[0])
            say(f"  {k + 1}/{len(cands)} tried — best so far: {best[3]['n']} trades, {best[3]['exp_r']:+.3f} R/trade, PF {best[3]['pf']:.2f}")
    results.sort(key=lambda x: x[0], reverse=True)
    report = []
    chosen = None
    for sc_, k, flat, tune_sc in results[:5]:  # validate the top 5, adopt the best that holds up
        cf = _cfg_with(cfg, flat)
        sig = sig_cache[tuple(flat[x] for x in sorted(SIGNAL_KEYS))]
        m = parts["validate"]
        val = score(run_backtest(f[m], sig[m], cf, d[m], c[m])["trades"], 10)
        report.append(dict(trial=k, params=flat, tune=tune_sc, validate=val))
        if chosen is None and val["n"] >= 10 and val["exp_r"] > 0 and val["pf"] > 1.0:
            chosen = report[-1]
    base_val = next((r for r in report if r["trial"] == 0), None)
    out = dict(adopted=chosen is not None, chosen=chosen, top5=report, base=base, base_validate=base_val)
    tuned = data_dir / "tuned.yaml"
    if chosen:
        tuned.write_text(yaml.safe_dump(dict(_nest(chosen["params"]),
                                             _tuning=dict(tune=chosen["tune"], validate=chosen["validate"])),
                                        sort_keys=False))
        say(f"✓ Adopted tuned rules (validate: {chosen['validate']['n']} trades, "
            f"{chosen['validate']['exp_r']:+.3f} R/trade, PF {chosen['validate']['pf']:.2f}) → {tuned}")
    else:
        tuned.unlink(missing_ok=True)
        say("✗ No rule set was profitable on BOTH the tuning and the validation period. Keeping config.yaml "
            "defaults — use watch mode only until more data says otherwise.")
    return out


def _get(cfg, dotted: str):
    a, b = dotted.split(".")
    return getattr(getattr(cfg, a), b)


def load_tuned(raw: dict, data_dir: Path) -> dict:
    p = data_dir / "tuned.yaml"
    if p.exists():
        t = yaml.safe_load(p.read_text()) or {}
        t.pop("_tuning", None)
        _deep_update(raw, t)
    return raw
