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


def load_config(path: str | Path | None = None, overrides: dict | None = None) -> SimpleNamespace:
    path = Path(path or os.environ.get("XAU_CONFIG", DEFAULT_PATH))
    raw = yaml.safe_load(path.read_text())
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
