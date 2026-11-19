"""Command-line entry point and interactive Windows launcher."""
from __future__ import annotations

import argparse
import json
import math
from pathlib import Path

import broker
import background
from engine import run, single_runner
from feeds import load_context, sync_feeds
from journal import Journal
from research import iso, read_bundle, synthetic_data, train, write_bundle
from strategy import DAY, MINUTE

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"
MARKET_FILE = DATA / "market_data.zip"
REPORTS = DATA / "research"


def settings():
    cfg = json.loads((ROOT/"settings.json").read_text(encoding="utf8"))
    s, d = cfg["strategy"],cfg["demo"]
    for name, value in {**s,**d}.items():
        if type(value) not in (int,float) or not math.isfinite(value) or value < 0:
            raise ValueError(f"Invalid numeric setting: {name}")
    for key in ("hold_seconds","entry_window_seconds","trigger_bucket_ms","max_entries_utc_day"):
        if type(s[key]) is not int or s[key] < 1:
            raise ValueError(f"{key} needs a positive whole number.")
    if not (0 < d["risk_fraction"] <= 1 and 0 < d["daily_equity_stop_fraction"] <= 1 and d["max_lots"] > 0):
        raise ValueError("Demo risk fractions must be positive and at most 1. Set a positive lot cap.")
    if not (0 < s["min_model_score"] < 1 and s["minimum_stop_usd_oz"] > 0 and s["target_multiple"] > 0):
        raise ValueError("Check the model threshold, minimum stop and target multiple.")
    if (s["entry_window_seconds"]*1000 < s["latency_ms"] or type(d["deviation_points"]) is not int
            or type(d["poll_ms"]) is not int or d["poll_ms"] < 1 or d["max_quote_age_seconds"] <= 0):
        raise ValueError("Check quote, latency and polling settings.")
    return cfg


def capture(cfg, path, days):
    if days < 1:
        raise ValueError("History days must be positive.")
    print("Refreshing scheduled releases and optional macro sources...", flush=True)
    print(json.dumps(sync_feeds(DATA,days),indent=2),flush=True)
    macro, events = load_context(DATA)
    with broker.connection(cfg) as (api,identity,symbol,cross):
        end = broker.now_ms()//MINUTE*MINUTE
        start = end-days*DAY
        print(f"Downloading {symbol} bid/ask quotes from {iso(start)} to {iso(end)}...",flush=True)
        ticks = broker.tick_data(api,symbol,start,end)
        if ticks.empty:
            raise ValueError("The demo terminal supplied no bid/ask history. Open the symbol in MT5, sync its history and retry.")
        market = broker.market_data(api,cross,start-75*MINUTE,end)
        source = broker.source_data(api,identity,symbol,cross)
        write_bundle(path,ticks,market,macro,events,source)
    print(f"{len(ticks):,} quotes saved in {path}",flush=True)
    return ticks,market,macro,events,source


def print_report(r, path):
    print(f"Data source: {r['source']['kind']} / {r['source']['symbol']}")
    print(f"Quotes: {r['quotes']:,} | triggers: {r['qualifying_quote_triggers']:,} | model inputs: {len(r['feature_coverage'])}")
    print("Out-of-sample results for one ounce per trade, in USD:")
    for key,title in (("ai","Adaptive AI"),("frozen_ai_same_costs","Frozen AI"),("five_minute_momentum_baseline","Momentum")):
        m = r[key]
        print(f"  {title:12s}  trades {m['trades']:4d}  net {m['net_usd_per_oz']:9.3f}  closed drawdown {m['closed_trade_drawdown_usd_per_oz']:.3f}")
    print("Report and trade lists:",path)


def dispatch(command, cfg, bundle=None, days=None, output=None, release=None):
    days = cfg["history_days"] if days is None else days
    bundle = Path(bundle or MARKET_FILE)
    output = Path(output or REPORTS)
    if command == "feeds":
        print(json.dumps(sync_feeds(DATA,days),indent=2))
    elif command in ("capture","prepare"):
        dataset = capture(cfg,bundle,days)
        if command == "prepare":
            print("Training and testing successive time periods...",flush=True)
            print_report(train(*dataset,cfg["strategy"],output),output)
    elif command == "backtest":
        print_report(train(*read_bundle(bundle),cfg["strategy"],output),output)
    elif command == "smoke":
        path = output / "synthetic_test"
        print("Generated quotes only. These figures exercise the software.",flush=True)
        print_report(train(*synthetic_data(1),cfg["strategy"],path),path)
    elif command in ("watch","demo"):
        modelpath = output/"model.json"
        if not modelpath.is_file():
            raise ValueError("Run option 2 (prepare) first to train on your demo feed.")
        artifact = json.loads(modelpath.read_text(encoding="utf8"))
        with broker.connection(cfg) as conn:
            run(conn,cfg,artifact,DATA,command=="demo")
    elif command in ("doctor","reviews","reconcile"):
        with broker.connection(cfg) as (api,identity,symbol,markets):
            a = broker.account(api,identity)
            if command == "doctor":
                status = broker.source_data(api,identity,symbol,markets)
                info, terminal, tick = api.symbol_info(symbol),api.terminal_info(),api.symbol_info_tick(symbol)
                status.update(account_mode="DEMO", account_currency=a.currency,
                              bid=getattr(tick,"bid",None), ask=getattr(tick,"ask",None),
                              last_quote_utc=iso(tick.time_msc) if tick else None,
                              quote_age_seconds=round((broker.now_ms()-tick.time_msc)/1000,2) if tick else None,
                              broker_min_stop_points=info.trade_stops_level,
                              orders_enabled=bool(a.trade_allowed and a.trade_expert and terminal.trade_allowed and not terminal.tradeapi_disabled))
                print(json.dumps(status,indent=2))
            elif command == "reconcile":
                key = broker.account_key(identity)
                with single_runner(key):
                    journal = Journal(DATA/"demo.sqlite",key,symbol)
                    try:
                        pos, orders = broker.exposure(api,symbol)
                        journal.reconcile(api,pos,orders,a.currency)
                        if release:
                            broker.account(api,identity)
                            journal.release_absent(api,release,pos,orders)
                        print(json.dumps(journal.unresolved(),indent=2))
                    finally:
                        journal.db.close()
            else:
                journal = Journal(DATA/"demo.sqlite",broker.account_key(identity),symbol)
                try:
                    pos, orders = broker.exposure(api,symbol)
                    for review in journal.reconcile(api,pos,orders,a.currency):
                        print(json.dumps(review))
                    reviews = journal.reviews()
                    if reviews.empty:
                        print("No completed bot trades on this demo account yet.")
                    else:
                        path = DATA/"trade_reviews.csv"
                        reviews.to_csv(path,index=False)
                        print(f"{len(reviews)} closed-trade reviews saved in {path}")
                finally:
                    journal.db.close()
    else:
        raise ValueError(f"Unknown command {command}.")


def menu():
    choices = {"1":"doctor","2":"prepare","3":"watch","4":"demo","5":"reviews","6":"smoke","7":"capture","8":"background"}
    while True:
        print("\nPU Prime | XAUUSD demo scalper\n"
              "1  Check demo connection and symbol\n"
              "2  Download market data, train AI and backtest\n"
              "3  Watch signals\n"
              "4  Start automatic demo orders\n"
              "5  Export closed-trade reviews\n"
              "6  Offline software check with generated quotes\n"
              "7  Export market data only\n8  Manage Windows background runner\n0  Quit")
        choice = input("Select: ").strip()
        if choice == "0":
            return
        if choice not in choices:
            continue
        try:
            if choice == "8":
                print("1  Watch in the background\n2  Enable automatic demo orders in the background\n"
                      "3  Show status\n4  Stop and remove startup task\n0  Back")
                actions = {"1":"watch","2":"demo","3":"status","4":"stop"}
                action = actions.get(input("Select: ").strip())
                if action:
                    print(json.dumps(background.manage(action,ROOT,DATA,settings()),indent=2))
            else:
                dispatch(choices[choice],settings())
        except KeyboardInterrupt:
            print("\nStopped. Any open demo position is visible in MT5.")
        except (OSError,ValueError,RuntimeError,KeyError) as exc:
            print(f"Could not complete this step: {exc}")


if __name__ == "__main__":
    cli = argparse.ArgumentParser(description="PU Prime XAUUSD demo scalper")
    cli.add_argument("command",choices=("menu","doctor","prepare","capture","backtest","watch","demo","reviews","reconcile","feeds","smoke","background","worker"))
    cli.add_argument("--mode",choices=("watch","demo","status","stop"),default="status",help="Windows background action")
    cli.add_argument("--bundle",help="Market-only ZIP for capture, prepare or offline replay")
    cli.add_argument("--days",type=int,help="Calendar days of broker history to download")
    cli.add_argument("--output",help="Research/model directory")
    cli.add_argument("--release",metavar="TAG",help="With reconcile, retire a verified absent ambiguous request")
    args = cli.parse_args()
    try:
        if args.command == "menu":
            menu()
        elif args.command == "background":
            print(json.dumps(background.manage(args.mode,ROOT,DATA,settings()),indent=2))
        elif args.command == "worker":
            background.supervise(ROOT,DATA,settings,background.local_state())
        else:
            dispatch(args.command,settings(),args.bundle,args.days,args.output,args.release)
    except KeyboardInterrupt:
        print("\nStopped. Any open demo position is visible in MT5.")
    except (OSError,ValueError,RuntimeError,KeyError) as exc:
        cli.exit(1,f"{exc}\n")
