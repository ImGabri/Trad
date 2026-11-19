"""Load config.yaml + secrets from environment (.env) into attribute-style objects."""
from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import yaml

DEFAULT_PATH = Path(__file__).with_name("config.yaml")


def _ns(obj: Any) -> Any:
    if isinstance(obj, dict):
        return SimpleNamespace(**{k: _ns(v) for k, v in obj.items()})
    if isinstance(obj, list):
        return [_ns(v) for v in obj]
    return obj


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
    raw = yaml.safe_load(path.read_text())
    data_dir = path.parent / "data"
    tuned = Path(tuned_path) if tuned_path else data_dir / "tuned.yaml"
    if use_tuned and tuned.exists():  # written by the optimizer
        t = yaml.safe_load(tuned.read_text()) or {}
        t.pop("_tuning", None)
        _deep_update(raw, t)
    user = data_dir / "user.yaml"  # dashboard settings — always win
    if user.exists():
        _deep_update(raw, yaml.safe_load(user.read_text()) or {})
    local = data_dir / "local.json"  # machine-specific: MT5 terminal path remembered at first connect
    if not raw["broker"].get("terminal_path") and local.exists():
        import json
        try:
            tp = json.loads(local.read_text()).get("terminal_path", "")
            if tp and Path(tp).exists():
                raw["broker"]["terminal_path"] = tp
        except ValueError:
            pass
    if overrides:
        _deep_update(raw, overrides)
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
