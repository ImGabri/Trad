"""Hard risk rules (§5) as pure functions + a small daily state machine. Shared by backtest and live."""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date


def round_lots(lots: float, step: float, vmin: float, vmax: float) -> float:
    lots = math.floor(lots / step + 1e-9) * step
    return round(min(lots, vmax), 2) if lots >= vmin - 1e-9 else 0.0


def position_size(equity: float, sl_distance: float, contract_size: float, risk_pct: float,
                  step=0.01, vmin=0.01, vmax=5.0, commission_per_side=0.0, min_lot_max_risk_pct: float = 0.0) -> float:
    """Lots such that SL loss (+ round-trip commission) ≤ risk_pct of equity.
    If even the minimum lot is too big, trade the minimum lot only when its risk ≤ min_lot_max_risk_pct
    (small-account mode, e.g. a €20 demo); otherwise return 0 (skip)."""
    if sl_distance <= 0 or equity <= 0:
        return 0.0
    risk_amount = equity * risk_pct / 100
    loss_per_lot = sl_distance * contract_size + 2 * commission_per_side
    lots = round_lots(risk_amount / loss_per_lot, step, vmin, vmax)
    if lots == 0 and min_lot_max_risk_pct > 0 and vmin * loss_per_lot <= equity * min_lot_max_risk_pct / 100:
        return vmin
    return lots


def min_required_equity(price: float, sl_distance: float, contract_size: float, leverage: float,
                        min_lot: float = 0.01, margin_min_lot: float | None = None) -> float:
    """margin for 0.01 lots + SL loss for 0.01 lots (§5 survival rule). Callers pass the SL distance at TYPICAL
    volatility (24 h median ATR) so a single news spike cannot latch the hard stop."""
    margin = margin_min_lot if margin_min_lot is not None else price * contract_size * min_lot / leverage
    return margin + sl_distance * contract_size * min_lot


def survival_breached(equity: float, required: float, mult: float) -> bool:
    return equity <= required * mult


def spread_ok(spread: float, avg_spread: float, mult: float) -> bool:
    return avg_spread <= 0 or spread <= mult * avg_spread


@dataclass
class RiskState:
    """Tracks per-UTC-day counters and hard-stop latches."""
    cfg: object
    day: date | None = None
    day_start_equity: float = 0.0
    trades_today: int = 0
    daily_halt: bool = False
    hard_stop: bool = False
    drift_pause: bool = False
    reasons: list = field(default_factory=list)

    def on_bar(self, day: date, equity: float):
        if day != self.day:
            self.day, self.day_start_equity, self.trades_today, self.daily_halt = day, equity, 0, False

    def check_daily_loss(self, equity: float) -> bool:
        lim = self.day_start_equity * self.cfg.risk.daily_loss_limit_pct / 100
        if not self.daily_halt and self.day_start_equity - equity >= lim:
            self.daily_halt = True
            self.reasons.append(("daily_loss_limit", self.day))
        return self.daily_halt

    def can_open(self, open_positions: int, side: int, open_sides_pnl: list[tuple[int, float]]) -> tuple[bool, str]:
        r = self.cfg.risk
        if self.hard_stop:
            return False, "hard_stop"
        if self.daily_halt:
            return False, "daily_loss_halt"
        if self.drift_pause:
            return False, "model_drift_pause"
        if open_positions >= r.max_concurrent:
            return False, "max_concurrent"
        if self.trades_today >= r.max_daily_trades:
            return False, "max_daily_trades"
        if not r.allow_averaging_down and any(s == side and pnl < 0 for s, pnl in open_sides_pnl):
            return False, "no_averaging_down"
        return True, ""
