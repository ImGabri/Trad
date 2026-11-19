"""Self-learning: the bot keeps everything it sees and learns from its own results.

1. CostModel   — real costs measured from its own logs: spread by hour-of-week (from the tick spread log
                 and broker bars) and real slippage (from its own fills). Backtests then use YOUR costs.
2. OutcomeModel — meta-labelling: a small model that learns which of the bot's OWN trades win, from
                 thousands of simulated walk-forward trades plus every real closed trade (weighted ×5).
                 It is only switched on if it improves results on trades it never saw; then it vetoes
                 entries it expects to lose, and it re-fits after every closed live trade.
3. Archive      — every retrain keeps the previous model in data/models/<time>/ and appends a line to
                 data/learning_log.jsonl, so progress is tracked and data only ever accumulates.
"""
from __future__ import annotations

import json
import logging
import pickle
import shutil
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

log = logging.getLogger("learning")

OUTCOME_FEATURES = ["p_side", "side", "hour_sin", "hour_cos", "dow", "sess_london", "sess_ny", "sess_asia",
                    "spread_ratio", "vol_regime", "atr_bps", "bias_agree", "adx15", "vwap_dev_atr", "rsi14",
                    "kind_ai", "kind_news", "min_to_event"]
LIVE_WEIGHT = 5.0


# ----------------------------------------------------------------------------- cost model
class CostModel:
    def __init__(self, spread_how: list[float] | None = None, slippage_usd: float | None = None, n_fills: int = 0):
        self.spread_how, self.slippage_usd, self.n_fills = spread_how, slippage_usd, n_fills

    @classmethod
    def learn(cls, store, min_fills: int = 20) -> "CostModel":
        prof = None
        try:
            sp = pd.read_sql_query("SELECT time_ms, spread FROM spread_log WHERE spread > 0", store.conn)
            if len(sp) > 50_000:
                t = pd.to_datetime(sp.time_ms, unit="ms", utc=True)
                how = t.dt.dayofweek * 24 + t.dt.hour
                med = sp.spread.groupby(how).median()
                prof = [float(med.get(h, np.nan)) for h in range(168)]
                base = float(np.nanmedian(prof))
                prof = [base if not np.isfinite(v) else v for v in prof]
        except Exception as e:  # noqa: BLE001
            log.warning("spread profile: %s", e)
        slip, n = None, 0
        try:
            r = pd.read_sql_query("SELECT slippage FROM trades WHERE slippage IS NOT NULL", store.conn)
            n = len(r)
            if n >= min_fills:
                slip = float(max(0.0, r.slippage.clip(lower=0).mean()))
        except Exception:  # noqa: BLE001
            pass
        return cls(prof, slip, n)

    def spread_for(self, idx: pd.DatetimeIndex, default: float) -> np.ndarray:
        if not self.spread_how:
            return np.full(len(idx), default)
        return np.asarray(self.spread_how)[np.asarray(idx.dayofweek * 24 + idx.hour)]

    def save(self, path: Path):
        path.write_text(json.dumps(dict(spread_how=self.spread_how, slippage_usd=self.slippage_usd,
                                        n_fills=self.n_fills, updated=_now()), indent=2))

    @classmethod
    def load(cls, path: Path) -> "CostModel":
        try:
            d = json.loads(path.read_text())
            return cls(d.get("spread_how"), d.get("slippage_usd"), d.get("n_fills", 0))
        except (FileNotFoundError, ValueError):
            return cls()


# -------------------------------------------------------------------------- outcome model
def entry_features(row: pd.Series, side: int, p_up: float, kind: str) -> dict:
    """Same feature vector for simulated and live trades (all known at the decision bar's close)."""
    h = int(row.get("hour", 0))
    return dict(
        p_side=float(p_up if side > 0 else 1 - p_up), side=float(side),
        hour_sin=float(np.sin(2 * np.pi * h / 24)), hour_cos=float(np.cos(2 * np.pi * h / 24)),
        dow=float(row.get("dow", 0)), sess_london=float(row.get("sess_london", 0)),
        sess_ny=float(row.get("sess_ny", 0)), sess_asia=float(row.get("sess_asia", 0)),
        spread_ratio=float(row.get("spread_ratio", 1.0)), vol_regime=float(row.get("vol_regime", 1)),
        atr_bps=float(row.get("m5_atr", 0) / max(row.get("close", 1), 1e-9) * 1e4),
        bias_agree=float((row.get("m15_ema_fast", 0) - row.get("m15_ema_slow", 0)) * side > 0),
        adx15=float(row.get("m15_adx", 0)), vwap_dev_atr=float(row.get("vwap_dev_atr", 0)),
        rsi14=float(row.get("rsi14", 50)), kind_ai=float(kind == "ai"), kind_news=float(kind == "news"),
        min_to_event=float(np.clip(row.get("min_to_event", 1440), -60, 1440)),
    )


def simulated_samples(trades: pd.DataFrame, f: pd.DataFrame, p_up: pd.Series) -> pd.DataFrame:
    """Turn backtest trades into training rows (features at the decision bar, label = net win)."""
    if trades is None or trades.empty or "decide_time" not in trades:
        return pd.DataFrame()
    rows = []
    fr = f.reindex(trades.decide_time)
    pu = p_up.reindex(trades.decide_time).fillna(0.5).to_numpy()
    for k, (t, tr) in enumerate(trades.iterrows()):
        x = entry_features(fr.iloc[k], int(tr.side), float(pu[k]), str(tr.kind))
        x.update(time=str(tr.decide_time), win=int(tr.pnl > 0), r=float(tr.r), weight=1.0, source="sim")
        rows.append(x)
    return pd.DataFrame(rows)


def live_samples(store) -> pd.DataFrame:
    try:
        df = pd.read_sql_query("SELECT open_time, features, pnl, sl, open_price, lots FROM trades "
                               "WHERE pnl IS NOT NULL AND features IS NOT NULL", store.conn)
    except Exception:  # noqa: BLE001
        return pd.DataFrame()
    rows = []
    for _, r in df.iterrows():
        try:
            x = json.loads(r.features)
        except (TypeError, ValueError):
            continue
        risk = abs(r.open_price - r.sl) * 100 * r.lots if r.sl else 0
        x.update(time=r.open_time, win=int(r.pnl > 0), r=float(r.pnl / risk) if risk else 0.0,
                 weight=LIVE_WEIGHT, source="live")
        rows.append(x)
    return pd.DataFrame(rows)


class OutcomeModel:
    def __init__(self, pipe=None, threshold: float = 0.0, enabled: bool = False, meta: dict | None = None):
        self.pipe, self.threshold, self.enabled, self.meta = pipe, threshold, enabled, meta or {}

    @staticmethod
    def _fit(df: pd.DataFrame):
        from sklearn.linear_model import LogisticRegression
        from sklearn.pipeline import make_pipeline
        from sklearn.preprocessing import StandardScaler
        pipe = make_pipeline(StandardScaler(), LogisticRegression(C=0.5, max_iter=500))
        pipe.fit(df[OUTCOME_FEATURES].to_numpy(float), df["win"].to_numpy(int),
                 logisticregression__sample_weight=df["weight"].to_numpy(float))
        return pipe

    @classmethod
    def build(cls, samples: pd.DataFrame, say=print) -> "OutcomeModel":
        """Fit on the older 70 % of trades, choose a veto threshold there, and enable the gate only if it
        also improves the newer 30 % it never saw. Then refit on everything for live use."""
        if samples is None or samples.empty or not set(OUTCOME_FEATURES) <= set(samples.columns):
            say("  Self-learning gate: no trades to learn from yet.")
            return cls(None, 0.0, False, dict(samples=0, reason="no trades yet"))
        s = samples.dropna(subset=OUTCOME_FEATURES).sort_values("time")
        if len(s) < 200 or s.win.nunique() < 2:
            say(f"  Self-learning gate: {len(s)} trades so far — needs 200 before it can judge entries.")
            return cls(cls._fit(s) if len(s) >= 30 and s.win.nunique() == 2 else None, 0.0, False,
                       dict(samples=len(s), reason="not enough trades yet"))
        cut = int(len(s) * 0.7)
        tr, va = s.iloc[:cut], s.iloc[cut:]
        pipe = cls._fit(tr)
        p_tr = pipe.predict_proba(tr[OUTCOME_FEATURES].to_numpy(float))[:, 1]
        best_t, best_r = 0.0, tr.r.sum()
        for t in np.arange(0.30, 0.71, 0.02):
            keep = p_tr >= t
            if keep.mean() >= 0.3 and tr.r[keep].sum() > best_r:
                best_t, best_r = float(t), tr.r[keep].sum()
        p_va = pipe.predict_proba(va[OUTCOME_FEATURES].to_numpy(float))[:, 1]
        keep = p_va >= best_t
        base_r, kept_r = float(va.r.mean()), float(va.r[keep].mean()) if keep.any() else float("nan")
        enabled = bool(best_t > 0 and keep.sum() >= 20 and kept_r > base_r and kept_r > 0)
        meta = dict(samples=len(s), live=int((s.source == "live").sum()), threshold=best_t,
                    val_trades=len(va), val_kept=int(keep.sum()), val_r_all=round(base_r, 4),
                    val_r_kept=round(kept_r, 4) if np.isfinite(kept_r) else None, enabled=enabled, updated=_now())
        say(f"  Self-learning gate: {'ON' if enabled else 'off'} — validation {len(va)} trades, avg "
            f"{base_r:+.3f} R → {kept_r:+.3f} R on the {int(keep.sum())} it would keep (threshold {best_t:.2f}).")
        return cls(cls._fit(s), best_t, enabled, meta)

    def allows(self, x: dict) -> tuple[bool, float]:
        if not self.enabled or self.pipe is None:
            return True, float("nan")
        p = float(self.pipe.predict_proba(np.array([[x[k] for k in OUTCOME_FEATURES]], float))[0, 1])
        return p >= self.threshold, p

    def update_with_live(self, store, sim_path: Path):
        """Re-fit after a closed live trade (threshold/enabled stay as validated at the last retrain)."""
        try:
            sim = pd.read_csv(sim_path) if sim_path.exists() else pd.DataFrame()
            allx = pd.concat([sim, live_samples(store)], ignore_index=True)
            if not set(OUTCOME_FEATURES) <= set(allx.columns):
                return
            allx = allx.dropna(subset=OUTCOME_FEATURES)
            if len(allx) >= 30 and allx.win.nunique() == 2:
                self.pipe = self._fit(allx)
                self.meta["live"] = int((allx.source == "live").sum())
                self.meta["refit"] = _now()
        except Exception as e:  # noqa: BLE001
            log.warning("outcome model refit failed: %s", e)

    def save(self, folder: Path):
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "outcome.pkl").write_bytes(pickle.dumps(self.pipe))
        (folder / "outcome.json").write_text(json.dumps(dict(self.meta, threshold=self.threshold,
                                                             enabled=self.enabled), indent=2, default=str))

    @classmethod
    def load(cls, folder: Path) -> "OutcomeModel":
        try:
            meta = json.loads((folder / "outcome.json").read_text())
            pipe = pickle.loads((folder / "outcome.pkl").read_bytes())
            return cls(pipe, float(meta.get("threshold", 0)), bool(meta.get("enabled")), meta)
        except (FileNotFoundError, ValueError, pickle.UnpicklingError, EOFError):
            return cls()


# ------------------------------------------------------------------------------- archive
def archive_model(data_dir: Path):
    """Keep the previous model version before a retrain overwrites it."""
    cur = data_dir / "model"
    meta = cur / "model.json"
    if not meta.exists():
        return
    try:
        stamp = json.loads(meta.read_text()).get("trained_at", _now())[:19].replace(":", "-")
    except ValueError:
        stamp = _now()[:19].replace(":", "-")
    dst = data_dir / "models" / stamp
    if not dst.exists():
        shutil.copytree(cur, dst)
    # keep the last 12 versions
    versions = sorted((data_dir / "models").iterdir())
    for old in versions[:-12]:
        shutil.rmtree(old, ignore_errors=True)


def log_learning(data_dir: Path, entry: dict):
    with (data_dir / "learning_log.jsonl").open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(dict(entry, time=_now()), default=str) + "\n")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()
