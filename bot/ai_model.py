"""LightGBM direction model (§4): training, walk-forward OOS predictions, SHAP importance, drift monitor."""
from __future__ import annotations

import json
from collections import deque
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

from backtest import holdout_range, walk_forward_windows
from features import MODEL_FEATURES, make_label

PARAMS = dict(objective="binary", learning_rate=0.03, num_leaves=31, min_child_samples=300,
              feature_fraction=0.8, bagging_fraction=0.8, bagging_freq=1, lambda_l2=5.0,
              max_bin=127, verbose=-1, num_threads=0, seed=7)


def usable_features(X: pd.DataFrame, max_nan=0.5) -> list[str]:
    """Drop features mostly missing in training (e.g. tick microstructure / correlated markets when the
    broker has no history) so live values never hit branches the model never learned."""
    return [c for c in X.columns if X[c].isna().mean() <= max_nan and X[c].nunique(dropna=True) > 1]


def auc(y: np.ndarray, p: np.ndarray) -> float:
    y, p = np.asarray(y), np.asarray(p)
    pos, neg = y == 1, y == 0
    if pos.sum() == 0 or neg.sum() == 0:
        return float("nan")
    ranks = pd.Series(p).rank().to_numpy()
    return float((ranks[pos].sum() - pos.sum() * (pos.sum() + 1) / 2) / (pos.sum() * neg.sum()))


class DirectionModel:
    def __init__(self, booster=None, features=None, meta=None):
        self.booster, self.features, self.meta = booster, features or [], meta or {}

    # ---------------------------------------------------------------- train / predict
    @classmethod
    def fit(cls, f: pd.DataFrame, cfg, rounds: int = 600) -> "DirectionModel":
        import lightgbm as lgb
        y = make_label(f, cfg.model.label_horizon_m5_bars)
        X = f[[c for c in MODEL_FEATURES if c in f.columns]]
        ok = y.notna() & X["atr1"].notna()
        X, y = X[ok], y[ok]
        feats = usable_features(X)
        X = X[feats]
        cut = int(len(X) * 0.85)  # time-ordered early-stopping split
        dtr = lgb.Dataset(X.iloc[:cut], y.iloc[:cut], free_raw_data=True)
        dva = lgb.Dataset(X.iloc[cut:], y.iloc[cut:], reference=dtr)
        booster = lgb.train(PARAMS, dtr, rounds, valid_sets=[dva],
                            callbacks=[lgb.early_stopping(50, verbose=False)])
        pv = booster.predict(X.iloc[cut:], num_iteration=booster.best_iteration)
        meta = dict(trained_at=datetime.now(timezone.utc).isoformat(), rows=int(len(X)),
                    train_start=str(X.index[0]), train_end=str(X.index[-1]),
                    best_iteration=int(booster.best_iteration or rounds), valid_auc=auc(y.iloc[cut:], pv))
        return cls(booster, feats, meta)

    def predict_proba(self, f: pd.DataFrame) -> pd.Series:
        X = f.reindex(columns=self.features)
        p = self.booster.predict(X, num_iteration=self.meta.get("best_iteration"))
        return pd.Series(p, index=f.index, name="p_up")

    def importance(self, f: pd.DataFrame, sample: int = 20000) -> pd.Series:
        """Mean |SHAP| per feature (LightGBM's built-in TreeSHAP via pred_contrib)."""
        X = f.reindex(columns=self.features).dropna(subset=["atr1"] if "atr1" in self.features else None)
        if len(X) > sample:
            X = X.sample(sample, random_state=7)
        contrib = self.booster.predict(X, pred_contrib=True, num_iteration=self.meta.get("best_iteration"))
        return pd.Series(np.abs(contrib[:, :-1]).mean(0), index=self.features).sort_values(ascending=False)

    # ---------------------------------------------------------------- persistence
    def save(self, folder: Path):
        folder.mkdir(parents=True, exist_ok=True)
        self.booster.save_model(str(folder / "model.txt"))
        (folder / "model.json").write_text(json.dumps(dict(features=self.features, **self.meta), indent=2, default=str))

    @classmethod
    def load(cls, folder: Path) -> "DirectionModel":
        import lightgbm as lgb
        meta = json.loads((folder / "model.json").read_text())
        return cls(lgb.Booster(model_file=str(folder / "model.txt")), meta.pop("features"), meta)


def signal_from_proba(p_up: pd.Series) -> tuple[pd.Series, pd.Series]:
    """ai_dir (+1 up / -1 down) and confidence = max(P(up), P(down))."""
    return pd.Series(np.where(p_up >= 0.5, 1, -1), index=p_up.index), np.maximum(p_up, 1 - p_up)


def walk_forward_predictions(f: pd.DataFrame, cfg, log=print) -> tuple[pd.Series, list[dict]]:
    """Out-of-sample P(up) over every walk-forward test window (holdout excluded)."""
    bt = cfg.backtest
    preds, folds = [], []
    for k, (a, b, c, d) in enumerate(walk_forward_windows(f.index, bt.wf_window_months, bt.wf_train_frac,
                                                          bt.holdout_months)):
        tr, te = f[(f.index >= a) & (f.index < b)], f[(f.index >= c) & (f.index < d)]
        if len(tr) < 20000 or len(te) < 1000:
            continue
        m = DirectionModel.fit(tr, cfg)
        p = m.predict_proba(te)
        y = make_label(te, cfg.model.label_horizon_m5_bars)
        ok = y.notna()
        folds.append(dict(fold=k, train=f"{a.date()}→{b.date()}", test=f"{c.date()}→{d.date()}",
                          test_auc=round(auc(y[ok], p[ok]), 4),
                          test_acc=round(float(((p[ok] >= .5) == y[ok]).mean()), 4)))
        log(f"  fold {k}: train {a.date()}→{b.date()}  test {c.date()}→{d.date()}  AUC {folds[-1]['test_auc']}")
        preds.append(p)
    return (pd.concat(preds) if preds else pd.Series(dtype=float)), folds


def holdout_predictions(f: pd.DataFrame, cfg) -> tuple[pd.Series, DirectionModel, tuple]:
    ho_a, ho_b = holdout_range(f.index, cfg.backtest.holdout_months)
    if (f.index < ho_a).sum() < 20000:  # short history: fall back to a 70/30 time split
        ho_a = f.index[int(len(f) * 0.7)]
    m = DirectionModel.fit(f[f.index < ho_a], cfg)
    return m.predict_proba(f[f.index >= ho_a]), m, (ho_a, ho_b)


class DriftMonitor:
    """Pause entries when live win rate over the last N trades is > tolerance points below backtest."""

    def __init__(self, backtest_win_rate: float, window: int = 50, tolerance_pct: float = 15):
        self.bt, self.window, self.tol = backtest_win_rate, window, tolerance_pct
        self.results: deque[int] = deque(maxlen=window)

    def add(self, pnl: float) -> bool:
        self.results.append(1 if pnl > 0 else 0)
        return self.drifted

    @property
    def live_win_rate(self) -> float:
        return 100 * sum(self.results) / len(self.results) if self.results else float("nan")

    @property
    def drifted(self) -> bool:
        return len(self.results) >= self.window and self.live_win_rate < self.bt - self.tol
