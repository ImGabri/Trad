"""Minimal in-memory stand-in for the MetaTrader5 package, driven by synthetic M1 bars."""
from types import SimpleNamespace as NS

import numpy as np

ACCOUNT_TRADE_MODE_DEMO = 0
TIMEFRAME_M1, TIMEFRAME_M5, TIMEFRAME_M15, TIMEFRAME_H1 = 1, 5, 15, 16385
COPY_TICKS_ALL = -1
SYMBOL_TRADE_MODE_FULL = 4
TRADE_ACTION_DEAL, TRADE_ACTION_SLTP = 1, 6
ORDER_TYPE_BUY, ORDER_TYPE_SELL = 0, 1
POSITION_TYPE_BUY, POSITION_TYPE_SELL = 0, 1
ORDER_FILLING_FOK, ORDER_FILLING_IOC, ORDER_FILLING_RETURN = 0, 1, 2
ORDER_TIME_GTC = 0
TRADE_RETCODE_DONE, TRADE_RETCODE_PLACED = 10009, 10008
DEAL_ENTRY_OUT, DEAL_ENTRY_OUT_BY = 1, 3
OFFSET = 2 * 3600


class Fake:
    def __init__(self, m1, start_bar=14000):
        self.m1 = m1
        self.cur = start_bar  # index of the bar currently forming
        self.positions, self.deals, self.orders = {}, [], []
        self.balance, self.next_ticket = 10_000.0, 1000
        t = (m1.index.view("int64") // 10**9) + OFFSET if hasattr(m1.index, "view") else None
        self.t = ((m1.index - m1.index[0]).total_seconds().astype("int64") + int(m1.index[0].timestamp()) + OFFSET).to_numpy()

    # module API ---------------------------------------------------------------
    def initialize(self, **kw): return True
    def shutdown(self): pass
    def last_error(self): return (0, "ok")

    def account_info(self):
        eq = self.balance + sum(self._pnl(p) for p in self.positions.values())
        return NS(login=1, server="PUPrime-Demo", balance=self.balance, equity=eq, leverage=500, currency="USD",
                  trade_mode=0, margin_mode=2, company="PU Prime")

    def terminal_info(self): return NS(trade_allowed=True, ping_last=20000, path="C:/MT5", name="MT5", build=5000)
    def symbols_get(self): return [NS(name="XAUUSD", trade_mode=4), NS(name="XAUUSD247", trade_mode=4), NS(name="USDX", trade_mode=4)]
    def symbol_select(self, s, v=True): return True

    def symbol_info(self, s):
        return NS(name=s, point=0.01, digits=2, trade_contract_size=100.0, volume_min=0.01, volume_step=0.01,
                  volume_max=100.0, trade_stops_level=0, trade_freeze_level=0, filling_mode=1, trade_mode=4, spread=10,
                  trade_tick_value=1.0, trade_tick_size=0.01, currency_profit="USD", path="Metals")

    def symbol_info_tick(self, s):
        r = self.m1.iloc[self.cur]
        bid = float(r.open)
        return NS(bid=bid, ask=bid + float(r.spread), time=int(self.t[self.cur]))

    def _arr(self, a, b):
        df = self.m1.iloc[a:b]
        out = np.zeros(len(df), dtype=[("time", "i8"), ("open", "f8"), ("high", "f8"), ("low", "f8"), ("close", "f8"),
                                       ("tick_volume", "i8"), ("spread", "i4"), ("real_volume", "i8")])
        out["time"] = self.t[a:b]
        for c in ("open", "high", "low", "close", "tick_volume"):
            out[c] = df[c].to_numpy()
        out["spread"] = (df["spread"].to_numpy() / 0.01).round()
        return out

    def copy_rates_from_pos(self, s, tf, pos, count):
        if pos == 1 and count == 1:  # the runner's "last closed bar" probe → advance one bar
            self.cur += 1
            self._match_stops()
        end = self.cur + 1 - pos
        return self._arr(max(0, end - count), end)

    def copy_rates_range(self, s, tf, a, b):
        return self._arr(0, self.cur)

    def copy_ticks_range(self, s, a, b, flags):
        r = self.m1.iloc[self.cur - 3:self.cur]
        out = np.zeros(len(r) * 4, dtype=[("time_msc", "i8"), ("bid", "f8"), ("ask", "f8"), ("last", "f8"),
                                           ("volume", "i8"), ("flags", "i4")])
        for k, (i, row) in enumerate(r.iterrows()):
            for j, px in enumerate((row.open, row.high, row.low, row.close)):
                n = k * 4 + j
                out[n] = ((self.t[self.cur - 3 + k] + j * 15) * 1000, px, px + row.spread, 0, 0, 0)
        return out

    def order_calc_margin(self, t, s, v, px): return px * 100 * v / 500

    def positions_get(self, symbol=None): return list(self.positions.values())

    def history_deals_get(self, position=None):
        return [d for d in self.deals if d.position_id == position]

    def order_send(self, req):
        self.orders.append(req)
        if req["action"] == TRADE_ACTION_SLTP:
            p = self.positions[req["position"]]
            p.sl = req["sl"]
            return NS(retcode=TRADE_RETCODE_DONE, comment="", order=0, price=0, volume=0)
        if "position" in req:
            p = self.positions.pop(req["position"])
            self._close(p, req["price"])
            return NS(retcode=TRADE_RETCODE_DONE, comment="", order=p.ticket, price=req["price"], volume=p.volume)
        self.next_ticket += 1
        side = 1 if req["type"] == ORDER_TYPE_BUY else -1
        p = NS(ticket=self.next_ticket, symbol=req["symbol"], type=0 if side > 0 else 1, volume=req["volume"],
               price_open=req["price"], sl=req["sl"], tp=req["tp"], magic=req["magic"], time=int(self.t[self.cur]),
               profit=0.0)
        self.positions[p.ticket] = p
        return NS(retcode=TRADE_RETCODE_DONE, comment="", order=p.ticket, price=req["price"], volume=req["volume"])

    # internals -----------------------------------------------------------------
    def _pnl(self, p, px=None):
        r = self.m1.iloc[self.cur]
        side = 1 if p.type == 0 else -1
        px = px if px is not None else (r.open if side > 0 else r.open + r.spread)
        p.profit = (px - p.price_open) * side * p.volume * 100
        return p.profit

    def _close(self, p, px):
        pnl = self._pnl(p, px)
        self.balance += pnl - 2 * p.volume
        self.deals += [NS(position_id=p.ticket, entry=0, price=p.price_open, profit=0, swap=0, commission=-p.volume, fee=0),
                       NS(position_id=p.ticket, entry=1, price=px, profit=pnl, swap=0, commission=-p.volume, fee=0)]

    def _match_stops(self):
        r = self.m1.iloc[self.cur - 1]
        for t, p in list(self.positions.items()):
            long_ = p.type == 0
            lo, hi = (r.low, r.high) if long_ else (r.low + r.spread, r.high + r.spread)
            hit = (p.sl if (lo <= p.sl if long_ else hi >= p.sl) else
                   p.tp if (hi >= p.tp if long_ else lo <= p.tp) else None)
            if hit is not None:
                self.positions.pop(t)
                self._close(p, hit)


def install(m1, **kw):
    import sys
    import types
    fake = Fake(m1, **kw)
    mod = types.ModuleType("MetaTrader5")
    for k, v in globals().items():
        if k.isupper():
            setattr(mod, k, v)
    for name in dir(fake):
        if not name.startswith("_") and callable(getattr(fake, name)):
            setattr(mod, name, getattr(fake, name))
    mod._fake = fake
    sys.modules["MetaTrader5"] = mod
    return mod
