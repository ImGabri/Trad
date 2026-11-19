"""Download history → features → walk-forward AI → backtests vs baselines → HTML report + final model."""
from __future__ import annotations

import base64
import io
import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pandas as pd

from ai_model import DirectionModel, auc, holdout_predictions, signal_from_proba, walk_forward_predictions
from backtest import monthly_pnl, run_with_baselines, summary_table
from features import build_features, make_label
from news import load_events, refresh_calendar
from strategy import compute_signals

log = logging.getLogger("prepare")


def download(cfg, data_dir: Path, years: float, say=print) -> tuple[pd.DataFrame, dict]:
    from data import MT5Connection, Store
    conn = MT5Connection(cfg)
    if not conn.connect():
        raise RuntimeError("Cannot connect to MT5 — open the PUPrime terminal and log in first.")
    store = Store(data_dir / "xau.db")
    gold = conn.resolve_gold(cfg.symbols.primary)
    suffix = gold[len(cfg.symbols.primary):]
    now = datetime.now(timezone.utc)
    start = now - timedelta(days=int(365.25 * years))
    syms = {"gold": gold}
    syms.update({k: conn.resolve_symbol(getattr(cfg.symbols, k), suffix) for k in ("dxy", "us10y", "spx")})
    out = {}
    for key, sym in syms.items():
        if not sym:
            say(f"  {key}: not offered by this server (feature left empty)")
            continue
        last = store.last_bar_time(sym, "M1")
        frm = max(start, last) if last else start
        say(f"  downloading {sym} M1 from {frm:%Y-%m-%d} … (MT5 may take a minute to sync history)")
        store.save_bars(sym, "M1", conn.rates_range(sym, "M1", frm, now))
        df = store.load_bars(sym, "M1", start)
        plain = sym.split(".")[0]
        if key != "gold" and plain != sym:
            extra = store.load_bars(plain, "M1", start)
            df = pd.concat([extra[~extra.index.isin(df.index)], df]).sort_index()
        if key == "gold" and sym != cfg.symbols.primary:  # bars imported from CSV under the plain name
            extra = store.load_bars(cfg.symbols.primary, "M1", start)
            df = pd.concat([extra[~extra.index.isin(df.index)], df]).sort_index()
        say(f"  {sym}: {len(df):,} bars {df.index.min():%Y-%m-%d} → {df.index.max():%Y-%m-%d}" if len(df) else f"  {sym}: no data")
        out[key] = df
    conn.shutdown()
    gold_df = out.pop("gold", pd.DataFrame())
    if len(gold_df) < 50_000:
        raise RuntimeError(f"Only {len(gold_df):,} gold M1 bars. In MT5 open View → Symbols → {gold} → Bars, "
                           "request more history, then try again (or load a CSV with 'import-csv').")
    return gold_df, {k: v["close"] for k, v in out.items() if len(v)}


def research(cfg, data_dir: Path, m1: pd.DataFrame, corr: dict, say=print) -> dict:
    out_dir = data_dir / "report"
    out_dir.mkdir(parents=True, exist_ok=True)
    refresh_calendar(cfg, data_dir)
    events = load_events(cfg, data_dir, m1.index.min(), m1.index.max() + pd.Timedelta(days=7))
    say(f"Building features on {len(m1):,} M1 bars …")
    f = build_features(m1, cfg, corr, None, events)
    sig = compute_signals(f, cfg, events)
    say(f"Rule setups: {(sig.rule_signal != 0).sum():,}")

    say("Walk-forward training (out-of-sample) …")
    p_wf, folds = walk_forward_predictions(f, cfg, log=say)
    say("Hold-out (final months, never seen in training) …")
    p_ho, _, (ho_a, ho_b) = holdout_predictions(f, cfg)

    sections = {}
    for name, p in (("walk_forward", p_wf), ("holdout", p_ho)):
        if p.empty:
            continue
        ff, ss = f.loc[p.index], sig.loc[p.index]
        d, c = signal_from_proba(p)
        res = run_with_baselines(ff, ss, cfg, d, c)
        y = make_label(ff, cfg.model.label_horizon_m5_bars)
        ok = y.notna()
        sections[name] = dict(results=res, auc=auc(y[ok], p[ok]), period=(p.index.min(), p.index.max()))
        say(f"\n{name}  {p.index.min():%Y-%m-%d} → {p.index.max():%Y-%m-%d}   AUC {sections[name]['auc']:.3f}")
        say(summary_table(res).round(2).to_string())

    say("\nTraining the final model on all data …")
    model = DirectionModel.fit(f, cfg)
    ref = sections.get("walk_forward") or sections.get("holdout")
    model.meta["backtest_win_rate"] = ref["results"]["strategy"]["metrics"]["win_rate_pct"] if ref else 50.0
    model.meta["confidence_threshold"] = cfg.strategy.confidence_threshold
    model.save(data_dir / "model")
    imp = model.importance(f)
    html = write_report(out_dir, sections, folds, imp, cfg)
    summary = {k: {kk: v["metrics"] for kk, v in s["results"].items()} | {"auc": s["auc"]} for k, s in sections.items()}
    (out_dir / "summary.json").write_text(json.dumps(dict(folds=folds, sections=summary,
                                                          holdout=[str(ho_a), str(ho_b)], model=model.meta),
                                                     indent=2, default=str))
    for k, s in sections.items():
        tr = s["results"]["strategy"]["trades"]
        if len(tr):
            tr.to_csv(out_dir / f"trades_{k}.csv", index=False)
    say(f"\nReport: {html}")
    return summary


# ------------------------------------------------------------------------------------- report
def _png(fig) -> str:
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=110, bbox_inches="tight")
    import matplotlib.pyplot as plt
    plt.close(fig)
    return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()


def write_report(out_dir: Path, sections: dict, folds: list, imp: pd.Series, cfg) -> Path:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    parts = [f"<h1>XAUUSD scalper — backtest report</h1><p>Generated {datetime.now(timezone.utc):%Y-%m-%d %H:%M} UTC. "
             f"Costs: spread per bar (fallback ${cfg.backtest.spread_cost}), ${cfg.backtest.commission_per_side_per_lot}"
             f"/side/lot commission, {cfg.backtest.slippage_pips} pips slippage. AI threshold "
             f"{cfg.strategy.confidence_threshold}.</p>"]
    for name, s in sections.items():
        res = s["results"]
        parts.append(f"<h2>{name.replace('_', '-')} ({s['period'][0]:%Y-%m-%d} → {s['period'][1]:%Y-%m-%d}), "
                     f"AUC {s['auc']:.3f}</h2>")
        parts.append(summary_table(res).round(2).to_html())
        fig, ax = plt.subplots(2, 1, figsize=(11, 6), sharex=True, gridspec_kw=dict(height_ratios=[3, 1]))
        for k, r in res.items():
            e = r["equity"].resample("1h").last().dropna()
            ax[0].plot(e.index, e.values, label=k, lw=1.6 if k == "strategy" else 0.9)
        ax[0].legend(); ax[0].set_ylabel("equity $"); ax[0].grid(alpha=.3)
        e = res["strategy"]["equity"]
        dd = (e / e.cummax() - 1) * 100
        ax[1].fill_between(dd.index, dd.values, 0, color="tab:red", alpha=.4); ax[1].set_ylabel("DD %"); ax[1].grid(alpha=.3)
        parts.append(f"<img src='{_png(fig)}'>")
        mp = monthly_pnl(res["strategy"])
        if len(mp):
            fig, ax = plt.subplots(figsize=(11, 2.8))
            ax.bar(mp.index.strftime("%Y-%m"), mp.values, color=np.where(mp.values >= 0, "tab:green", "tab:red"))
            ax.set_title("Monthly P&L"); ax.tick_params(axis="x", rotation=60); ax.grid(alpha=.3)
            parts.append(f"<img src='{_png(fig)}'>")
        m = res["strategy"]["metrics"]
        if m.get("exit_reasons"):
            parts.append("<p>Exit reasons: " + ", ".join(f"{k} {v}" for k, v in m["exit_reasons"].items()) + "</p>")
    if folds:
        parts.append("<h2>Walk-forward folds</h2>" + pd.DataFrame(folds).to_html(index=False))
    fig, ax = plt.subplots(figsize=(8, 7))
    top = imp.head(25)[::-1]
    ax.barh(top.index, top.values); ax.set_title("Feature importance (mean |SHAP|, final model)")
    parts.append(f"<img src='{_png(fig)}'>")
    css = ("body{font-family:Segoe UI,Arial;max-width:1150px;margin:24px auto;padding:0 16px}"
           "table{border-collapse:collapse;font-size:13px}td,th{border:1px solid #ccc;padding:3px 8px;text-align:right}"
           "img{max-width:100%}")
    path = out_dir / "report.html"
    path.write_text(f"<!doctype html><meta charset=utf-8><title>XAU backtest</title><style>{css}</style>" + "".join(parts),
                    encoding="utf-8")
    return path
