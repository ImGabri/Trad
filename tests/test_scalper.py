import tempfile
import unittest
import json
from pathlib import Path
from types import SimpleNamespace as N
from unittest.mock import patch

import numpy as np
import pandas as pd

import broker
import engine
import learning
from feeds import calendar_update, load_context, parse_bls, sync_feeds
from goldbot import settings
from journal import Journal
from research import fill_outcome, read_bundle, synthetic_data, write_bundle
from strategy import MINUTE, candidate_features, event_mask, features, quote_frame, quote_triggers

CFG = settings()
S, DEMO = CFG["strategy"],CFG["demo"]


class Terminal:
    ACCOUNT_TRADE_MODE_DEMO = 0
    TRADE_ACTION_DEAL = 1
    ORDER_TYPE_BUY, ORDER_TYPE_SELL = 0, 1
    ORDER_TIME_GTC = 0
    ORDER_FILLING_FOK, ORDER_FILLING_IOC, ORDER_FILLING_RETURN = 0, 1, 2
    POSITION_TYPE_BUY = 0
    def __init__(self):
        self.a = N(login=123,server="PUPrime-Demo",company="PU Prime",trade_mode=0,trade_allowed=True,
                   trade_expert=True,equity=10_000.,currency="USD")
        self.spec = N(name="XAUUSD.s",volume_min=.01,volume_step=.01,volume_max=100.,trade_tick_size=.01,
                      point=.01,trade_mode=4,trade_exemode=2,trade_stops_level=5,
                      filling_mode=3,digits=2,currency_profit="USD",visible=True)
        self.pos, self.orders, self.deals, self.sent = [],[],[],[]
        self.result = N(retcode=10009,order=100,deal=101,price=100.10,comment="done")
        self.info = N(trade_allowed=True,tradeapi_disabled=False,connected=True)
    def account_info(self): return self.a
    def terminal_info(self): return self.info
    def symbol_info(self, symbol): return self.spec
    def symbol_info_tick(self,symbol): return N(bid=100.,ask=100.10,time_msc=broker.now_ms())
    def symbol_select(self,symbol,visible): return True
    def positions_get(self,**kwargs): return self.pos
    def orders_get(self,**kwargs): return self.orders
    def order_check(self,request): return N(retcode=0)
    def order_send(self,request):
        self.sent.append(dict(request))
        return self.result
    def order_calc_profit(self,kind,symbol,volume,entry,exit_price):
        return (1 if kind == 0 else -1)*volume*100*(exit_price-entry)
    def history_deals_get(self,*args,**kwargs): return self.deals
    def history_orders_get(self,*args,**kwargs): return []


def signal(side,stamp):
    return {"direction":side,"side":"BUY" if side==1 else "SELL","trigger_ms":stamp,"base":.62,"score":.62,
            "vector":[1.,.5,.3,.6,.1],"atr":1.,"drivers":{"quote_imbalance_1m":.4}}


def deal(pid, ticket, when, entry, volume, profit=0., commission=0., comment="", reason=3):
    return N(position_id=pid,ticket=ticket,order=100 if entry==0 else ticket,magic=broker.MAGIC,symbol="XAUUSD.s",
             entry=entry,type=0 if entry==0 else 1,time_msc=when,volume=volume,profit=profit,swap=0.,fee=0.,
             commission=commission,comment=comment,reason=reason,price=100.15 if entry==0 else 100.25)


class Execution(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name)/"demo.sqlite"
        self.db = Journal(self.path,"acct","XAUUSD.s")
        self.api = Terminal()
        self.id = (self.api.a.login,self.api.a.server)
    def tearDown(self):
        self.db.db.close()
        self.temp.cleanup()
    def enter(self,side,stamp=None):
        return engine.entry(self.api,self.id,"XAUUSD.s",self.db,signal(side,stamp or broker.now_ms()),S,DEMO)
    def test_buy_is_immediate_market_request_with_brackets(self):
        r = self.enter(1)
        req = self.api.sent[0]
        self.assertEqual((r["status"],req["action"],req["type"]),("accepted",1,0))
        self.assertLess(req["sl"],100.)
        self.assertGreater(req["tp"],100.1)
        self.assertLessEqual(req["volume"],.1)
        self.assertNotIn("price",req)
        self.assertEqual(len(self.api.sent),1)
        self.assertIn("skip",self.enter(-1))
    def test_short_request_has_short_brackets(self):
        r = self.enter(-1)
        req = self.api.sent[0]
        self.assertEqual((r["status"],req["type"]),("accepted",1))
        self.assertGreater(req["sl"],100.1)
        self.assertLess(req["tp"],100.)
    def test_real_or_switched_account_cannot_send(self):
        self.api.a.trade_mode = 2
        with self.assertRaisesRegex(RuntimeError,"DEMO"):
            self.enter(1)
        self.api.a.trade_mode, self.api.a.login = 0, 321
        with self.assertRaisesRegex(RuntimeError,"changed"):
            self.enter(-1)
        self.assertFalse(self.api.sent)
    def test_stale_and_wide_quote(self):
        self.assertIn("expired",self.enter(1,broker.now_ms()-40_000)["skip"])
        self.api.symbol_info_tick = lambda symbol:N(bid=100.,ask=101.2,time_msc=broker.now_ms())
        self.assertIn("spread",self.enter(1)["skip"])
        self.assertFalse(self.api.sent)
    def test_small_balance_does_not_round_up_to_minimum_lot(self):
        self.api.a.equity = 10
        self.assertIn("minimum lot",self.enter(-1)["skip"])
        self.assertFalse(self.api.sent)
    def test_daily_limit_survives_restart_and_recovery(self):
        ts = broker.now_ms()
        self.assertFalse(self.db.risk_stop(ts,10000,.01))
        self.assertTrue(self.db.risk_stop(ts,9800,.01))
        self.db.db.close()
        self.db = Journal(self.path,"acct","XAUUSD.s")
        self.assertTrue(self.db.risk_stop(ts,11000,.01))
        self.assertFalse(self.db.risk_stop(ts+86_400_000,11000,.01))
    def test_broker_timeout_does_not_duplicate_entry_after_restart(self):
        self.api.result = None
        self.assertEqual(self.enter(1)["status"],"uncertain")
        self.db.db.close()
        self.db = Journal(self.path,"acct","XAUUSD.s")
        self.assertIn("pending",self.enter(1)["skip"])
        self.assertEqual(len(self.api.sent),1)
    def test_recovery_preserves_ambiguous_matching_broker_deal(self):
        stamp = broker.now_ms()-180_000
        self.db.reserve("absent",stamp,signal(1,stamp))
        self.api.deals = [deal(500,101,stamp+30,0,.01,comment="broker rewrote comment")]
        with self.assertRaisesRegex(ValueError,"Broker activity"):
            self.db.release_absent(self.api,"absent",[],[])
        self.assertEqual(len(self.db.unresolved()),1)
        self.api.deals = []
        self.assertTrue(self.db.release_absent(self.api,"absent",[],[]))
        self.assertEqual(self.db.unresolved(),[])
        self.assertFalse(self.api.sent)
    def test_partial_close_then_net_loss_learns_once_after_restart(self):
        stamp = broker.now_ms()-30_000
        tag = "GSe123"
        info = {**signal(1,stamp),"requested":100.10,"spread":.10}
        self.db.reserve(tag,stamp,info)
        self.api.deals = [deal(500,101,stamp+200,0,.05,commission=-.30,comment=tag),
                          deal(500,102,stamp+5000,1,.02,.15,commission=-.10)]
        self.assertEqual(self.db.reconcile(self.api,[N(identifier=500)],[],"USD"),[])
        self.assertEqual(self.db.reconcile(self.api,[],[],"USD"),[])
        self.db.db.close()
        self.db = Journal(self.path,"acct","XAUUSD.s")
        self.api.deals.append(deal(500,103,stamp+12_000,1,.03,.10,commission=-.10,reason=5))
        review = self.db.reconcile(self.api,[],[],"USD")[0]
        self.assertAlmostEqual(review["net_account_currency"],-.25)
        self.assertIn("costs erased",review["observation"])
        self.assertLess(review["score_after_same_setup"],review["score_before_update"])
        self.assertEqual(self.db.reconcile(self.api,[],[],"USD"),[])
        self.assertEqual(self.db.state()["BUY"]["closed"],1)
        self.assertEqual(self.db.state()["SELL"]["closed"],0)
        self.assertEqual(len(self.db.reviews()),1)
    def test_positive_short_updates_only_its_score(self):
        base = .62
        v = learning.context(base,-1,.1,-.7,-.2)
        state = learning.blank()
        before,after = learning.update(state["SELL"],base,v,2.5)
        self.assertGreater(after,before)
        self.assertEqual(state["BUY"],learning.blank()["BUY"])
    def test_symbol_suffix_and_distinct_247_contract(self):
        alt = N(**{**vars(self.api.spec),"name":"XAUUSD247","visible":True})
        self.assertEqual(broker.resolve_gold(self.api,"auto",[alt,self.api.spec]),"XAUUSD.s")
        with self.assertRaises(ValueError):
            broker.resolve_gold(self.api,"XAUUSD247",[alt,self.api.spec])
    def test_time_exit_uses_owned_position_ticket(self):
        stamp = broker.now_ms()-200_000
        self.db.reserve("GSe123",stamp,{**signal(1,stamp),"requested":100.10,"spread":.1})
        pos = N(identifier=500,ticket=505,comment="GSe123",type=0,time_msc=stamp,volume=.05,magic=broker.MAGIC)
        self.api.pos = [pos]
        self.api.deals = [deal(500,101,stamp,0,.05,comment="GSe123")]
        engine.manage_time_exits(self.api,self.id,"XAUUSD.s",[pos],[],self.db,S,DEMO,Path(self.temp.name))
        self.assertEqual((self.api.sent[0]["position"],self.api.sent[0]["type"]),(505,1))
    def test_quote_to_order_through_runner_for_both_directions(self):
        boundary = broker.now_ms()//MINUTE*MINUTE
        clock = boundary+5100
        times = np.r_[np.arange(boundary-76*MINUTE,boundary+5000,1000),clock]
        hist = np.empty(len(times),dtype=[("time_msc","int64"),("bid","float64"),("ask","float64")])
        hist["time_msc"] = times
        hist["bid"] = 100 + .3*np.sin(np.arange(len(times))*.07)
        hist["bid"][-6:] = 100.
        self.api.COPY_TICKS_INFO = 1
        self.api.copy_ticks_range = lambda sym,l,r,fl: hist[(hist["time_msc"]>=round(l.timestamp()*1000)) &
                                                            (hist["time_msc"]<=round(r.timestamp()*1000))]
        self.api.symbol_info_tick = lambda sym:N(time_msc=clock,bid=float(hist["bid"][-1]),ask=float(hist["ask"][-1]))
        model = {"features":["tick_impulse_spreads"],"classes":[-1,0,1],"median":[0.],"scale":[1.],
                 "weight":[[-3.],[0.],[3.]],"bias":[0.,0.,0.]}
        artifact = {"source":{"kind":"PUPrime-MT5","server":self.id[1],"symbol":"XAUUSD.s"},
                    "strategy":S,"trained_through_ms":boundary-1,"classifier":model}
        for side in (1,-1):
            with self.subTest(side=side):
                hist["bid"][-1] = 100.+side*.5
                hist["ask"] = hist["bid"]+.1
                self.api.sent.clear()
                with patch("broker.now_ms",return_value=clock), patch("engine.threading.Thread.start"), \
                     patch("engine.time.sleep",side_effect=KeyboardInterrupt), self.assertRaises(KeyboardInterrupt):
                    engine.run((self.api,self.id,"XAUUSD.s",{}),CFG,artifact,Path(self.temp.name)/str(side),True)
                self.assertEqual(len(self.api.sent),1)
                self.assertEqual(self.api.sent[0]["type"],0 if side==1 else 1)
    def test_synthetic_model_cannot_run_on_broker(self):
        artifact = {"source":{"kind":"synthetic","symbol":"SYNTHETIC"}}
        with self.assertRaisesRegex(ValueError,"Train using tick history"):
            engine.run((self.api,self.id,"XAUUSD.s",{}),CFG,artifact,Path(self.temp.name),True)
        self.assertFalse(self.api.sent)


class Data(unittest.TestCase):
    def test_replay_pays_bid_ask_commission_and_slippage_both_directions(self):
        times = np.array([0,1000,2000])
        for side, prices, entry in ((1, [100,100,100],100.2),(-1,[100.2]*3,100)):
            n, ts, reason = fill_outcome(times,np.array(prices),0,2,side,entry,1,1.4,S)
            self.assertAlmostEqual(n,-.36)
            self.assertEqual((ts,reason),(2000,"time"))
    def test_stop_gap_fills_at_observed_worse_quote(self):
        n,ts,why = fill_outcome(np.array([0,1000,2000]),np.array([99.9,97.5,101]),0,2,1,100,1,1.4,S)
        self.assertEqual((ts,why),(1000,"stop"))
        self.assertAlmostEqual(n,-2.66)
    def test_current_tick_can_trigger_buy_or_sell_inside_same_minute(self):
        t = quote_frame({"ms":[60_100,60_120,60_140,62_010,62_050],"bid":[100,100.1,100.2,100.1,99.90],
                         "ask":[100.1,100.2,100.3,100.2,100]})
        pts = quote_triggers(t,S)
        self.assertEqual(list(pts.index),[60_120,62_010])
        self.assertGreater(pts.tick_impulse_spreads.iloc[0],0)
        self.assertLess(pts.tick_impulse_spreads.iloc[1],0)
    def test_opposite_impulse_does_not_wait_for_next_bucket(self):
        t = quote_frame({"ms":[60_100,60_120,60_140],"bid":[100.,100.4,99.9],"ask":[100.1,100.5,100.]})
        self.assertEqual(quote_triggers(t,S).index.tolist(),[60_120,60_140])
    def test_future_ticks_and_unpublished_macro_do_not_change_a_signal(self):
        ticks,market,*_ = synthetic_data(1)
        t0 = int(ticks.ms.iloc[0])
        t = t0+100*MINUTE
        future = pd.DataFrame({"known_ms":[t+MINUTE], "observed_ms":[t-1000], "series":["macro"],"value":[4.5]})
        original = features(ticks.loc[ticks.ms.lt(t)],market,future,asof_ms=t)
        extra = ticks.loc[ticks.ms.lt(t+3*MINUTE)].copy()
        extra.loc[extra.ms.ge(t),["ask","bid"]] += 500
        revised = features(extra,market,future)
        pd.testing.assert_series_equal(original.loc[t],revised.loc[t])
        self.assertTrue(pd.isna(original.loc[t,"macro_level"]))
        self.assertEqual(revised.loc[t+MINUTE,"macro_level"],4.5)
        points = quote_triggers(ticks[(ticks.ms>=t)&(ticks.ms<t+2*MINUTE)],S)
        intraminute = candidate_features(original,points,S)
        self.assertTrue((intraminute.index < t+MINUTE).all())
        self.assertFalse(intraminute.empty)
    def test_archived_calendar_reschedule_and_bls_timezone(self):
        text = "BEGIN:VCALENDAR\nBEGIN:VEVENT\nUID:1\nDTSTART;TZID=US-Eastern:20261110T083000\nSUMMARY:Consumer Price Index\nEND:VEVENT\nEND:VCALENDAR"
        parsed = parse_bls(text)
        event_ms = pd.Timestamp("2026-11-10T13:30:00Z").value//1_000_000
        self.assertEqual(parsed["bls-1"][0],event_ms)
        old = pd.DataFrame(columns=["event_id","known_ms","event_ms","name","active"])
        archive = calendar_update(old,parsed,event_ms-2*MINUTE,event_ms+MINUTE)
        archive = calendar_update(archive,{},event_ms-MINUTE,event_ms+MINUTE)
        self.assertEqual(event_mask([event_ms-3*MINUTE,event_ms-90_000,event_ms],archive,S).tolist(),[False,True,False])
    def test_optional_provider_ingestion_uses_first_vintage_and_utc_events(self):
        def response(url):
            if "tradingeconomics" in url:
                return json.dumps([{"CalendarId":"123","Date":"2026-12-01T19:00:00","Event":"Fed decision","DateSpan":"0"}]).encode()
            if "stlouisfed" in url:
                self.assertIn("output_type=4",url)
                return json.dumps({"observations":[{"date":"2026-11-02","realtime_start":"2026-11-05","value":"2.25"}]}).encode()
            return b"BEGIN:VCALENDAR\nBEGIN:VEVENT\nUID:A\nDTSTART:20261201T133000Z\nSUMMARY:Consumer Price Index\nEND:VEVENT\nEND:VCALENDAR"
        with tempfile.TemporaryDirectory() as folder, patch("feeds.get_bytes",side_effect=response), \
             patch.dict("os.environ",{"TE_API_KEY":"fixture","FRED_API_KEY":"fixture"}):
            notes = sync_feeds(Path(folder),14)
            macro,events = load_context(Path(folder))
        self.assertEqual(len(macro),6)
        expected = pd.Timestamp("2026-11-06T06:00:00Z").value//1_000_000
        self.assertTrue(macro.known_ms.eq(expected).all())
        self.assertEqual(set(events.event_id),{"te-123","bls-A"})
        self.assertIn("first-release",notes["DFII10"])
    def test_quote_bundle_roundtrip_exact_including_same_millisecond(self):
        ticks = quote_frame({"ms":[2000,1000,1000],"bid":[102,100,101],"ask":[102.1,100.1,101.1]})
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder)/"market.zip"
            write_bundle(path,ticks,None,None,None,{"format":1,"kind":"test"})
            actual,*rest = read_bundle(path)
        pd.testing.assert_frame_equal(actual,ticks)
        self.assertEqual(list(actual.bid),[100.,101.,102.])


if __name__ == "__main__":
    unittest.main()
