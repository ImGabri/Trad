"""PU Prime MT5 data and demo-only market requests. No credentials in the program."""
from __future__ import annotations

import hashlib
import math
import os
import re
import time
from contextlib import contextmanager
from datetime import datetime, timezone

import numpy as np
import pandas as pd

from strategy import DAY, MINUTE, quote_frame

MAGIC = 82540931
MARKETS = {"silver": ["XAGUSD"], "eurusd": ["EURUSD"], "usdjpy": ["USDJPY"],
           "dollar_index": ["USDX", "DXY", "USDIndex", "USIndex", "DOLLAR_INDX"],
           "sp500": ["SP500", "US500", "SPX500", "SP500ft"], "wti": ["USOUSD", "USOIL", "XTIUSD", "CL-OIL"],
           "vix": ["VIX", "VIXIndex"], "nasdaq100": ["NAS100", "USTEC", "NASCOMP", "US100"]}


def now_ms():
    return time.time_ns() // 1_000_000


def utc(ms):
    return datetime.fromtimestamp(ms/1000, tz=timezone.utc)


def account(api, expected=None, trading=False):
    a, terminal = api.account_info(), api.terminal_info()
    if a is None or terminal is None or not terminal.connected:
        raise RuntimeError("Open the PU Prime MT5 desktop terminal and log in to the demo.")
    if a.trade_mode != getattr(api, "ACCOUNT_TRADE_MODE_DEMO", 0):
        raise RuntimeError("This program requires an MT5 account reported as DEMO.")
    broker = re.sub(r"[^a-z0-9]", "", (a.server + " " + a.company).lower())
    if not any(name in broker for name in ("puprime", "pacificunion")):
        raise RuntimeError("The terminal's broker/server identity does not identify PU Prime.")
    if expected is not None and (a.login, a.server) != expected:
        raise RuntimeError("The selected terminal account changed. Restart on the original demo.")
    if trading and (not a.trade_allowed or not a.trade_expert or not terminal.trade_allowed or terminal.tradeapi_disabled):
        raise RuntimeError("Enable algorithmic trading and external Python trading in this demo terminal.")
    return a


def account_key(identity):
    return hashlib.sha256(f"{identity[0]}|{identity[1]}".encode()).hexdigest()[:24]


def resolve_gold(api, selected, all_symbols):
    pattern = re.compile(r"XAUUSD(?:[._-][a-z][a-z0-9]{0,7}|m)?", re.I)
    options = [i for i in all_symbols if pattern.fullmatch(i.name)
               and i.trade_mode == getattr(api, "SYMBOL_TRADE_MODE_FULL", 4)
               and str(i.currency_profit).upper() in ("USD", "USC")]
    if selected.lower() != "auto":
        options = [i for i in options if i.name.casefold() == selected.casefold()]
    elif len(options) > 1:
        visible = [i for i in options if i.visible]
        if len(visible) == 1:
            options = visible
    if len(options) != 1:
        names = ", ".join(i.name for i in options) or "(none)"
        raise ValueError(f"Set symbol in settings.json to your tradable XAUUSD symbol from Market Watch. Candidates: {names}")
    gold = options[0].name
    if not api.symbol_select(gold, True):
        raise RuntimeError("MT5 could not select " + gold)
    return gold


def markets_for(api, gold, all_symbols):
    suffix = gold[len("XAUUSD"):]
    names = {i.name.casefold(): i.name for i in all_symbols}
    selected = {}
    for key, aliases in MARKETS.items():
        candidates = [base + suff for suff in dict.fromkeys([suffix, ""]) for base in aliases]
        for alias in candidates:
            actual = names.get(alias.casefold())
            if actual and api.symbol_select(actual, True):
                selected[key] = actual
                break
    return selected


@contextmanager
def connection(settings, terminal_path=None):
    try:
        import MetaTrader5 as mt
    except ImportError as exc:
        raise RuntimeError("The broker connector needs Windows x64 with MetaTrader5 installed. Offline replay also works on Linux/macOS.") from exc
    path = terminal_path or os.environ.get("MT5_PATH")
    ok = mt.initialize(path, timeout=15000) if path else mt.initialize(timeout=15000)
    if not ok:
        raise RuntimeError(f"MT5 connection failed ({mt.last_error()[0]}). Check the open terminal or set MT5_PATH.")
    try:
        a = account(mt)
        symbols = mt.symbols_get()
        if not symbols:
            raise RuntimeError("The terminal returned no symbols.")
        gold = resolve_gold(mt, settings["symbol"], symbols)
        yield mt, (a.login, a.server), gold, markets_for(mt, gold, symbols)
    finally:
        mt.shutdown()


def tick_data(api, symbol, start, end):
    """Read exact half-open millisecond ranges even when terminal dates round to seconds."""
    chunks = []
    for left in range(start, end, 6*3_600_000):
        right = min(end, left + 6*3_600_000)
        rows = api.copy_ticks_range(symbol, utc(left//1000*1000), utc(((right+999)//1000)*1000), api.COPY_TICKS_INFO)
        if rows is None:
            raise RuntimeError(f"MT5 quote-history request failed for {symbol} ({api.last_error()[0]}).")
        if len(rows):
            stamp = rows["time_msc"]
            keep = (stamp >= left) & (stamp < right)
            if keep.any():
                chunks.append(pd.DataFrame({k:rows[v][keep] for k,v in (("ms","time_msc"),("bid","bid"),("ask","ask"))}))
    if not chunks:
        return pd.DataFrame(columns=["ms","bid","ask"])
    return quote_frame(pd.concat(chunks, ignore_index=True))


def market_data(api, markets, start, end):
    frames = []
    for name, symbol in markets.items():
        rows = api.copy_rates_range(symbol, api.TIMEFRAME_M1, utc(start), utc(end))
        if rows is None or not len(rows):
            continue
        known = rows["time"].astype("int64")*1000 + MINUTE
        # A market's close becomes available at the end of its minute.
        ok = (known <= end) & np.isfinite(rows["close"]) & (rows["close"] > 0)
        if ok.any():
            frames.append(pd.DataFrame({"known_ms": known[ok], "series": name, "value": rows["close"][ok]}))
    return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame(columns=["known_ms","series","value"])


def source_data(api, identity, symbol, markets):
    account(api, identity)
    info = api.symbol_info(symbol)
    return {"format": 1, "kind": "PUPrime-MT5", "server": identity[1], "symbol": symbol,
            "context_symbols": markets, "point": info.point, "tick_size": info.trade_tick_size,
            "contract_oz_per_lot": info.trade_contract_size, "min_lots": info.volume_min, "lot_step": info.volume_step}


def quote(api, symbol, max_age):
    t = api.symbol_info_tick(symbol)
    if t is None or not (math.isfinite(t.bid) and math.isfinite(t.ask) and t.ask >= t.bid > 0):
        return None
    if not -250 <= now_ms() - t.time_msc <= max_age*1000:
        return None
    return t


def exposure(api, symbol):
    positions, orders = api.positions_get(symbol=symbol), api.orders_get(symbol=symbol)
    if positions is None or orders is None:
        raise RuntimeError("MT5 could not verify positions and working orders.")
    return positions, orders


def brackets(info, side, entry, distance, multiple):
    tick = float(info.trade_tick_size or info.point)
    if not math.isfinite(tick) or tick <= 0:
        raise ValueError("Invalid price increment in contract specification.")
    lower = math.floor if side == 1 else math.ceil
    upper = math.ceil if side == 1 else math.floor
    sl = round(lower((entry-side*distance)/tick)*tick, info.digits)
    tp = round(upper((entry+side*distance*multiple)/tick)*tick, info.digits)
    return sl, tp


def size(api, a, info, order_type, side, entry, sl, s, demo):
    # Price-offset conversion asks MT5 to express costs and stop risk in account currency.
    pad = s["commission_roundtrip_usd_oz"] + 2*s["slippage_usd_oz_each_side"]
    unit_volume = float(info.volume_min)
    if not all(math.isfinite(v) and v > 0 for v in (unit_volume,info.volume_step,info.volume_max,a.equity)):
        return 0.
    loss = api.order_calc_profit(order_type, info.name, unit_volume, entry, sl - side*pad)
    if loss is None or not math.isfinite(loss) or loss >= 0:
        return 0.
    max_volume = min(a.equity*demo["risk_fraction"]*unit_volume/abs(loss), info.volume_max, demo["max_lots"])
    if max_volume + 1e-12 < unit_volume:
        return 0.
    steps = math.floor((max_volume-unit_volume)/info.volume_step + 1e-10)
    return round(unit_volume + steps*info.volume_step, 8)


def deal_request(api, info, side, price, volume, tag, demo):
    market = getattr(api, "SYMBOL_TRADE_EXECUTION_MARKET", 2)
    if info.filling_mode & 1:
        fill = api.ORDER_FILLING_FOK
    elif info.filling_mode & 2:
        fill = api.ORDER_FILLING_IOC
    elif info.trade_exemode != market:
        fill = api.ORDER_FILLING_RETURN
    else:
        raise ValueError("The symbol reports no supported market fill policy.")
    request = {"action": api.TRADE_ACTION_DEAL, "symbol": info.name, "volume": float(volume),
               "type": api.ORDER_TYPE_BUY if side == 1 else api.ORDER_TYPE_SELL,
               "type_time": api.ORDER_TIME_GTC, "type_filling": fill, "deviation": demo["deviation_points"],
               "magic": MAGIC, "comment": tag}
    if info.trade_exemode != market:
        request["price"] = price
    return request


def history(api, start, end):
    deals = api.history_deals_get(utc(start), utc(end))
    if deals is None:
        raise RuntimeError("MT5 deal history is unavailable.")
    return deals
