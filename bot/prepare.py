"""Download history → features → walk-forward AI → backtests vs baselines → HTML report + final model."""
from __future__ import annotations

import base64
import copy
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


def download(cfg, data_dir: Path, years: float, say=print, dukascopy_back: bool = True) -> tuple[pd.DataFrame, dict]:
    """PU Prime MT5 bars (exact broker prices, kept forever in data/xau.db and extended every run) plus older
    history from the public OANDA dataset on Hugging Face (fallback: Dukascopy) for multi-year training."""
    from data import MT5Connection, Store, account_equity_usd, remember_terminal
    conn = MT5Connection(cfg)
    if not conn.connect():
        raise RuntimeError("Cannot connect to MT5 — open the PUPrime terminal and log in first.")
    remember_terminal(data_dir)
    store = Store(data_dir / "xau.db")
    from learning import CostModel
    (data_dir / "learning").mkdir(parents=True, exist_ok=True)
    costs = CostModel.learn(store)
    costs.save(data_dir / "learning" / "costs.json")
    if costs.spread_how or costs.slippage_usd is not None:
        say(f"  Learned costs from your own data: spread profile {'yes' if costs.spread_how else 'not yet'}, "
            f"slippage {costs.slippage_usd if costs.slippage_usd is not None else 'not yet'} ({costs.n_fills} fills)")
    gold = conn.resolve_gold(cfg.symbols.primary)
    suffix = gold[len(cfg.symbols.primary):]
    now = datetime.now(timezone.utc)
    start = now - timedelta(days=int(365.25 * years))
    syms = {"gold": gold}
    syms.update(conn.resolve_context(cfg, suffix))
    out = {}
    for key, sym in syms.items():
        if not sym:
            say(f"  {key}: not offered by this server")
            continue
        last = store.last_bar_time(sym, "M1")
        frm = max(start, last) if last else start
        say(f"  PU Prime {sym}: syncing M1 from {frm:%Y-%m-%d} …")
        store.save_bars(sym, "M1", conn.rates_range(sym, "M1", frm, now))
        df = store.load_bars(sym, "M1", start)
        plain = sym.split(".")[0]
        if key != "gold" and plain != sym:
            extra = store.load_bars(plain, "M1", start)
            df = (pd.concat([extra[~extra.index.isin(df.index)], df]).sort_index() if len(extra) else df)
        if key == "gold" and sym != cfg.symbols.primary:  # bars imported from CSV under the plain name
            extra = store.load_bars(cfg.symbols.primary, "M1", start)
            df = (pd.concat([extra[~extra.index.isin(df.index)], df]).sort_index() if len(extra) else df)
        say(f"  {sym}: {len(df):,} broker bars" + (f" {df.index.min():%Y-%m-%d} → {df.index.max():%Y-%m-%d}" if len(df) else ""))
        out[key] = df
    equity = account_equity_usd(gold)
    if equity is not None and equity < 10:  # empty/new account → fall back to config size
        equity = None
    conn.shutdown()
    gold_df = out.pop("gold", pd.DataFrame())
    gold_df = _older_history(cfg, data_dir, store, gold_df, start, say, dukascopy_back)
    if len(gold_df) < 50_000:
        raise RuntimeError(f"Only {len(gold_df):,} gold M1 bars. Check your internet connection (history download) "
                           f"or in MT5 open View → Symbols → {gold} → Bars and request more history.")
    corr = {k: v["close"] for k, v in out.items() if len(v)}
    for key, spec in vars(cfg.context).items():
        if not syms.get(key):
            corr.pop(key, None)  # not tradable live on this server → never train on it
            continue
        if getattr(spec, "hf", ""):
            s = _older_context(spec.hf, data_dir, corr.get(key), start, say)
            if s is not None:
                corr[key] = s
    corr["_equity"] = equity
    return gold_df, corr


def _older_history(cfg, data_dir: Path, store, mt5_df: pd.DataFrame, start: datetime, say=print,
                   dukascopy_back: bool = True) -> pd.DataFrame:
    """Prepend history older than the broker's own M1 depth. Broker bars always win where both exist."""
    hc = cfg.history
    first = mt5_df.index.min() if len(mt5_df) else pd.Timestamp.now(tz="UTC")
    if first <= pd.Timestamp(start) + pd.Timedelta(days=7):
        return mt5_df
    try:
        from history import download as hf_download, to_broker_bid
        say(f"  Downloading older gold history {start:%Y-%m} → {first:%Y-%m} (public OANDA data, cached locally) …")
        mid = hf_download(hc.hf_gold, data_dir / "hf_cache", start.date(), say)
        older = to_broker_bid(mid[mid.index < first], hc.oanda_mid_to_bid, None)
        from learning import CostModel
        older["spread"] = CostModel.load(data_dir / "learning" / "costs.json").spread_for(older.index,
                                                                                            cfg.backtest.spread_cost)
        if len(older):
            out = pd.concat([older, mt5_df]).sort_index() if len(mt5_df) else older
            return out.astype("float64")
    except Exception as e:  # noqa: BLE001
        say(f"  Hugging Face history unavailable ({e})")
    return _with_dukascopy(store, mt5_df, start, say) if dukascopy_back else mt5_df


def _older_context(repo: str, data_dir: Path, broker: pd.Series | None, start: datetime, say=print) -> pd.Series | None:
    """1-minute closes of another market for the years before the broker's own history (public source)."""
    try:
        from history import download as hf_download
        first = broker.index.min() if broker is not None and len(broker) else pd.Timestamp.now(tz="UTC")
        if first <= pd.Timestamp(start) + pd.Timedelta(days=7):
            return broker
        mid = hf_download(repo, data_dir / "hf_cache", start.date(), say)["close"]
        older = mid[mid.index < first]
        return pd.concat([older, broker]).sort_index() if broker is not None else older
    except Exception as e:  # noqa: BLE001
        say(f"  {repo} history unavailable ({e})")
        return broker


def _with_dukascopy(store, mt5_df: pd.DataFrame, start: datetime, say=print) -> pd.DataFrame:
    """Fill history older than the broker's M1 depth from Dukascopy (cached in SQLite as XAUUSD_DUKA)."""
    import dukascopy
    first_needed = mt5_df.index.min() if len(mt5_df) else pd.Timestamp.now(tz="UTC")
    have = store.load_bars("XAUUSD_DUKA", "M1", start)
    last_have = have.index.max() if len(have) else None
    a = (last_have + pd.Timedelta(days=1)).date() if last_have is not None else start.date()
    b = first_needed.date()
    if a < b:
        say(f"  Dukascopy: downloading XAUUSD M1 {a} → {b} (older than PUPrime's history; ~1 min per year)")
        new = dukascopy.download(a, b, say=say)
        if len(new):
            med = new["close"].median()
            if not 200 < med < 20000:
                say(f"  Dukascopy data looks wrong (median price {med:.1f}) — skipped"); new = new.iloc[0:0]
            store.save_bars("XAUUSD_DUKA", "M1", new)
        have = store.load_bars("XAUUSD_DUKA", "M1", start)
    if not len(have):
        say("  Dukascopy: no data (site unreachable?) — using PUPrime history only")
        return mt5_df
    older = have[have.index < first_needed]
    say(f"  + Dukascopy {len(older):,} older bars from {older.index.min():%Y-%m-%d}" if len(older) else "  Dukascopy: nothing older needed")
    return pd.concat([older, mt5_df]).sort_index()


def research(cfg, data_dir: Path, m1: pd.DataFrame, corr: dict, say=print, trials: int = 60) -> dict:
    from config import load_config
    from learning import CostModel, OutcomeModel, archive_model, live_samples, log_learning, simulated_samples
    from optimize import optimize
    corr = dict(corr)
    equity = corr.pop("_equity", None)
    learn_dir = data_dir / "learning"
    learn_dir.mkdir(parents=True, exist_ok=True)
    costs = CostModel.load(learn_dir / "costs.json")
    if costs.slippage_usd is not None:  # backtests use the slippage the bot actually got
        ov0 = copy.deepcopy(cfg.raw.get("_overrides") or {})
        ov0.setdefault("backtest", {})["slippage_pips"] = round(costs.slippage_usd / cfg.instrument.pip_size, 3)
        cfg = load_config(cfg.raw.get("_path"), overrides=ov0, tuned_path=data_dir / "tuned.yaml")
        say(f"Using your measured slippage: ${costs.slippage_usd:.3f} per side ({costs.n_fills} fills)")
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

    opt = None
    if len(p_wf) > 20_000 and trials > 0:
        opt = optimize(f, p_wf, events, cfg, data_dir, trials, say=say, equity=equity)
    elif trials > 0:
        say("Not enough out-of-sample data to retune rules safely (need ~2 weeks of walk-forward predictions).")
    # re-load with tuned.yaml (if adopted) and the real account size for the final report
    ov = copy.deepcopy(cfg.raw.get("_overrides") or {})
    if costs.slippage_usd is not None:
        ov.setdefault("backtest", {})["slippage_pips"] = round(costs.slippage_usd / cfg.instrument.pip_size, 3)
    if equity:
        ov.setdefault("backtest", {})["initial_equity"] = round(equity, 2)
    cfg = load_config(cfg.raw.get("_path"), overrides=ov, tuned_path=data_dir / "tuned.yaml")
    sig = compute_signals(f, cfg, events)
    if equity:
        say(f"Final backtests use your account size: ${equity:.2f} (USD equivalent)")

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

    # self-learning gate: learn which of the bot's own trades win (simulated walk-forward + real ones)
    sim = pd.DataFrame()
    if "walk_forward" in sections:
        sim = simulated_samples(sections["walk_forward"]["results"]["strategy"]["trades"], f, p_wf)
        sim.to_csv(learn_dir / "sim_trades.csv", index=False)
    from data import Store
    live = live_samples(Store(data_dir / "xau.db"))
    outcome = OutcomeModel.build(pd.concat([sim, live], ignore_index=True), say=say)
    outcome.save(learn_dir)

    say("\nTraining the final model on all data …")
    archive_model(data_dir)
    model = DirectionModel.fit(f, cfg)
    ref = sections.get("walk_forward") or sections.get("holdout")
    model.meta["backtest_win_rate"] = ref["results"]["strategy"]["metrics"]["win_rate_pct"] if ref else 50.0
    model.meta["confidence_threshold"] = cfg.strategy.confidence_threshold
    model.save(data_dir / "model")
    imp = model.importance(f)
    html = write_report(out_dir, sections, folds, imp, cfg, opt)
    summary = {k: {kk: v["metrics"] for kk, v in s["results"].items()} | {"auc": s["auc"]} for k, s in sections.items()}
    (out_dir / "summary.json").write_text(json.dumps(dict(folds=folds, sections=summary, optimization=opt,
                                                          holdout=[str(ho_a), str(ho_b)], model=model.meta),
                                                     indent=2, default=str))
    for k, s in sections.items():
        tr = s["results"]["strategy"]["trades"]
        if len(tr):
            tr.to_csv(out_dir / f"trades_{k}.csv", index=False)
    wf = summary.get("walk_forward", {}).get("strategy", {})
    log_learning(data_dir, dict(bars=len(f), first=str(f.index[0]), last=str(f.index[-1]),
                                wf_auc=summary.get("walk_forward", {}).get("auc"), wf_trades=wf.get("n_trades"),
                                wf_return_pct=wf.get("total_return_pct"), tuned=bool(opt and opt.get("adopted")),
                                outcome_gate=outcome.meta, live_trades=len(live)))
    say(f"\nReport: {html}")
    return summary


# ------------------------------------------------------------------------------------- report
def _png(fig) -> str:
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=110, bbox_inches="tight")
    import matplotlib.pyplot as plt
    plt.close(fig)
    return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()


def write_report(out_dir: Path, sections: dict, folds: list, imp: pd.Series, cfg, opt: dict | None = None) -> Path:
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
    if opt:
        parts.append("<h2>Rule optimisation</h2><p>" + ("Adopted tuned rules (data/tuned.yaml)." if opt["adopted"] else
                     "No rule set was profitable on both tuning and validation — defaults kept; run watch mode only.") + "</p>")
        rows = [dict(trial=r["trial"], **{k.split(".")[1]: v for k, v in r["params"].items()},
                     tune_n=r["tune"]["n"], tune_growth=round(r["tune"]["growth_pct"], 1), tune_PF=round(r["tune"]["pf"], 2),
                     val_n=r["validate"]["n"], val_growth=round(r["validate"]["growth_pct"], 1),
                     val_PF=round(r["validate"]["pf"], 2))
                for r in opt["top5"]]
        parts.append(pd.DataFrame(rows).to_html(index=False))
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
