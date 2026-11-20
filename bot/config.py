"""Load config.yaml + secrets from environment (.env) into attribute-style objects."""
from __future__ import annotations

import copy
import os
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import yaml

DEFAULT_PATH = Path(__file__).with_name("config.yaml")
TURBO_USER_STRATEGY_KEYS = {"time_stop_minutes", "sl_atr", "tp_atr", "skip_market_open_check"}  # style-preset exits


def _ns(obj: Any) -> Any:
    if isinstance(obj, dict):
        return SimpleNamespace(**{k: _ns(v) for k, v in obj.items()})
    if isinstance(obj, list):
        return [_ns(v) for v in obj]
    return obj


_YAML_CACHE: dict = {}
try:  # libyaml (C) parser when available — ~10× faster than the pure-Python one
    _Loader = yaml.CSafeLoader
except AttributeError:  # pragma: no cover
    _Loader = yaml.SafeLoader


def _yaml(path: Path):
    """Parsed YAML of a file, cached by (mtime, size): the dashboard reads the config several times a second.
    Returns a deep copy so callers can merge/mutate freely."""
    try:
        st = path.stat()
    except OSError:
        return None
    key = str(path)
    hit = _YAML_CACHE.get(key)
    if hit is None or hit[0] != (st.st_mtime_ns, st.st_size):
        hit = ((st.st_mtime_ns, st.st_size), yaml.load(path.read_text(encoding="utf-8"), Loader=_Loader))
        _YAML_CACHE[key] = hit
    return copy.deepcopy(hit[1])


def _load_dotenv(path: Path) -> None:
    """Minimal .env reader (KEY=VALUE lines); does not override existing env vars."""
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def load_config(path: str | Path | None = None, overrides: dict | None = None, use_tuned: bool = True,
                tuned_path: str | Path | None = None) -> SimpleNamespace:
    path = Path(path or os.environ.get("XAU_CONFIG", DEFAULT_PATH))
    raw = _yaml(path)
    data_dir = path.parent / "data"
    tuned = Path(tuned_path) if tuned_path else data_dir / "tuned.yaml"
    user = data_dir / "user.yaml"  # dashboard settings — always win
    if use_tuned and tuned.exists() and _final_mode(raw, _yaml(user) if user.exists() else None, overrides) == "scalp":
        t = _yaml(tuned) or {}   # written by the optimizer: it tunes the SCALPING rules only — every other style
        if _scalp_tuning(t):     # (Perfect, Hyper Max, Signal AI, Basket …) keeps its own tested settings
            t.pop("_tuning", None)
            _deep_update(raw, t)
    if user.exists():
        _deep_update(raw, _yaml(user) or {})
    if overrides:
        _deep_update(raw, copy.deepcopy(overrides))
    if str(raw.get("strategy", {}).get("mode", "scalp")).lower() == "turbo" and isinstance(raw.get("turbo"), dict):
        _deep_update(raw, copy.deepcopy(raw["turbo"]))  # Turbo's own exits / risk (config.yaml `turbo:` section)
        if user.exists():  # explicit dashboard settings still win
            u = _yaml(user) or {}
            _deep_update(raw, {k: v for k, v in u.items() if k != "strategy"})
            # style presets may set their own tested hold time (e.g. Hyper Max 5: 24 h); other user strategy keys
            # (scalping exits, equity-% take profit) must not override Turbo's tested exits
            keep = {k: v for k, v in (u.get("strategy") or {}).items() if k in TURBO_USER_STRATEGY_KEYS}
            if keep:
                _deep_update(raw, {"strategy": keep})
    local = data_dir / "local.json"  # machine-specific: MT5 terminal path remembered at first connect
    if not raw["broker"].get("terminal_path") and local.exists():
        import json
        try:
            tp = json.loads(local.read_text()).get("terminal_path", "")
            if tp and Path(tp).exists():
                raw["broker"]["terminal_path"] = tp
        except ValueError:
            pass
    if overrides:  # explicit overrides always win
        _deep_update(raw, copy.deepcopy(overrides))
    _load_dotenv(path.with_name(".env"))
    cfg = _ns(raw)
    cfg.secrets = SimpleNamespace(
        mt5_login=int(os.environ["MT5_LOGIN"]) if os.environ.get("MT5_LOGIN") else None,
        mt5_password=os.environ.get("MT5_PASSWORD"),
        mt5_server=os.environ.get("MT5_SERVER") or cfg.broker.server or None,
        telegram_token=os.environ.get("TELEGRAM_TOKEN"),
        telegram_chat_id=os.environ.get("TELEGRAM_CHAT_ID"),
    )
    cfg.raw = raw
    raw["_path"] = str(path)
    raw["_overrides"] = overrides or {}
    return cfg


def _scalp_tuning(t: dict) -> bool:
    """A tuned.yaml made for the scalping rules. Files before v3.0.1 have no mode: a retrain while a breakout style was
    active saved that style's own exits (e.g. 40×ATR target, 15 h hold) — far outside the scalping search space."""
    mode = ((t.get("_tuning") or {}).get("mode") if isinstance(t, dict) else None)
    if mode:
        return str(mode).lower() == "scalp"
    st = (t.get("strategy") or {}) if isinstance(t, dict) else {}
    try:
        return float(st.get("tp_atr", 0)) <= 2.0 and float(st.get("sl_atr", 0)) <= 1.5 and \
            float(st.get("time_stop_minutes", 0)) <= 30
    except (TypeError, ValueError):
        return False


def _final_mode(*layers) -> str:
    """strategy.mode after config.yaml → user.yaml → overrides (the last layer that sets it wins)."""
    mode = "scalp"
    for d in layers:
        st = d.get("strategy") if isinstance(d, dict) else None
        if isinstance(st, dict) and st.get("mode"):
            mode = str(st["mode"]).lower()
    return mode


def _deep_update(base: dict, upd: dict) -> None:
    for k, v in upd.items():
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            _deep_update(base[k], v)
        else:
            base[k] = v


def commission_per_side(cfg) -> float:
    """$ per lot per side for the configured account type."""
    t = str(cfg.broker.account_type).upper()
    return {"PRIME": 3.5, "STANDARD": 0.0}.get(t, cfg.backtest.commission_per_side_per_lot)
