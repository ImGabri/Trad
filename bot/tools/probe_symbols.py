"""Run on the Windows VPS with the PUPrime MT5 terminal open and logged in:

    python tools/probe_symbols.py            (writes probe_report.json next to this script)

Confirms: gold symbol + contract spec, which correlated symbols (DXY / US10Y / S&P) exist,
server UTC offset, live spread, and how much M1/tick history the broker serves (§8–9 need 3 years).
"""
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import MetaTrader5 as mt5  # noqa: E402

from config import load_config  # noqa: E402
from data import MT5Connection  # noqa: E402


def main():
    cfg = load_config()
    conn = MT5Connection(cfg)
    if not conn.connect():
        sys.exit(f"MT5 connect failed: {mt5.last_error()}")
    acc, term = mt5.account_info(), mt5.terminal_info()
    gold = conn.resolve_gold(cfg.symbols.primary)
    suffix = gold[len(cfg.symbols.primary):]
    rep = {"account": dict(login=acc.login, server=acc.server, company=acc.company, leverage=acc.leverage,
                           currency=acc.currency, trade_mode=acc.trade_mode, margin_mode=acc.margin_mode,
                           demo=acc.trade_mode == mt5.ACCOUNT_TRADE_MODE_DEMO),
           "terminal": dict(name=term.name, build=term.build, algo_trading=term.trade_allowed, ping_ms=term.ping_last / 1000),
           "server_utc_offset_h": conn.server_offset.total_seconds() / 3600,
           "gold": conn.symbol_spec(gold), "correlated": {}, "history": {}}
    tick = mt5.symbol_info_tick(gold)
    if tick:
        rep["gold"]["live_spread_usd"] = round(tick.ask - tick.bid, 3)
    rep["gold"]["margin_0.01_buy"] = mt5.order_calc_margin(mt5.ORDER_TYPE_BUY, gold, 0.01, tick.ask) if tick else None
    for key in ("dxy", "us10y", "spx"):
        rep["correlated"][key] = conn.resolve_symbol(getattr(cfg.symbols, key), suffix)
    # anything that looks related, so we can extend the alias lists
    pats = ("USD", "DX", "10Y", "TNOTE", "BOND", "500", "SPX", "NAS", "US30", "VIX", "XAG", "XAU")
    rep["all_index_like_symbols"] = sorted(s.name for s in mt5.symbols_get()
                                           if any(p in s.name.upper() for p in pats) and len(s.name) <= 12)[:200]
    now = datetime.now(timezone.utc)
    for tf in ("M1", "M5", "M15", "H1"):
        df = conn.rates_range(gold, tf, now - timedelta(days=int(365.25 * 3.5)), now, chunk_days=60)
        rep["history"][tf] = dict(first=str(df.index.min()), last=str(df.index.max()), bars=len(df))
    t = conn.ticks_range(gold, now - timedelta(days=400), now - timedelta(days=399))
    rep["history"]["ticks_400d_ago_1day"] = len(t)
    out = Path(__file__).with_name("probe_report.json")
    out.write_text(json.dumps(rep, indent=2, default=str))
    print(json.dumps(rep, indent=2, default=str))
    print(f"\nSaved {out}")
    conn.shutdown()


if __name__ == "__main__":
    main()
