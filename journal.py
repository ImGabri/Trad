"""Durable entry intents, account limits and exactly-once learning per closed position."""
import json
import sqlite3
from pathlib import Path

import pandas as pd

import learning
from broker import DAY, MAGIC, history, now_ms, tick_data, utc

def excursions(api, symbol, start, end, side, entry_price):
    if entry_price is None or end < start or end-start > DAY:
        return {}
    try:
        quotes = tick_data(api,symbol,start,end+1)
    except (AttributeError,ValueError,RuntimeError):
        return {}
    if quotes.empty:
        return {}
    moves = (quotes.bid-entry_price) if side == 1 else (entry_price-quotes.ask)
    return {"best_move_usd_oz": round(max(0.,float(moves.max())),5),
            "worst_move_usd_oz": round(min(0.,float(moves.min())),5)}


class Journal:
    def __init__(self, path, account, symbol):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, timeout=10)
        self.key, self.symbol = account, symbol
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS entries(
                acct TEXT, symbol TEXT, tag TEXT, stamp INTEGER, status TEXT, info TEXT,
                order_id INTEGER DEFAULT 0, deal_id INTEGER DEFAULT 0, position_id INTEGER DEFAULT 0,
                closed_ms INTEGER DEFAULT 0, review TEXT, PRIMARY KEY(acct,symbol,tag));
            CREATE TABLE IF NOT EXISTS learning(acct TEXT, symbol TEXT, value TEXT, PRIMARY KEY(acct,symbol));
            CREATE TABLE IF NOT EXISTS days(acct TEXT, day INTEGER, anchor REAL, halted INTEGER, PRIMARY KEY(acct,day));
            CREATE TABLE IF NOT EXISTS exits(acct TEXT, symbol TEXT, position_id INTEGER, stamp INTEGER, tag TEXT,
                status TEXT, PRIMARY KEY(acct,symbol,position_id));
        """)
        self.db.row_factory = sqlite3.Row

    @property
    def scope(self):
        return self.key, self.symbol

    def state(self):
        row = self.db.execute("SELECT value FROM learning WHERE acct=? AND symbol=?", self.scope).fetchone()
        return json.loads(row[0]) if row else learning.blank()

    def risk_stop(self, stamp, equity, fraction):
        if equity <= 0:
            return True
        key = (self.key, stamp//DAY)
        with self.db:
            self.db.execute("INSERT OR IGNORE INTO days VALUES (?,?,?,0)", (*key, equity))
            self.db.execute("UPDATE days SET halted=1 WHERE acct=? AND day=? AND ? <= anchor*(1-?)", (*key, equity, fraction))
            return bool(self.db.execute("SELECT halted FROM days WHERE acct=? AND day=?", key).fetchone()[0])

    def entry_block(self, stamp, s):
        query = "SELECT status,stamp,closed_ms FROM entries WHERE acct=? AND symbol=? AND status != 'rejected'"
        rows = self.db.execute(query, self.scope).fetchall()
        if any(r["status"] != "closed" for r in rows):
            return "prior entry or fill reconciliation pending"
        if sum(r["stamp"]//DAY == stamp//DAY for r in rows) >= s["max_entries_utc_day"]:
            return "daily entry limit"
        if any(r["closed_ms"] + s["cooldown_seconds"]*1000 > stamp for r in rows):
            return "post-close cooldown"
        return ""

    def reserve(self, tag, stamp, info):
        with self.db:
            cur = self.db.execute("INSERT OR IGNORE INTO entries(acct,symbol,tag,stamp,status,info) VALUES(?,?,?,?,'submitting',?)",
                                  (*self.scope, tag, int(stamp), json.dumps(info, allow_nan=False)))
        return cur.rowcount == 1

    def acknowledge(self, tag, status, result):
        with self.db:
            self.db.execute("UPDATE entries SET status=?, order_id=?, deal_id=? WHERE acct=? AND symbol=? AND tag=?",
                            (status, int(getattr(result, "order", 0)), int(getattr(result, "deal", 0)), *self.scope, tag))

    def exit_ready(self, api, pid, stamp):
        r = self.db.execute("SELECT * FROM exits WHERE acct=? AND symbol=? AND position_id=?", (*self.scope,pid)).fetchone()
        if not r:
            return True
        if stamp - r["stamp"] < 2000:
            return False
        if r["status"] == "uncertain":
            deals = api.history_deals_get(position=pid)
            if deals is None or not any(d.comment == r["tag"] for d in deals):
                # A definitive rejection/cancellation also releases the close retry.
                orders = api.history_orders_get(utc(r["stamp"]-60_000), utc(stamp+1000))
                if orders is None or not any(o.comment == r["tag"] and o.state in (2,5,6) for o in orders):
                    return False
        return True

    def save_exit(self, pid, stamp, tag, status):
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO exits VALUES(?,?,?,?,?,?)", (*self.scope,pid,stamp,tag,status))

    def close_once(self, tag, closed, info, review):
        with self.db:
            cur = self.db.execute("UPDATE entries SET status='closed',closed_ms=? WHERE acct=? AND symbol=? AND tag=? AND status!='closed'",
                                  (int(closed), *self.scope, tag))
            if not cur.rowcount:
                return None
            if review["learned"]:
                states = self.state()
                before, after = learning.update(states[info["side"]], info["base"], info["vector"], review["net_account_currency"])
                review.update({"score_before_update": round(before, 5), "score_after_same_setup": round(after, 5),
                               "learned_trades_this_side": states[info["side"]]["closed"]})
                self.db.execute("INSERT OR REPLACE INTO learning VALUES(?,?,?)", (*self.scope, json.dumps(states)))
            self.db.execute("UPDATE entries SET review=? WHERE acct=? AND symbol=? AND tag=?", (json.dumps(review), *self.scope, tag))
        return review

    def reconcile(self, api, positions, orders, currency, stamp=None):
        stamp = now_ms() if stamp is None else stamp
        query = "SELECT * FROM entries WHERE acct=? AND symbol=? AND status NOT IN ('closed','rejected')"
        pending = self.db.execute(query, self.scope).fetchall()
        if not pending:
            return []
        open_ids = {p.identifier for p in positions}
        reports = []
        for entry in pending:
            tag, pid = entry["tag"], entry["position_id"]
            if not pid:
                deals = history(api, entry["stamp"]-60_000, stamp+1000)
                matches = [d for d in deals if d.magic == MAGIC and d.symbol == self.symbol and d.entry == 0 and
                           (d.comment == tag or d.ticket == entry["deal_id"] or d.order == entry["order_id"])]
                if not matches:
                    if not any(o.comment == tag for o in orders):
                        past_orders = api.history_orders_get(utc(entry["stamp"]-60_000), utc(stamp+1000))
                        if past_orders and any((o.comment == tag or o.ticket == entry["order_id"]) and
                                               o.symbol == self.symbol and o.magic == MAGIC and o.state in (2,5,6)
                                               for o in past_orders):
                            self.acknowledge(tag, "rejected", None)
                    continue
                ids = {d.position_id for d in matches}
                if len(ids) != 1:
                    continue
                pid = int(ids.pop())
                with self.db:
                    self.db.execute("UPDATE entries SET position_id=?,status='open' WHERE acct=? AND symbol=? AND tag=?",
                                    (pid,*self.scope,tag))
            if pid in open_ids:
                continue
            all_deals = api.history_deals_get(position=pid)
            if not all_deals:
                continue
            dd = sorted([d for d in all_deals if d.type in (0,1)], key=lambda d: (d.time_msc,d.ticket))
            opening, closing = [d for d in dd if d.entry == 0], [d for d in dd if d.entry in (1,3)]
            reversal = any(d.entry == 2 for d in dd)
            if not closing and not reversal:
                continue
            vin, vout = sum(d.volume for d in opening), sum(d.volume for d in closing)
            if not reversal and (vin <= 0 or abs(vin-vout) > 1e-7):
                continue
            info = json.loads(entry["info"])
            mixed = reversal or any(d.magic != MAGIC for d in opening)
            gross = sum(d.profit for d in all_deals)
            costs = sum(d.commission+d.fee+d.swap for d in all_deals)
            net = gross+costs
            end = dd[-1]
            fill = sum(d.price*d.volume for d in opening)/vin if vin else None
            sign = 1 if info["side"] == "BUY" else -1
            reason = {getattr(api,"DEAL_REASON_SL",4): "stop loss", getattr(api,"DEAL_REASON_TP",5): "take profit"}.get(end.reason)
            if not reason:
                reason = "time exit" if end.comment.startswith("GSx") else "other broker or manual exit"
            observation = ("position ownership changed" if mixed else "costs erased a positive gross result"
                           if gross > 0 and net <= 0 else "profitable after costs" if net > 0 else "flat or losing after costs")
            review = {"tag": tag, "side": info["side"], "currency": currency, "trigger_utc": utc(entry["stamp"]).isoformat(),
                      "entry_utc": utc(dd[0].time_msc).isoformat(),
                      "closed_utc": utc(end.time_msc).isoformat(), "held_seconds": round((end.time_msc-dd[0].time_msc)/1000,3),
                      "volume_lots": vin, "gross_account_currency": gross, "attributed_fees_swap": costs,
                      "net_account_currency": net, "exit": reason, "observation": observation, "learned": not mixed,
                      "spread_usd_oz": info["spread"], "entry_adverse_slippage_usd_oz": sign*(fill-info["requested"]) if fill else None,
                      "trigger_to_fill_ms": int(dd[0].time_msc-entry["stamp"]), "entry_score": info["score"],
                      "entry_drivers": info["drivers"]}
            if not mixed:
                review.update(excursions(api,self.symbol,dd[0].time_msc,end.time_msc,sign,fill))
                distance = abs(info["requested"]-info["sl"]) if "sl" in info else None
                if distance and net <= 0 and review.get("best_move_usd_oz",0) >= distance*.5:
                    review["price_path"] = "moved at least half the initial stop distance in favor before closing flat or down"
            result = self.close_once(tag, end.time_msc, info, review)
            if result:
                reports.append(result)
        return reports

    def reviews(self):
        rows = self.db.execute("SELECT review FROM entries WHERE acct=? AND symbol=? AND review IS NOT NULL ORDER BY closed_ms", self.scope)
        return pd.DataFrame([json.loads(r[0]) for r in rows])

    def unresolved(self):
        q = "SELECT tag,stamp,status FROM entries WHERE acct=? AND symbol=? AND status NOT IN ('closed','rejected') ORDER BY stamp"
        return [dict(r) for r in self.db.execute(q,self.scope)]

    def release_absent(self, api, tag, positions, orders):
        """Retire a locally selected ambiguous intent only after checking broker history."""
        row = self.db.execute("SELECT * FROM entries WHERE acct=? AND symbol=? AND tag=?",(*self.scope,tag)).fetchone()
        if not row or row["status"] not in ("submitting","uncertain") or row["position_id"]:
            raise ValueError("That tag is not an ambiguous entry without a matched position.")
        now = now_ms()
        if positions or orders or now-row["stamp"] < 120_000:
            raise ValueError("Wait for broker processing and check the symbol's positions and working orders.")
        deals = history(api,row["stamp"]-60_000,now+1000)
        orders_history = api.history_orders_get(utc(row["stamp"]-60_000),utc(now+1000))
        if orders_history is None:
            raise RuntimeError("Broker order history is unavailable.")
        matches = any(d.symbol == self.symbol and d.magic == MAGIC and
                      (d.comment == tag or d.ticket == row["deal_id"] or d.order == row["order_id"]
                       or (d.entry == 0 and d.time_msc >= row["stamp"])) for d in deals)
        matches |= any(o.symbol == self.symbol and o.magic == MAGIC and
                       (o.comment == tag or o.ticket == row["order_id"] or o.time_setup_msc >= row["stamp"])
                       for o in orders_history)
        if matches:
            raise ValueError("Broker activity may correspond to this intent. Keep its journal entry pending.")
        with self.db:
            cur = self.db.execute("UPDATE entries SET status='rejected' WHERE acct=? AND symbol=? AND tag=? AND status IN ('uncertain','submitting')",
                                  (*self.scope,tag))
        return cur.rowcount == 1
