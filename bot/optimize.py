"""Data-driven retuning of the trading rules (TP/SL/trailing/time stop/AI threshold/trigger/level filters).

Random search over a parameter grid on out-of-sample AI predictions:
  tune window (first 70 % of the OOS period)  → pick the best configuration
  validate window (last 30 %)                 → adopt it only if it is ALSO profitable there
Each trial is simulated with YOUR account: starting balance, minimum lot, small-account rules, survival
stop and compounding. Score = log growth of the account (a config that blows the account up scores worst).
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
    "strategy.equity_tp_pct": [0.0, 0.5, 1.0, 1.0, 2.0, 3.0],   # 1 % (your rule) weighted ×2
    "strategy.equity_tp_hold_conf": [0.5, 0.55, 0.6, 0.65],
    "strategy.ai_only_min_conf": [0.0, 0.0, 0.6, 0.65, 0.7],
    "risk.max_daily_trades": [20, 40, 80],
    "risk.max_concurrent": [1, 2, 3],
    "analyst.veto_conf": [0.0, 0.55, 0.6, 0.65],
}
SIGNAL_KEYS = {"strategy.m1_trigger_bars", "strategy.require_rising_volume", "strategy.level_tolerance_atr",
               "strategy.m15_adx_min", "sessions.asia_enabled"}


def _nest(flat: dict) -> dict:
    out: dict = {}
    for k, v in flat.items():
        a, b = k.split(".")
        out.setdefault(a, {})[b] = v
    return out


def _cfg_with(cfg, flat: dict, equity: float):
    raw = copy.deepcopy(cfg.raw)
    _deep_update(raw, _nest(flat))
    raw.setdefault("backtest", {})["initial_equity"] = float(equity)
    new = _ns(raw)
    new.raw, new.secrets = raw, cfg.secrets
    return new


def score(res: dict, min_trades: int) -> dict:
    tr = res["trades"]
    eq = res["equity"]
    start, end = float(eq.iloc[0]) if len(eq) else 1.0, float(eq.iloc[-1]) if len(eq) else 1.0
    n = 0 if tr is None else len(tr)
    growth = float(np.log(max(end, 1e-9) / max(start, 1e-9)))
    if n < min_trades:  # too few trades to trust: rank below every config that traded enough
        return dict(n=n, exp_r=float("nan"), pf=float("nan"), growth_pct=(np.exp(growth) - 1) * 100,
                    blown=res.get("stop_reason") is not None, score=-1e6 + growth)
    r = tr["r"].to_numpy()
    wins, losses = tr.pnl[tr.pnl > 0].sum(), -tr.pnl[tr.pnl <= 0].sum()
    return dict(n=int(n), exp_r=float(r.mean()), pf=float(wins / losses) if losses > 0 else float("inf"),
                win_rate=float((tr.pnl > 0).mean() * 100), growth_pct=float((np.exp(growth) - 1) * 100),
                max_dd_pct=float(res["metrics"]["max_drawdown_pct"]), blown=res.get("stop_reason") is not None,
                score=growth)


def optimize(f: pd.DataFrame, p_up: pd.Series, events, cfg, data_dir: Path, trials: int = 60, seed: int = 7,
             say=print, equity: float | None = None, outlook: pd.DataFrame | None = None) -> dict:
    equity = float(equity or cfg.backtest.initial_equity)
    p_up = p_up.dropna()
    idx = p_up.index
    cut = idx[int(len(idx) * 0.7)]
    d, c = signal_from_proba(p_up)
    tmask, vmask = idx < cut, idx >= cut
    f_t, f_v = f.loc[idx[tmask]], f.loc[idx[vmask]]  # sliced once — no per-trial copies
    d_t, c_t, d_v, c_v = d[tmask], c[tmask], d[vmask], c[vmask]
    ol_t = outlook.reindex(idx[tmask]) if outlook is not None else None
    ol_v = outlook.reindex(idx[vmask]) if outlook is not None else None
    rng = random.Random(seed)
    base = {k: _get(cfg, k) for k in SPACE}
    cands = [base] + [{k: rng.choice(v) for k, v in SPACE.items()} for _ in range(trials)]
    skey = lambda flat: tuple(flat[x] for x in sorted(SIGNAL_KEYS))  # noqa: E731
    # group trials sharing signal settings so only ONE signal table is in memory at a time
    order = [0] + sorted(range(1, len(cands)), key=lambda k: str(skey(cands[k])))
    cache: dict = {}

    def signals(part: str, flat: dict, cf):
        key = (part, skey(flat))
        if key not in cache:
            cache.clear()
            cache[key] = compute_signals(f_t if part == "tune" else f_v, cf, events)
        return cache[key]

    results = []
    min_trades = max(30, int(len(f_t) / 1440 / 20))  # ≳ 1 trade per trading month
    say(f"Optimizing {len(cands)} rule sets for a {equity:.0f} account on {idx[0]:%Y-%m-%d} → {cut:%Y-%m-%d}, "
        f"validating to {idx[-1]:%Y-%m-%d} …")
    for done, k in enumerate(order):
        flat = cands[k]
        cf = _cfg_with(cfg, flat, equity)
        res = run_backtest(f_t, signals("tune", flat, cf), cf, d_t, c_t, label=f"trial{k}", outlook=ol_t)
        sc = score(res, min_trades)
        results.append((sc["score"], k, flat, sc))
        if done % 10 == 0:
            b = max(results, key=lambda x: x[0])[3]
            say(f"  {done + 1}/{len(cands)} tried — best so far: {b['n']} trades, account {b['growth_pct']:+.1f} %, "
                f"PF {b['pf']:.2f}")
    results.sort(key=lambda x: x[0], reverse=True)
    report = []
    chosen = None
    for sc_, k, flat, tune_sc in results[:5]:  # validate the top 5, adopt the best that holds up
        cf = _cfg_with(cfg, flat, equity)
        val = score(run_backtest(f_v, signals("validate", flat, cf), cf, d_v, c_v, outlook=ol_v), 10)
        report.append(dict(trial=k, params=flat, tune=tune_sc, validate=val))
        if (chosen is None and tune_sc["score"] > 0 and val["n"] >= 10 and val["growth_pct"] > 0
                and val["pf"] > 1.0 and not val["blown"]):
            chosen = report[-1]
    base_val = next((r for r in report if r["trial"] == 0), None)
    out = dict(adopted=chosen is not None, chosen=chosen, top5=report, base=base, base_validate=base_val)
    tuned = data_dir / "tuned.yaml"
    if chosen:
        tuned.write_text(yaml.safe_dump(dict(_nest(chosen["params"]),
                                             _tuning=dict(mode=str(getattr(cfg.strategy, "mode", "scalp")).lower(),
                                                          tune=chosen["tune"], validate=chosen["validate"])),
                                        sort_keys=False))
        v = chosen["validate"]
        say(f"✓ Adopted tuned rules — validation: {v['n']} trades, account {v['growth_pct']:+.1f} %, PF {v['pf']:.2f}, "
            f"max DD {v['max_dd_pct']:.1f} % → {tuned}")
    else:
        tuned.unlink(missing_ok=True)
        say("✗ No rule set was profitable on BOTH the tuning and the validation period. Keeping config.yaml "
            "defaults — use watch mode only until more data says otherwise.")
    return out


def _get(cfg, dotted: str):
    a, b = dotted.split(".")
    return getattr(getattr(cfg, a), b, SPACE[dotted][0])


def load_tuned(raw: dict, data_dir: Path) -> dict:
    p = data_dir / "tuned.yaml"
    if p.exists():
        t = yaml.safe_load(p.read_text()) or {}
        t.pop("_tuning", None)
        _deep_update(raw, t)
    return raw
