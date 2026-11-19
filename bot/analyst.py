"""Market analyst: "how does the market look and what will probably happen next?"

1. M15Analyst — a LightGBM model that reads the 15-minute chart (trend, momentum, volatility, position in the
   day's range), the hourly trend, other markets and the news calendar, and predicts the next 60 minutes
   (UP/DOWN probability). Trained walk-forward, so backtests only ever see out-of-sample forecasts;
   retrained every weekend on all saved data (it trains on itself as the data grows).

2. NewsAgent (optional, needs an Anthropic API key) — Claude reads the CURRENT gold news with live web search,
   together with a numeric summary of the 15-minute chart and upcoming high-impact events, and returns a
   structured outlook: bias, confidence, trade conditions (normal / caution / avoid), drivers, risks.
   Every call is stored and graded 60 minutes later; its recent track record is fed back into the next prompt
   so it calibrates itself.

Both feed the entry decision: a strongly opposing outlook vetoes a trade; "avoid" pauses new entries.
"""
from __future__ import annotations

import json
import logging
import sqlite3
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pandas as pd

from features import adx, atr, bb_pctb, ema, macd, resample, rsi

log = logging.getLogger("analyst")
HORIZON_BARS = 4  # 4 × M15 = the next 60 minutes


# ============================================================================ M15 model
def m15_frame(m1: pd.DataFrame, correlated: dict | None = None, events: pd.DatetimeIndex | None = None) -> pd.DataFrame:
    """Features per CLOSED 15-minute bar (index = bar open time)."""
    b = resample(m1, 15)
    c = b["close"]
    a = atr(b, 14)
    x = pd.DataFrame(index=b.index)
    for k in (1, 2, 4, 8, 16):
        x[f"r{k}"] = c.pct_change(k, fill_method=None) * 1e4
    x["rsi"] = rsi(c, 14)
    x["macd_h"] = macd(c)[2] / a
    x["atr_bps"] = a / c * 1e4
    x["atr_rel"] = a / a.rolling(96, min_periods=20).mean()
    x["pctb"] = bb_pctb(c)
    x["adx"] = adx(b, 14)
    e20, e50 = ema(c, 20), ema(c, 50)
    x["ema20_d"] = (c - e20) / a
    x["ema50_d"] = (c - e50) / a
    x["ema20_s"] = (e20 - e20.shift(4)) / a
    # hourly trend, visible to an M15 bar only once the H1 bar has CLOSED (merge on close times — no look-ahead)
    h1 = resample(m1, 60)["close"]
    h1_s = (ema(h1, 20) - ema(h1, 20).shift(3)).rename("v").to_frame()
    h1_s.index = h1_s.index + pd.Timedelta(minutes=60)
    left = pd.DataFrame(index=b.index + pd.Timedelta(minutes=15))
    x["h1_trend"] = pd.merge_asof(left, h1_s, left_index=True, right_index=True, direction="backward")["v"].values / a
    day = (b.index - pd.Timedelta(hours=22)).floor("D")
    hi, lo = b["high"].groupby(day).cummax(), b["low"].groupby(day).cummin()
    x["day_pos"] = (c - lo) / (hi - lo).replace(0, np.nan)
    x["body"] = (b["close"] - b["open"]) / a
    x["wick_up"] = (b["high"] - b[["open", "close"]].max(axis=1)) / a
    x["wick_dn"] = (b[["open", "close"]].min(axis=1) - b["low"]) / a
    x["vol_rel"] = b["tick_volume"] / b["tick_volume"].rolling(96, min_periods=20).mean()
    x["hour"], x["dow"] = b.index.hour, b.index.dayofweek
    if events is not None and len(events):
        t = (b.index + pd.Timedelta(minutes=15)).values.astype("datetime64[ns]")
        ev = np.sort(events.values.astype("datetime64[ns]"))
        pos = np.searchsorted(ev, t)
        nxt = np.where(pos < len(ev), (ev[np.minimum(pos, len(ev) - 1)] - t) / np.timedelta64(1, "m"), 1e4)
        prv = np.where(pos > 0, (t - ev[np.maximum(pos - 1, 0)]) / np.timedelta64(1, "m"), 1e4)
        x["min_to_event"], x["min_since_event"] = np.clip(nxt, 0, 1e4), np.clip(prv, 0, 1e4)
    for name, s in (correlated or {}).items():
        if s is None or len(s) == 0 or name.startswith("_"):
            continue
        s15 = s[~s.index.duplicated()].sort_index().resample("15min", label="left", closed="left").last()
        x[f"{name}_r1"] = s15.pct_change(1, fill_method=None).reindex(b.index) * 1e4
        x[f"{name}_r4"] = s15.pct_change(4, fill_method=None).reindex(b.index) * 1e4
    x["close"] = c
    return x.astype("float32").assign(close=c.astype("float64"))


def m15_label(x: pd.DataFrame) -> pd.Series:
    fut = x["close"].shift(-HORIZON_BARS)
    return (fut > x["close"]).astype(float).where(fut.notna())


class M15Analyst:
    PARAMS = dict(objective="binary", learning_rate=0.03, num_leaves=15, min_child_samples=200,
                  feature_fraction=0.8, bagging_fraction=0.8, bagging_freq=1, lambda_l2=5.0, verbose=-1, seed=11)

    def __init__(self, booster=None, features=None, meta=None):
        self.booster, self.features, self.meta = booster, features or [], meta or {}

    @classmethod
    def fit(cls, x: pd.DataFrame, rounds: int = 400) -> "M15Analyst":
        import lightgbm as lgb
        y = m15_label(x)
        feats = [c for c in x.columns if c != "close" and x[c].notna().mean() > 0.5]
        ok = y.notna()
        X, y = x.loc[ok, feats], y[ok]
        cut = int(len(X) * 0.85)
        dtr = lgb.Dataset(X.iloc[:cut], y.iloc[:cut])
        dva = lgb.Dataset(X.iloc[cut:], y.iloc[cut:], reference=dtr)
        b = lgb.train(cls.PARAMS, dtr, rounds, valid_sets=[dva], callbacks=[lgb.early_stopping(40, verbose=False)])
        from ai_model import auc
        pv = b.predict(X.iloc[cut:], num_iteration=b.best_iteration)
        return cls(b, feats, dict(rows=len(X), best_iteration=int(b.best_iteration or rounds),
                                  valid_auc=auc(y.iloc[cut:].to_numpy(), pv),
                                  trained_at=datetime.now(timezone.utc).isoformat()))

    def predict(self, x: pd.DataFrame) -> pd.Series:
        return pd.Series(self.booster.predict(x.reindex(columns=self.features),
                                              num_iteration=self.meta.get("best_iteration")), index=x.index)

    def save(self, folder: Path):
        folder.mkdir(parents=True, exist_ok=True)
        self.booster.save_model(str(folder / "m15.txt"))
        (folder / "m15.json").write_text(json.dumps(dict(features=self.features, **self.meta), indent=2, default=str))

    @classmethod
    def load(cls, folder: Path) -> "M15Analyst | None":
        try:
            import lightgbm as lgb
            meta = json.loads((folder / "m15.json").read_text())
            return cls(lgb.Booster(model_file=str(folder / "m15.txt")), meta.pop("features"), meta)
        except (FileNotFoundError, ValueError):
            return None


def walk_forward_m15(x: pd.DataFrame, months: int = 6, min_train_months: int = 12, say=print) -> pd.Series:
    """Out-of-sample P(up in the next 60 min) per M15 bar: expanding train, 6-month test blocks."""
    from ai_model import auc
    out = []
    start = x.index[0] + pd.DateOffset(months=min_train_months)
    while start < x.index[-1]:
        end = start + pd.DateOffset(months=months)
        tr, te = x[x.index < start], x[(x.index >= start) & (x.index < end)]
        if len(tr) > 5000 and len(te) > 200:
            m = M15Analyst.fit(tr)
            p = m.predict(te)
            y = m15_label(te)
            ok = y.notna()
            say(f"  M15 analyst {start:%Y-%m} → {end:%Y-%m}: AUC {auc(y[ok].to_numpy(), p[ok].to_numpy()):.3f}")
            out.append(p)
        start = end
    return pd.concat(out) if out else pd.Series(dtype=float)


def outlook_for_m1(p15: pd.Series, m1_index: pd.DatetimeIndex) -> pd.DataFrame:
    """Map M15 forecasts onto M1 rows, visible only after the M15 bar closed (no look-ahead)."""
    from features import align_closed
    al = align_closed(m1_index, pd.DataFrame({"p": p15}), 15, "o_")
    p = al["o_p"].to_numpy(float)
    return pd.DataFrame({"outlook_dir": np.where(np.isnan(p), 0, np.where(p >= 0.5, 1, -1)),
                         "outlook_conf": np.where(np.isnan(p), 0.0, np.maximum(p, 1 - p))}, index=m1_index)


# ============================================================================ news agent
SCHEMA = {
    "type": "object",
    "properties": {
        "bias": {"type": "string", "enum": ["up", "down", "neutral"]},
        "confidence": {"type": "number"},
        "trade_conditions": {"type": "string", "enum": ["normal", "caution", "avoid"]},
        "summary": {"type": "string"},
        "drivers": {"type": "array", "items": {"type": "string"}},
        "risks": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["bias", "confidence", "trade_conditions", "summary", "drivers", "risks"],
    "additionalProperties": False,
}

SYSTEM = """You are a gold (XAU/USD) intraday market analyst supporting an automated scalping bot on a demo account.
Every 15 minutes you get a numeric snapshot of the 15-minute chart, the bot's own model forecast, upcoming
high-impact US events, and your own recent track record. Search the web for the latest gold-relevant news
(Fed and rate expectations, US data surprises, dollar and yields, geopolitics, central-bank buying) from the
last few hours, then judge the most likely direction of XAU/USD over the NEXT 60 MINUTES.

Be calibrated: confidence 0.5 means no edge; above 0.7 only with a clear, fresh catalyst aligned with price
action. Use trade_conditions "avoid" when a major release or breaking headline makes the next hour a
coin-flip with outsized risk, "caution" for elevated uncertainty, otherwise "normal". Learn from your track
record: if recent calls were poor, lower your confidence. Keep the summary to two sentences."""


class NewsAgent:
    def __init__(self, data_dir: Path, model: str = "claude-opus-5-5", interval_min: int = 15, effort: str = "medium"):
        self.data_dir, self.model, self.interval, self.effort = data_dir, model, interval_min, effort
        self.db = data_dir / "xau.db"
        self.latest: dict | None = None
        self.busy = False
        self.last_run = 0.0
        self._init_db()

    def _init_db(self):
        con = sqlite3.connect(self.db)
        con.execute("CREATE TABLE IF NOT EXISTS analyst_calls (time TEXT PRIMARY KEY, price REAL, bias TEXT, "
                    "confidence REAL, conditions TEXT, summary TEXT, payload TEXT, price_after REAL, correct INTEGER)")
        con.commit()
        con.close()

    @staticmethod
    def available() -> bool:
        import os
        try:
            import anthropic  # noqa: F401
        except ImportError:
            return False
        return bool(os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN"))

    def due(self) -> bool:
        return not self.busy and time.time() - self.last_run >= self.interval * 60

    def track_record(self, price_now: float | None = None, now: datetime | None = None) -> dict:
        """Grade calls older than 60 min against the price at the time they were graded; return stats."""
        con = sqlite3.connect(self.db)
        now = now or datetime.now(timezone.utc)
        if price_now is not None:
            for t, p0, bias in con.execute("SELECT time, price, bias FROM analyst_calls WHERE correct IS NULL").fetchall():
                if datetime.fromisoformat(t) <= now - timedelta(minutes=60) and bias != "neutral":
                    ok = int((price_now - p0) * (1 if bias == "up" else -1) > 0)
                    con.execute("UPDATE analyst_calls SET price_after=?, correct=? WHERE time=?", (price_now, ok, t))
            con.commit()
        rows = con.execute("SELECT time, bias, confidence, price, price_after, correct FROM analyst_calls "
                           "WHERE correct IS NOT NULL ORDER BY time DESC LIMIT 40").fetchall()
        con.close()
        n = len(rows)
        return dict(graded=n, hit_rate=round(sum(r[5] for r in rows) / n, 3) if n else None,
                    recent=[dict(time=r[0][:16], bias=r[1], conf=r[2], move=round((r[4] or 0) - r[3], 2),
                                 correct=bool(r[5])) for r in rows[:8]])

    def run_async(self, snapshot: dict):
        if self.busy:
            return
        self.busy, self.last_run = True, time.time()
        threading.Thread(target=self._run, args=(snapshot,), daemon=True).start()

    def _run(self, snapshot: dict):
        try:
            res = self.analyse(snapshot)
            if res:
                self.latest = res
        except Exception as e:  # noqa: BLE001
            log.warning("news agent failed: %s", e)
        finally:
            self.busy = False

    def analyse(self, snapshot: dict) -> dict | None:
        import anthropic
        track = self.track_record(snapshot.get("price"))
        prompt = ("Market snapshot (UTC):\n" + json.dumps(snapshot, default=str, indent=1) +
                  "\n\nYour recent track record (graded 60 min after each call):\n" + json.dumps(track, indent=1) +
                  "\n\nSearch the latest gold news, then give your outlook for the next 60 minutes.")
        client = anthropic.Anthropic()
        messages = [{"role": "user", "content": prompt}]
        resp = None
        for _ in range(4):  # server tools may pause a long turn; continue it
            resp = client.beta.messages.create(
                model=self.model, max_tokens=16000, system=SYSTEM, messages=messages,
                tools=[{"type": "web_search_20260209", "name": "web_search", "max_uses": 4}],
                output_config={"effort": self.effort, "format": {"type": "json_schema", "schema": SCHEMA}},
                betas=["server-side-fallback-2026-07-01"], fallbacks="default",
            )
            if resp.stop_reason != "pause_turn":
                break
            messages = [{"role": "user", "content": prompt}, {"role": "assistant", "content": resp.content}]
        if resp is None or resp.stop_reason == "refusal":
            log.warning("news agent: no answer (%s)", getattr(resp, "stop_reason", None))
            return None
        text = "".join(b.text for b in resp.content if b.type == "text")
        try:
            out = json.loads(text)
        except ValueError:
            log.warning("news agent returned non-JSON output")
            return None
        out["confidence"] = float(min(max(out.get("confidence", 0.5), 0.0), 1.0))
        out["time"] = datetime.now(timezone.utc).isoformat()
        out["price"] = snapshot.get("price")
        con = sqlite3.connect(self.db)
        con.execute("INSERT OR REPLACE INTO analyst_calls (time, price, bias, confidence, conditions, summary, payload) "
                    "VALUES (?,?,?,?,?,?,?)", (out["time"], out["price"], out["bias"], out["confidence"],
                                               out["trade_conditions"], out["summary"], json.dumps(out)))
        con.commit()
        con.close()
        log.info("news agent: %s %.2f %s — %s", out["bias"], out["confidence"], out["trade_conditions"], out["summary"])
        return out


def chart_snapshot(x15: pd.DataFrame, p_up: float | None, events_next: list[str], price: float) -> dict:
    """Compact numeric description of the latest 15-minute chart for the news agent."""
    r = x15.iloc[-1]
    last8 = x15["close"].iloc[-9:].round(2).tolist()
    return dict(time=str(x15.index[-1] + pd.Timedelta(minutes=15)), price=round(price, 2), last_8_m15_closes=last8,
                ret_15m_bps=round(float(r["r1"]), 1), ret_1h_bps=round(float(r["r4"]), 1),
                ret_4h_bps=round(float(r["r16"]), 1), rsi_14=round(float(r["rsi"]), 1), adx=round(float(r["adx"]), 1),
                atr_bps=round(float(r["atr_bps"]), 1), position_in_day_range=round(float(r["day_pos"]), 2),
                hourly_trend=round(float(r["h1_trend"]), 2),
                model_p_up_next_60min=None if p_up is None else round(p_up, 3), upcoming_events=events_next[:5])
