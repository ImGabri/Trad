"""Low-latency quote-triggered demo runner. Historical/network refreshes stay off the order path."""
from __future__ import annotations

import json
import math
import os
import tempfile
import threading
import time
from contextlib import contextmanager
from pathlib import Path

import numpy as np
import pandas as pd

import broker
import learning
from feeds import load_context, sync_feeds
from journal import Journal
from online_learning import OnlineLearner
from strategy import DIRECTION, MINUTE, candidate_features, event_mask, features, predict, quote_triggers, spread_ok, stop_distance


@contextmanager
def single_runner(key):
    path = Path(tempfile.gettempdir()) / ("puprime_scalper_" + key + ".lock")
    with path.open("a+b") as f:
        f.seek(0, 2)
        if f.tell() == 0:
            f.write(b"0")
            f.flush()
        f.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise RuntimeError("Another bot instance is using this demo account.") from exc
        yield


def emit(folder, kind, **data):
    item = {"time_utc": broker.utc(broker.now_ms()).isoformat(), "event": kind, **data}
    with (folder / "activity.jsonl").open("a", encoding="utf8") as f:
        f.write(json.dumps(item, allow_nan=False) + "\n")
    if kind != "quote_decision":
        print(json.dumps(item, ensure_ascii=False), flush=True)


def send(api, identity, request, on_result):
    broker.account(api, identity, trading=True)
    start = time.perf_counter()
    result = api.order_send(request)
    elapsed = round((time.perf_counter()-start)*1000, 2)
    code = result.retcode if result is not None else None
    if code in (10009,10010):
        status = "accepted"
    elif code in (None,10008,10011,10012,10023,10028,10031,10039) or code not in range(10004,10047):
        status = "uncertain"
    else:
        status = "rejected"
    on_result(status,result)
    return {"status": status, "retcode": code, "request_roundtrip_ms": elapsed,
            "broker_comment": str(getattr(result, "comment", "")), "fill_price": getattr(result,"price",None)}


def entry(api, identity, symbol, journal, signal, s, demo):
    side, stamp = signal["direction"], signal["trigger_ms"]
    tag = f"GSe{stamp:x}"
    blocked = journal.entry_block(stamp, s)
    if blocked:
        return {"skip": blocked}
    a = broker.account(api, identity, trading=True)
    if journal.risk_stop(broker.now_ms(), a.equity, demo["daily_equity_stop_fraction"]):
        return {"skip": "daily equity limit"}
    positions, orders = broker.exposure(api, symbol)
    if positions or orders:
        return {"skip": "symbol already has exposure"}
    q = broker.quote(api, symbol, demo["max_quote_age_seconds"])
    if q is None or q.time_msc < stamp or not 0 <= broker.now_ms() - stamp <= 1000*s["entry_window_seconds"]:
        return {"skip": "expired signal or stale quote"}
    distance = stop_distance(signal["atr"], s)
    if not spread_ok(q.ask-q.bid, distance, s):
        return {"skip": "spread widened"}
    info = api.symbol_info(symbol)
    if info is None or info.trade_mode != getattr(api,"SYMBOL_TRADE_MODE_FULL",4):
        return {"skip": "symbol cannot open both directions"}
    price = q.ask if side == 1 else q.bid
    other = q.bid if side == 1 else q.ask
    sl, tp = broker.brackets(info, side, price, distance, s["target_multiple"])
    min_stop = info.trade_stops_level * info.point
    if min(side*(other-sl), side*(tp-other)) < min_stop:
        return {"skip": "broker minimum stop distance"}
    order_type = api.ORDER_TYPE_BUY if side == 1 else api.ORDER_TYPE_SELL
    volume = broker.size(api,a,info,order_type,side,price,sl,s,demo)
    if volume <= 0:
        return {"skip": "minimum lot exceeds risk budget or profit estimate unavailable"}
    req = broker.deal_request(api,info,side,price,volume,tag,demo)
    req.update(sl=sl, tp=tp)
    check = api.order_check(req)
    if check is None or check.retcode != 0:
        return {"skip": "order_check", "retcode": getattr(check,"retcode",None), "comment": str(getattr(check,"comment",""))}
    q2 = broker.quote(api, symbol, demo["max_quote_age_seconds"])
    if q2 is None or broker.now_ms()-stamp > 1000*s["entry_window_seconds"]:
        return {"skip": "quote expired during order check"}
    drift = abs((q2.ask if side == 1 else q2.bid) - price)
    if drift > max(info.trade_tick_size, s["slippage_usd_oz_each_side"]) or not spread_ok(q2.ask-q2.bid, distance,s):
        return {"skip": "quote moved during order check"}
    if any(broker.exposure(api, symbol)):
        return {"skip": "symbol acquired exposure"}
    broker.account(api, identity, trading=True)
    meta = {**signal,"requested":price,"spread":q.ask-q.bid,"lots":volume,"sl":sl,"tp":tp}
    if not journal.reserve(tag, stamp, meta):
        return {"skip": "quote already submitted"}
    result = send(api,identity,req,lambda status,r: journal.acknowledge(tag,status,r))
    return {"tag":tag,"side":signal["side"],"quote_to_request_end_ms":broker.now_ms()-stamp,
            "requested_price":price, "lots":volume, "sl":sl, "tp":tp, **result}


def manage_time_exits(api, identity, symbol, positions, orders, journal, s, demo, folder):
    now = broker.now_ms()
    for p in positions:
        if p.magic != broker.MAGIC or now < p.time_msc + s["hold_seconds"]*1000:
            continue
        owned = journal.db.execute("SELECT 1 FROM entries WHERE acct=? AND symbol=? AND (position_id=? OR tag=?) AND status!='closed'",
                                   (*journal.scope,p.identifier,p.comment)).fetchone()
        if not owned or any(o.position_id == p.identifier for o in orders) or not journal.exit_ready(api,p.identifier,now):
            continue
        dd = api.history_deals_get(position=p.identifier)
        if not dd or any(d.entry == 2 or (d.entry == 0 and d.magic != broker.MAGIC) for d in dd):
            continue
        q = broker.quote(api,symbol,demo["max_quote_age_seconds"])
        if q is None:
            continue
        broker.account(api,identity,trading=True)
        side = -1 if p.type == api.POSITION_TYPE_BUY else 1
        info = api.symbol_info(symbol)
        tag = f"GSx{now:x}"
        req = broker.deal_request(api,info,side,q.ask if side == 1 else q.bid,p.volume,tag,demo)
        req["position"] = p.ticket
        check = api.order_check(req)
        if check is None or check.retcode:
            journal.save_exit(p.identifier,now,tag,"rejected")
            emit(folder,"time_exit_check",retcode=getattr(check,"retcode",None))
            continue
        current = api.positions_get(ticket=p.ticket)
        if not current or current[0].identifier != p.identifier or current[0].volume != p.volume or current[0].magic != broker.MAGIC:
            continue
        journal.save_exit(p.identifier,now,tag,"uncertain")
        result = send(api,identity,req,lambda status,r: journal.save_exit(p.identifier,now,tag,status))
        emit(folder,"time_exit",tag=tag,**result)


def score(model, row, states, s):
    prob, z = predict(model,row)
    classes = model["classes"]
    x = row.iloc[0]
    ctx = (x.tick_spread_fraction, x.tick_impulse_spreads, x.quote_imbalance_1m)
    side, scores, vectors = learning.side_scores(classes,prob[0],states,ctx,s["min_model_score"])
    best = classes.index(side) if side else int(prob[0].argmax())
    runner = max((i for i in range(len(classes)) if i != best),key=lambda i:prob[0,i])
    delta = z[0]*(np.array(model["weight"][best])-np.array(model["weight"][runner]))
    leaders = np.argsort(-np.abs(delta))[:4]
    return {"direction":side,"side":DIRECTION[side],"trigger_ms":int(row.index[-1]),
            "base":float(prob[0,best]), "score":float(scores.get(side,prob[0,best])),
            "buy_score":round(scores[1],5), "sell_score":round(scores[-1],5),
            "vector":vectors[side].tolist() if side else [], "atr":float(x.atr_usd_oz),
            "drivers":{model["features"][i]:round(float(delta[i]),3) for i in leaders},
            "missing_inputs":[k for k in model["features"] if k not in row or not math.isfinite(float(x[k]))]}


def validate_session(conn, cfg, artifact, orders_enabled):
    api, identity, symbol, markets = conn
    source = artifact["source"]
    if (source.get("kind"),source.get("server"),source.get("symbol")) != ("PUPrime-MT5",identity[1],symbol):
        raise ValueError("Train using tick history from this PU Prime demo server and gold symbol.")
    s, demo = cfg["strategy"],cfg["demo"]
    if s != artifact["strategy"]:
        raise ValueError("Strategy settings changed. Retrain on the saved market export before running.")
    if artifact["trained_through_ms"] > broker.now_ms():
        raise ValueError("Model training timestamp is ahead of the system clock.")
    broker.account(api,identity,trading=orders_enabled)


def run(conn, cfg, artifact, folder, orders_enabled, active=None):
    validate_session(conn,cfg,artifact,orders_enabled)
    api, identity, symbol, markets = conn
    key = broker.account_key(identity)
    folder.mkdir(parents=True,exist_ok=True)
    with single_runner(key):
        journal = Journal(folder/"demo.sqlite",key,symbol)
        online = OnlineLearner(folder, artifact["classifier"], cfg["strategy"])
        online.start()
        try:
            loop(conn,cfg,artifact["classifier"],folder,journal,orders_enabled,active,online)
        finally:
            online.stop()
            journal.db.close()


def loop(conn,cfg,model,folder,journal,orders_enabled,active=None,online=None):
    api, identity, symbol, markets = conn
    s, demo = cfg["strategy"], cfg["demo"]
    seen, bucket, minute = None, -1, -1
    handled = set()
    bars, events, hist, market = None, None, None, None
    service_at = refresh_at = 0.
    refresh = None
    heartbeat_at = 0.0
    last_decision = None
    blocked = ""
    states = journal.state()
    journal_states = states
    mode = "demo orders" if orders_enabled else "watch"
    emit(folder,"started",mode=mode,symbol=symbol,poll_interval_ms=demo["poll_ms"])
    if online is None:
        online = OnlineLearner(folder, model, s)
        online.start()
    states = learning.combined(states, online.states)
    while True:
        current = time.monotonic()
        if current >= service_at:
            if active is not None and not active():
                return
            a = broker.account(api,identity,trading=orders_enabled)
            positions, orders = broker.exposure(api,symbol)
            reviews = journal.reconcile(api,positions,orders,a.currency)
            for review in reviews:
                emit(folder,"trade_review",**review)
            if reviews:
                journal_states = journal.state()
            held = journal.risk_stop(broker.now_ms(),a.equity,demo["daily_equity_stop_fraction"])
            if orders_enabled:
                manage_time_exits(api,identity,symbol,positions,orders,journal,s,demo,folder)
            blocked = ("daily equity limit" if held else "existing exposure" if positions or orders
                       else journal.entry_block(broker.now_ms(),s))
            if online is not None:
                try:
                    report = online.tick(asof_ms=broker.now_ms(), events=events)
                    scan = report.get("scan") or {}
                    if scan and scan.get("side"):
                        emit(folder, "online_signal", mode="online", symbol=symbol, **scan)
                    for result in report.get("resolved") or []:
                        emit(folder, "online_resolve", mode="online", symbol=symbol, **result)
                    states = learning.combined(journal_states, online.states)
                except Exception:
                    pass
            service_at = current + .5
        if current >= refresh_at and not (refresh and refresh.is_alive()):
            def sync():
                emit(folder,"data_feeds",providers=sync_feeds(folder,cfg["history_days"]))
            refresh = threading.Thread(target=sync,daemon=True)
            refresh.start()
            refresh_at = current + 4*3600
        t = broker.quote(api,symbol,demo["max_quote_age_seconds"])
        if t and (t.time_msc,t.bid,t.ask) != seen:
            seen = (t.time_msc,t.bid,t.ask)
            current_minute = t.time_msc // MINUTE * MINUTE
            if minute != current_minute:
                start = current_minute - 75*MINUTE
                left = max(start,minute)
                fresh = broker.tick_data(api,symbol,left,current_minute)
                old = hist.loc[hist.ms.ge(start)] if hist is not None else None
                hist = pd.concat([old,fresh],ignore_index=True) if old is not None and not old.empty else fresh
                # Overlap recent reference bars to pick up delayed terminal history.
                changes = broker.market_data(api,markets,max(start,left-2*MINUTE),current_minute)
                if market is not None and not market.empty:
                    market = pd.concat([market.loc[market.known_ms.ge(start)],changes],ignore_index=True)
                    market = market.drop_duplicates(["series","known_ms"],keep="last")
                else:
                    market = changes
                macro, events = load_context(folder)
                bars = features(hist,market,macro,asof_ms=current_minute) if not hist.empty else None
                minute = current_minute
            b = t.time_msc//s["trigger_bucket_ms"]
            if b != bucket:
                bucket, handled = b, set()
            if len(handled) < 2 and bars is not None and minute in bars.index:
                # Catch distinct impulses within one poll and act on the newest eligible quote.
                raw = broker.tick_data(api,symbol,b*s["trigger_bucket_ms"]-2000,t.time_msc+1)
                if len(raw) >= 2:
                    points = quote_triggers(raw,s)
                    points = points.loc[(points.index//s["trigger_bucket_ms"] == b) & ~points.index.isin(handled)]
                    if not points.empty:
                        handled.update(points.index)
                        row = candidate_features(bars,points.tail(1),s)
                        if not row.empty and broker.now_ms()-row.index[-1] <= 1000*s["entry_window_seconds"]:
                            signal = score(model,row,states,s)
                            news = bool(event_mask(row.index,events,s)[0])
                            why = "scheduled news window" if news else blocked
                            last_decision = dict(time_utc=broker.utc(int(row.index[-1])).isoformat(), side=signal["side"], score=signal["score"], blocked=why)
                            emit(folder,"quote_decision",mode="demo" if orders_enabled else "watch",blocked=why,**signal)
                            if signal["direction"] and not why:
                                if orders_enabled:
                                    if active is not None and not active():
                                        return
                                    result = entry(api,identity,symbol,journal,signal,s,demo)
                                    emit(folder,"entry_attempt",**result)
                                    if result.get("status") in ("accepted","uncertain"):
                                        blocked = "entry submitted"
                                else:
                                    emit(folder,"watch_signal",side=signal["side"],score=signal["score"],symbol=symbol)
        if current >= heartbeat_at:
            status = "scanning quotes"
            if t is None:
                status = "waiting for a fresh broker quote"
            elif bars is None or minute not in bars.index:
                status = "waiting for minute history"
            status = blocked or status
            emit(folder,"heartbeat",mode=mode,symbol=symbol,status=status,last_decision=last_decision)
            heartbeat_at = current + 15
        time.sleep(demo["poll_ms"]/1000)
