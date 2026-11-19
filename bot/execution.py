"""MT5 order placement and position management (§3 exits, §7). SL/TP are always attached at placement."""
from __future__ import annotations

import logging
from dataclasses import dataclass

try:
    import MetaTrader5 as mt5
except ImportError:  # pragma: no cover
    mt5 = None

log = logging.getLogger("execution")
MAGIC = 26_1119  # identifies this bot's orders


@dataclass
class Fill:
    ok: bool
    ticket: int = 0
    price: float = 0.0
    requested: float = 0.0
    volume: float = 0.0
    retcode: int = 0
    comment: str = ""


def _filling(info) -> int:
    fm = info.filling_mode  # bit flags: 1 = FOK, 2 = IOC
    if fm & 1:
        return mt5.ORDER_FILLING_FOK
    if fm & 2:
        return mt5.ORDER_FILLING_IOC
    return mt5.ORDER_FILLING_RETURN


INVALID_FILL, REQUOTE, PRICE_CHANGED, PRICE_OFF = 10030, 10004, 10020, 10021


def _send_deal(req: dict, info, side: int):
    """order_send with the broker-quirk retries brokers need in practice:
    unsupported filling mode → try the other modes; requote / price moved → refresh the price once."""
    modes = [req["type_filling"]] + [m for m in (mt5.ORDER_FILLING_FOK, mt5.ORDER_FILLING_IOC,
                                                  mt5.ORDER_FILLING_RETURN) if m != req["type_filling"]]
    res, refreshed = None, False
    for mode in modes:
        req["type_filling"] = mode
        res = _send_demo(req)
        if res is not None and res.retcode in (REQUOTE, PRICE_CHANGED, PRICE_OFF) and not refreshed:
            tick = mt5.symbol_info_tick(req["symbol"])
            if tick is not None:
                new = tick.ask if side > 0 else tick.bid
                shift = new - req["price"]
                req["price"] = new
                for k in ("sl", "tp"):
                    if req.get(k):
                        req[k] = _round(req[k] + shift, info)
                refreshed = True
                res = _send_demo(req)
        if res is None or res.retcode != INVALID_FILL:
            return res
        log.info("filling mode %s rejected, trying the next one", mode)
    return res


def _round(price: float, info) -> float:
    return round(round(price / info.trade_tick_size) * info.trade_tick_size, info.digits)


def min_stop_distance(info) -> float:
    return max(info.trade_stops_level, info.trade_freeze_level) * info.point


def _send_demo(request):
    account = mt5.account_info()
    if account is None or account.trade_mode != mt5.ACCOUNT_TRADE_MODE_DEMO:
        raise RuntimeError("Order blocked: select the PU Prime demo account in MT5.")
    return mt5.order_send(request)


def open_market(symbol: str, side: int, lots: float, sl_dist: float, tp_dist: float, deviation_points: int,
                comment: str = "xau-v2") -> Fill:
    info, tick = mt5.symbol_info(symbol), mt5.symbol_info_tick(symbol)
    if info is None or tick is None:
        return Fill(False, comment="no symbol/tick")
    price = tick.ask if side > 0 else tick.bid
    sl_dist = max(sl_dist, min_stop_distance(info) + info.point)
    tp_dist = max(tp_dist, min_stop_distance(info) + info.point)
    req = dict(action=mt5.TRADE_ACTION_DEAL, symbol=symbol, volume=float(lots),
               type=mt5.ORDER_TYPE_BUY if side > 0 else mt5.ORDER_TYPE_SELL, price=price,
               sl=_round(price - side * sl_dist, info), tp=_round(price + side * tp_dist, info),
               deviation=int(deviation_points), magic=MAGIC, comment=comment[:31],
               type_time=mt5.ORDER_TIME_GTC, type_filling=_filling(info))
    res = _send_deal(req, info, side)
    if res is None:
        return Fill(False, requested=price, comment=str(mt5.last_error()))
    ok = res.retcode in (mt5.TRADE_RETCODE_DONE, mt5.TRADE_RETCODE_PLACED)
    if not ok:
        log.warning("order rejected: %s %s", res.retcode, res.comment)
    return Fill(ok, res.order, res.price or price, price, res.volume, res.retcode, res.comment)


def bot_positions(symbol: str) -> list:
    return [p for p in (mt5.positions_get(symbol=symbol) or []) if p.magic == MAGIC]


def modify_sl(pos, new_sl: float) -> bool:
    info = mt5.symbol_info(pos.symbol)
    tick = mt5.symbol_info_tick(pos.symbol)
    new_sl = _round(new_sl, info)
    # respect broker min distance from current price
    gap = min_stop_distance(info)
    if pos.type == mt5.POSITION_TYPE_BUY and new_sl > tick.bid - gap:
        return False
    if pos.type == mt5.POSITION_TYPE_SELL and new_sl < tick.ask + gap:
        return False
    res = _send_demo(dict(action=mt5.TRADE_ACTION_SLTP, symbol=pos.symbol, position=pos.ticket,
                              sl=new_sl, tp=pos.tp, magic=MAGIC))
    return bool(res and res.retcode == mt5.TRADE_RETCODE_DONE)


def close_position(pos, deviation_points: int, comment: str = "close") -> Fill:
    info, tick = mt5.symbol_info(pos.symbol), mt5.symbol_info_tick(pos.symbol)
    buy = pos.type == mt5.POSITION_TYPE_BUY
    price = tick.bid if buy else tick.ask
    res = _send_deal(dict(action=mt5.TRADE_ACTION_DEAL, symbol=pos.symbol, volume=pos.volume,
                          type=mt5.ORDER_TYPE_SELL if buy else mt5.ORDER_TYPE_BUY, position=pos.ticket,
                          price=price, deviation=int(deviation_points), magic=MAGIC, comment=comment[:31],
                          type_time=mt5.ORDER_TIME_GTC, type_filling=_filling(info)), info, -1 if buy else 1)
    ok = bool(res and res.retcode == mt5.TRADE_RETCODE_DONE)
    if not ok:
        log.error("close failed #%s: %s", pos.ticket, res.comment if res else mt5.last_error())
    return Fill(ok, pos.ticket, res.price if res else 0, price, pos.volume, res.retcode if res else 0)


def close_all(symbol: str, deviation_points: int, reason: str) -> int:
    n = 0
    for p in bot_positions(symbol):
        n += close_position(p, deviation_points, reason).ok
    return n


def closed_pnl(ticket: int) -> tuple[float, float, float] | None:
    """(profit+swap+commission+fee, exit price, commission) for a closed position, from deal history."""
    deals = mt5.history_deals_get(position=ticket)
    if not deals:
        return None
    outs = [d for d in deals if d.entry in (mt5.DEAL_ENTRY_OUT, mt5.DEAL_ENTRY_OUT_BY)]
    if not outs:
        return None
    total = sum(d.profit + d.swap + d.commission + getattr(d, "fee", 0.0) for d in deals)
    comm = sum(d.commission + getattr(d, "fee", 0.0) for d in deals)
    return total, outs[-1].price, comm
