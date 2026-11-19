"""Bar-by-bar M1 backtester with the full §3/§5/§6 rule set and the §9 cost model.

Conventions
- Bars are BID prices (as MT5 stores them). Ask = bid + spread.
- A decision is made at the close of bar i and filled at the OPEN of bar i+1.
- Long: buy at ask (+ slippage), exit at bid. Short: sell at bid (− slippage), exit at ask.
- Stops/time-stops/forced flats pay slippage; take-profits are limit fills (no slippage).
- If a bar touches both SL and TP, SL is assumed to fill first (conservative).
- Costs: per-bar spread (or a flat $0.10 when missing) + commission per side per lot + slippage in pips.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

from config import commission_per_side
from strategy import decide_entry
from risk import RiskState, min_required_equity, position_size, survival_breached


@dataclass
class Pos:
    side: int
    entry: float
    sl: float
    tp: float
    lots: float
    open_i: int
    atr: float
    best: float
    spread_in: float
    conf: float
    kind: str  # "normal" | "news"


def run_backtest(f: pd.DataFrame, sig: pd.DataFrame, cfg, ai_dir: pd.Series | None = None,
                 ai_conf: pd.Series | None = None, entry_signal: pd.Series | None = None,
                 label: str = "strategy") -> dict:
    """f: features (features.build_features), sig: strategy.compute_signals.
    ai_dir/ai_conf: model direction (+1/-1) and confidence per bar; None = rules only (confidence 1.0).
    entry_signal: optional override of sig['rule_signal'] (used for baselines)."""
    st, rk, nw, bt, ins = cfg.strategy, cfg.risk, cfg.news, cfg.backtest, cfg.instrument
    n = len(f)
    idx = f.index
    o, h, l, c = (f[k].to_numpy(float) for k in ("open", "high", "low", "close"))
    spr = f["spread"].to_numpy(float)
    spr_ratio = f["spread_ratio"].to_numpy(float)
    atr5 = f["m5_atr"].to_numpy(float)
    rule = (entry_signal if entry_signal is not None else sig["rule_signal"]).to_numpy(int)
    has_ai = ai_conf is not None
    conf = ai_conf.reindex(idx).to_numpy(float) if has_ai else np.ones(n)
    adir = ai_dir.reindex(idx).fillna(0).to_numpy(int) if has_ai else rule.copy()
    session = sig["session"].to_numpy()
    blocked = sig["entry_blocked"].to_numpy(bool)
    must_flat = sig["must_flat"].to_numpy(bool)
    phase = sig["news_phase"].to_numpy()
    ev_id = sig["event_id"].to_numpy(int)
    days = idx.floor("D")
    gap_next = np.r_[(np.diff(idx.values) / np.timedelta64(1, "m")), np.inf]

    slip = bt.slippage_pips * ins.pip_size
    comm = commission_per_side(cfg)
    cs = ins.contract_size

    equity_cash = float(bt.initial_equity)
    risk = RiskState(cfg)
    positions: list[Pos] = []
    trades: list[dict] = []
    eq_curve = np.empty(n)
    exposure = np.zeros(n, bool)
    pending = None  # (side, sl_mult, tp_mult, conf, kind, decided_i)
    news_used: set[int] = set()
    stop_reason = None

    def mtm(i):
        u = 0.0
        for p in positions:
            px = c[i] if p.side > 0 else c[i] + spr[i]
            u += (px - p.entry) * p.side * p.lots * cs
        return equity_cash + u

    def close(p: Pos, i: int, px: float, reason: str):
        nonlocal equity_cash
        gross = (px - p.entry) * p.side * p.lots * cs
        fee = 2 * comm * p.lots
        equity_cash += gross - fee
        trades.append(dict(open_time=idx[p.open_i], close_time=idx[i], side=p.side, lots=p.lots,
                           entry=p.entry, exit=px, sl=p.sl, tp=p.tp, pnl=gross - fee, commission=fee,
                           hold_min=(i - p.open_i + 1), reason=reason, conf=p.conf, kind=p.kind,
                           spread_in=p.spread_in, session=session[p.open_i]))

    def exit_px(p: Pos, i: int, at: float | None = None) -> float:
        """Market exit at bar i close (or a given bid level) with slippage."""
        bid = c[i] if at is None else at
        return bid - slip if p.side > 0 else bid + spr[i] + slip

    for i in range(n):
        # ---------- fill pending order at this bar's open
        if pending is not None and stop_reason is None:
            side, slm, tpm, cf, kind, di = pending
            pending = None
            a = atr5[di]
            entry = o[i] + spr[i] + slip if side > 0 else o[i] - slip
            sl_d, tp_d = slm * a, tpm * a
            lots = position_size(equity_cash, sl_d, cs, rk.risk_per_trade_pct, ins.lot_step, ins.min_lot,
                                 ins.max_lot, comm)
            if lots > 0:
                positions.append(Pos(side, entry, entry - side * sl_d, entry + side * tp_d, lots, i, a,
                                     entry, spr[i], cf, kind))
                risk.trades_today += 1
                if kind == "news":
                    news_used.add(ev_id[di])

        # ---------- manage open positions intrabar
        for p in list(positions):
            if p.side > 0:
                hit_sl, hit_tp = l[i] <= p.sl, h[i] >= p.tp
                sl_fill = min(o[i], p.sl) - slip
            else:
                ask_h, ask_l = h[i] + spr[i], l[i] + spr[i]
                hit_sl, hit_tp = ask_h >= p.sl, ask_l <= p.tp
                sl_fill = max(o[i] + spr[i], p.sl) + slip
            if hit_sl:
                close(p, i, sl_fill, "trail" if p.side * (p.sl - p.entry) > 0 else "sl")
                positions.remove(p)
                continue
            if hit_tp:
                close(p, i, p.tp, "tp")
                positions.remove(p)
                continue
            # trailing (uses this bar's extreme, applies from next bar on)
            p.best = max(p.best, h[i]) if p.side > 0 else min(p.best, l[i] + spr[i])
            if (p.best - p.entry) * p.side >= st.trail_trigger_atr * p.atr:
                new_sl = p.best - p.side * st.trail_distance_atr * p.atr
                if (new_sl - p.sl) * p.side > 0:
                    p.sl = new_sl
            # time stop
            if i - p.open_i + 1 >= st.time_stop_minutes:
                close(p, i, exit_px(p, i), "time")
                positions.remove(p)

        # ---------- bar-close state
        risk.on_bar(days[i].date(), mtm(i) if i else equity_cash)
        eq = mtm(i)

        if must_flat[i] and positions:
            for p in list(positions):
                close(p, i, exit_px(p, i), "friday_flat")
            positions.clear()
            eq = mtm(i)

        if stop_reason is None:
            if risk.check_daily_loss(eq) and positions:
                for p in list(positions):
                    close(p, i, exit_px(p, i), "daily_loss_limit")
                positions.clear()
                eq = mtm(i)

        if stop_reason is None and not np.isnan(atr5[i]):
            req = min_required_equity(c[i], st.sl_atr * atr5[i], cs, cfg.broker.leverage, ins.min_lot)
            if survival_breached(eq, req, rk.survival_multiplier):
                for p in list(positions):
                    close(p, i, exit_px(p, i), "survival_hard_stop")
                positions.clear()
                risk.hard_stop = True
                stop_reason = ("survival_hard_stop", idx[i])
                eq = mtm(i)

        # pre-news: tighten SL on open positions
        if phase[i] == "pre":
            for p in positions:
                tight = c[i] - nw.pre_tighten_sl_atr * atr5[i] if p.side > 0 else c[i] + spr[i] + nw.pre_tighten_sl_atr * atr5[i]
                if (tight - p.sl) * p.side > 0:
                    p.sl = tight

        eq_curve[i] = eq
        exposure[i] = bool(positions)

        # ---------- entry decision at bar close → fill next bar
        if stop_reason or gap_next[i] > 5:
            continue
        side, slm, tpm, kind, _ = decide_entry(
            cfg, rule=int(rule[i]), ai_dir=int(adir[i]), conf=float(conf[i]), has_ai=has_ai,
            session=session[i], blocked=bool(blocked[i]), phase=phase[i], event_id=int(ev_id[i]),
            news_used=news_used, spread_ratio=float(spr_ratio[i]), atr_m5=float(atr5[i]), slippage_usd=slip)
        if side == 0:
            continue
        ok, _ = risk.can_open(len(positions), side,
                              [(p.side, (c[i] - p.entry) * p.side) for p in positions])
        if ok:
            pending = (int(side), slm, tpm, float(conf[i]), kind, i)

    # close anything left at the end
    for p in positions:
        close(p, n - 1, exit_px(p, n - 1), "end")
    eq_curve[-1] = equity_cash

    tr = pd.DataFrame(trades)
    eq_s = pd.Series(eq_curve, index=idx, name=label)
    return dict(label=label, trades=tr, equity=eq_s, exposure=exposure.mean(),
                metrics=metrics(eq_s, tr, exposure, bt.initial_equity), stop_reason=stop_reason,
                risk_events=risk.reasons)


# ------------------------------------------------------------------------------ metrics
def metrics(eq: pd.Series, tr: pd.DataFrame, exposure: np.ndarray, initial: float) -> dict:
    daily = eq.resample("D").last().dropna()
    daily = daily[daily.index.dayofweek < 5] if len(daily) > 10 else daily
    r = daily.pct_change().dropna()
    dd = eq / eq.cummax() - 1
    downside = r[r < 0].std()
    m = dict(
        final_equity=float(eq.iloc[-1]) if len(eq) else initial,
        total_return_pct=float((eq.iloc[-1] / initial - 1) * 100) if len(eq) else 0.0,
        sharpe=float(r.mean() / r.std() * np.sqrt(252)) if r.std() > 0 else 0.0,
        sortino=float(r.mean() / downside * np.sqrt(252)) if downside and downside > 0 else 0.0,
        max_drawdown_pct=float(dd.min() * 100) if len(dd) else 0.0,
        exposure_pct=float(np.mean(exposure) * 100),
        n_trades=int(len(tr)),
    )
    if len(tr):
        wins, losses = tr.loc[tr.pnl > 0, "pnl"], tr.loc[tr.pnl <= 0, "pnl"]
        m.update(win_rate_pct=float(len(wins) / len(tr) * 100),
                 profit_factor=float(wins.sum() / -losses.sum()) if losses.sum() < 0 else float("inf"),
                 avg_trade_pnl=float(tr.pnl.mean()), avg_hold_min=float(tr.hold_min.mean()),
                 total_commission=float(tr.commission.sum()),
                 exit_reasons=tr.reason.value_counts().to_dict())
    else:
        m.update(win_rate_pct=0.0, profit_factor=0.0, avg_trade_pnl=0.0, avg_hold_min=0.0)
    return m


def monthly_pnl(res: dict) -> pd.Series:
    tr = res["trades"]
    if tr.empty:
        return pd.Series(dtype=float)
    return tr.set_index("close_time")["pnl"].resample("ME").sum()


# ---------------------------------------------------------------------------- baselines
def buy_and_hold(f: pd.DataFrame, cfg) -> dict:
    bt, cs = cfg.backtest, cfg.instrument.contract_size
    c = f["close"]
    lots = bt.initial_equity / (c.iloc[0] * cs)  # 1× notional, fractional lots allowed for a benchmark
    entry = c.iloc[0] + f["spread"].iloc[0]
    eq = bt.initial_equity + (c - entry) * lots * cs - 2 * commission_per_side(cfg) * lots
    eq.name = "buy_and_hold"
    return dict(label="buy_and_hold", equity=eq, trades=pd.DataFrame(),
                metrics=metrics(eq, pd.DataFrame(), np.ones(len(eq), bool), bt.initial_equity))


def ema_cross_signal(f: pd.DataFrame, fast=9, slow=21) -> pd.Series:
    from features import ema
    a, b = ema(f["close"], fast), ema(f["close"], slow)
    cross = np.sign(a - b)
    return (cross.where(cross != cross.shift()).fillna(0)).astype(int)


def random_signal(f: pd.DataFrame, rate: float, seed: int = 7) -> pd.Series:
    rng = np.random.default_rng(seed)
    s = np.where(rng.random(len(f)) < rate, rng.choice([-1, 1], len(f)), 0)
    return pd.Series(s, index=f.index)


def run_with_baselines(f, sig, cfg, ai_dir=None, ai_conf=None) -> dict[str, dict]:
    res = {"strategy": run_backtest(f, sig, cfg, ai_dir, ai_conf, label="strategy")}
    # baselines bypass the AI gate and use identical exits/costs/risk rules
    res["ema_cross"] = run_backtest(f, sig, cfg, entry_signal=ema_cross_signal(f), label="ema_cross")
    rate = max(res["strategy"]["metrics"]["n_trades"], 50) / max(len(f), 1)
    res["random"] = run_backtest(f, sig, cfg, entry_signal=random_signal(f, rate), label="random")
    res["buy_and_hold"] = buy_and_hold(f, cfg)
    return res


def summary_table(results: dict[str, dict]) -> pd.DataFrame:
    keys = ["total_return_pct", "sharpe", "sortino", "max_drawdown_pct", "win_rate_pct", "profit_factor",
            "avg_trade_pnl", "avg_hold_min", "exposure_pct", "n_trades"]
    return pd.DataFrame({k: {m: v["metrics"].get(m) for m in keys} for k, v in results.items()}).T


# ---------------------------------------------------------------------------- walk-forward
def walk_forward_windows(index: pd.DatetimeIndex, test_months: int = 3, train_frac: float = 0.70,
                         holdout_months: int = 3, train_months: int | None = None):
    """Yield (train_start, train_end, test_start, test_end) rolling by `test_months`.
    Train length defaults to test × 0.7/0.3 (70/30 split); pass train_months (e.g. 36) for the
    production 3-year window. The final `holdout_months` are never yielded — use holdout_range()."""
    start, end = index.min().floor("D"), index.max().ceil("D")
    holdout_start = end - pd.DateOffset(months=holdout_months)
    train_m = train_months or int(round(test_months * train_frac / (1 - train_frac)))
    ts = start
    while True:
        tr_end = ts + pd.DateOffset(months=train_m)
        te_end = tr_end + pd.DateOffset(months=test_months)
        if te_end > holdout_start:
            break
        yield ts, tr_end, tr_end, te_end
        ts = ts + pd.DateOffset(months=test_months)


def holdout_range(index: pd.DatetimeIndex, holdout_months: int = 3):
    end = index.max().ceil("D")
    return end - pd.DateOffset(months=holdout_months), end
