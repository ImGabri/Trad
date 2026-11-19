"""XAUUSD AI scalper — launcher menu and command line.

    python main.py              interactive menu (what START.cmd opens)
    python main.py doctor       check MT5 connection, symbols, history
    python main.py prepare      download history, train AI, backtest, write report
    python main.py watch|demo   run in this window
    python main.py background start-demo|start-watch|status|stop
    python main.py kill         close all bot positions and halt
    python main.py smoke        offline end-to-end self-test (no MT5 needed)
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from pathlib import Path

APP = Path(__file__).resolve().parent
sys.path.insert(0, str(APP))
os.chdir(APP)


def _preload_vc_runtime():
    """LightGBM needs vcomp140.dll (OpenMP) + msvcp140.dll. PCs without the VC++ redistributable lack them,
    so load the copies that ship inside scikit-learn's wheel before lightgbm is imported."""
    if os.name != "nt":
        return
    import ctypes
    import importlib.util
    spec = importlib.util.find_spec("sklearn")
    if not spec or not spec.submodule_search_locations:
        return
    libs = Path(list(spec.submodule_search_locations)[0]) / ".libs"
    if libs.is_dir():
        os.add_dll_directory(str(libs))
        for dll in ("msvcp140.dll", "vcomp140.dll"):
            try:
                ctypes.WinDLL(str(libs / dll))
            except OSError:
                pass


_preload_vc_runtime()

from config import load_config  # noqa: E402

DATA = APP / "data"
VERSION = (APP / "VERSION").read_text().strip() if (APP / "VERSION").exists() else "dev"


def cfg_and_logs(console=True):
    from monitor import setup_logging
    cfg = load_config(APP / "config.yaml")
    log_dir = Path(cfg.monitoring.log_dir)
    setup_logging(log_dir if log_dir.is_absolute() else APP / log_dir, cfg.monitoring.log_retention_days, console)
    return cfg


# ----------------------------------------------------------------------------- commands
def doctor(cfg):
    from data import MT5Connection, mt5
    if mt5 is None:
        print("MetaTrader5 package missing — run this on Windows from START.cmd."); return 1
    conn = MT5Connection(cfg)
    if not conn.connect():
        print(f"✗ Cannot connect to MT5: {mt5.last_error()}\n  Open the PUPrime MT5 terminal, log in to your demo, "
              "and keep it running."); return 1
    acc, term = mt5.account_info(), mt5.terminal_info()
    gold = conn.resolve_gold(cfg.symbols.primary)
    suffix = gold[len(cfg.symbols.primary):]
    spec = conn.symbol_spec(gold)
    tick = mt5.symbol_info_tick(gold)
    demo = acc.trade_mode == mt5.ACCOUNT_TRADE_MODE_DEMO
    print(f"✓ Connected  {acc.company} | server {acc.server} | login {acc.login} | "
          f"{'DEMO' if demo else 'REAL ⚠'} | leverage 1:{acc.leverage} | equity {acc.equity:.2f} {acc.currency}")
    print(f"{'✓' if term.trade_allowed else '✗'} Algo Trading button {'ON' if term.trade_allowed else 'OFF — click it in MT5'}")
    print(f"✓ Gold symbol {gold}: contract {spec['contract_size']:g} oz, lot {spec['volume_min']}–{spec['volume_max']} "
          f"step {spec['volume_step']}, stops level {spec['stops_level']} pts, digits {spec['digits']}")
    if tick:
        m = mt5.order_calc_margin(mt5.ORDER_TYPE_BUY, gold, spec["volume_min"], tick.ask)
        print(f"  bid {tick.bid:.2f} ask {tick.ask:.2f} spread ${tick.ask - tick.bid:.2f} | margin for "
              f"{spec['volume_min']} lot ≈ {m:.2f} {acc.currency} | ping {term.ping_last / 1000:.0f} ms")
    print(f"  server time offset UTC{conn.server_offset.total_seconds() / 3600:+.0f}")
    for k in ("dxy", "us10y", "spx"):
        s = conn.resolve_symbol(getattr(cfg.symbols, k), suffix)
        print(f"  {k:6s}: {s or 'not available (feature left empty)'}")
    from datetime import datetime, timedelta, timezone
    now = datetime.now(timezone.utc)
    df = conn.rates_range(gold, "M1", now - timedelta(days=int(365.25 * 3)), now, chunk_days=90)
    print(f"  M1 history: {len(df):,} bars" + (f" from {df.index.min():%Y-%m-%d}" if len(df) else ""))
    print("✓ Model trained" if (DATA / "model" / "model.txt").exists() else "• No AI model yet — run option 2.")
    conn.shutdown()
    return 0


def prepare(cfg, years: float):
    from prepare import download, research
    print(f"Downloading up to {years:g} years of M1 history from MT5 …")
    m1, corr = download(cfg, DATA, years)
    research(cfg, DATA, m1, corr)
    open_report()


def import_csv(cfg, path: str, offset: float):
    from data import Store, load_csv_bars
    df = load_csv_bars(path, offset, cfg.backtest.spread_cost)
    Store(DATA / "xau.db").save_bars(cfg.symbols.primary, "M1", df)
    print(f"Imported {len(df):,} bars {df.index.min()} → {df.index.max()}. Option 2 merges them with MT5 data.")


def import_codex(cfg, paths):
    from data import Store, import_codex_bundle
    store = Store(DATA / "xau.db")
    for p in paths:
        print(p, import_codex_bundle(p.strip().strip('"'), store, DATA, cfg.symbols.primary))
    print("Imported. Option 2 merges this with MT5 history.")


def run_live(cfg, mode: str, keep_running=lambda: True):
    from live import Runner
    if not (DATA / "model" / "model.txt").exists():
        raise SystemExit("No AI model yet — run option 2 (prepare) first.")
    (DATA / "STOP").unlink(missing_ok=True)
    Runner(cfg, APP, mode, keep_running).run()


def worker():
    """Entry point of the background task: follows data/control.json, restarts the runner on errors."""
    import background
    cfg = cfg_and_logs(console=False)
    log = logging.getLogger("worker")
    gen = background.read_control(APP).get("generation")

    def active():
        c = background.read_control(APP)
        return bool(c.get("enabled")) and c.get("generation") == gen

    delay = 5
    from monitor import Status
    status = Status(DATA / "status.json")
    while active():
        try:
            run_live(cfg, background.read_control(APP)["mode"], active)
            if (DATA / "STOP").exists():
                break
        except SystemExit as e:
            status.update(state="waiting", detail=str(e))
        except Exception as e:  # noqa: BLE001
            log.exception("runner crashed")
            status.update(state="waiting", detail=str(e))
        t = time.monotonic() + delay
        while active() and time.monotonic() < t:
            time.sleep(1)
        delay = min(120, delay * 2)
        cfg = load_config(APP / "config.yaml")
    status.update(state="stopped")


def kill(cfg):
    import background
    (DATA / "STOP").parent.mkdir(exist_ok=True)
    (DATA / "STOP").touch()
    st = background.status(APP)
    if st.get("background") == "on" and st.get("worker") == "running":
        print("Kill switch set — the background worker is closing positions …")
        time.sleep(4)
    from data import MT5Connection, mt5
    import execution as ex
    conn = MT5Connection(cfg)
    if conn.connect():
        gold = conn.resolve_gold(cfg.symbols.primary)
        dev = int(cfg.risk.max_slippage_pips * cfg.instrument.pip_size / mt5.symbol_info(gold).point)
        n = ex.close_all(gold, dev, "kill")
        print(f"Closed {n} remaining bot positions.")
    background.stop(APP, quiet=True)
    print("Bot halted. Delete data/STOP (menu → reset) before starting again.")


def reset():
    (DATA / "STOP").unlink(missing_ok=True)
    p = DATA / "state.json"
    if p.exists():
        s = json.loads(p.read_text())
        s.update(hard_stop=False, drift_pause=False)
        p.write_text(json.dumps(s, indent=2))
    print("Kill switch, survival hard stop and drift pause cleared.")


def telegram_setup():
    from monitor import Telegram
    env = APP / ".env"
    token = input("Telegram bot token from @BotFather (blank = cancel): ").strip()
    if not token:
        return
    input("Now open your bot in Telegram, send it any message, then press Enter here …")
    tg = Telegram(token, "0")
    try:
        upd = tg._call("getUpdates", {"timeout": 0})
        chat = next(u["message"]["chat"]["id"] for u in reversed(upd["result"]) if "message" in u)
    except Exception as e:  # noqa: BLE001
        print(f"Could not read your chat id ({e}). Check the token and that you messaged the bot."); return
    lines = [l for l in (env.read_text().splitlines() if env.exists() else [])
             if not l.startswith(("TELEGRAM_TOKEN=", "TELEGRAM_CHAT_ID="))]
    env.write_text("\n".join(lines + [f"TELEGRAM_TOKEN={token}", f"TELEGRAM_CHAT_ID={chat}"]) + "\n")
    Telegram(token, chat).send("✅ XAU scalper connected. Commands: /status /pause /resume /stop", block=True)
    print(f"Saved. Test message sent to chat {chat}.")


def smoke():
    """Offline self-test: synthetic data → features → AI walk-forward → backtest → report."""
    sys.path.insert(0, str(APP / "tests"))
    from synthetic import synthetic_m1
    from prepare import research
    cfg = load_config(APP / "config.yaml", overrides={"backtest": {"wf_window_months": 1, "holdout_months": 1}})
    out = APP / "data_smoke"
    m1 = synthetic_m1(days=150)
    s = research(cfg, out, m1, {})
    assert "holdout" in s, "smoke: no holdout section"
    print("SMOKE OK")


def open_report():
    p = DATA / "report" / "report.html"
    if p.exists() and os.name == "nt":
        os.startfile(p)  # noqa: S606


def status_screen():
    import background
    from monitor import Status
    print(json.dumps(background.status(APP), indent=2))
    s = Status.read(DATA / "status.json")
    if s:
        print(f"last worker state: {s.get('state')} {s.get('detail', '')} ({s.get('heartbeat_age_s')} s ago)")
    rep = DATA / "report" / "summary.json"
    if rep.exists():
        m = json.loads(rep.read_text())["model"]
        print(f"model trained {m.get('trained_at', '')[:16]} on {m.get('rows', 0):,} bars, "
              f"backtest win rate {m.get('backtest_win_rate', 0):.1f}%")


MENU = f"""
=====  XAUUSD AI Scalper v{VERSION} — PUPrime MT5 (demo)  =====
 1  Check MT5 connection, symbols and history
 2  Download history, train AI and backtest (opens report)
 3  Watch mode in this window (signals only, no orders)
 4  Demo trading in this window
 5  Background: start DEMO trading (survives closing this window / reboot+logon)
 6  Background: start WATCH mode
 7  Background: status
 8  Background: stop
 9  KILL SWITCH — close all bot positions and halt
10  Reset kill switch / hard stop / drift pause
11  Set up Telegram alerts
12  Open last backtest report
 0  Exit
"""


def menu():
    import background
    while True:
        print(MENU)
        ch = input("Select: ").strip()
        try:
            if ch == "0":
                return
            cfg = cfg_and_logs()
            if ch == "1":
                doctor(cfg)
            elif ch == "2":
                y = input("Years of history [3]: ").strip() or "3"
                prepare(cfg, float(y))
            elif ch in ("3", "4"):
                print("Running — press Ctrl+C to stop.")
                try:
                    run_live(cfg, "watch" if ch == "3" else "demo")
                except KeyboardInterrupt:
                    print("Stopped. Open positions keep their broker-side SL/TP.")
            elif ch in ("5", "6"):
                background.start(APP, "demo" if ch == "5" else "watch")
                time.sleep(3)
                print(json.dumps(background.status(APP), indent=2))
                print("You can close this window. The runner starts again whenever you log in to Windows.")
            elif ch == "7":
                status_screen()
            elif ch == "8":
                background.stop(APP)
                print("Background runner stopped. Open positions keep their SL/TP.")
            elif ch == "9":
                if input("Type KILL to close all bot positions: ").strip() == "KILL":
                    kill(cfg)
            elif ch == "10":
                reset()
            elif ch == "11":
                telegram_setup()
            elif ch == "12":
                open_report()
            elif ch == "13":
                ps = input("Path(s) to market_data.zip and/or markets.csv (separate with ;): ").split(";")
                import_codex(cfg, [p for p in ps if p.strip()])
        except SystemExit as e:
            print(e)
        except Exception as e:  # noqa: BLE001
            logging.getLogger("menu").exception("error")
            print(f"Error: {e}")
        input("\nPress Enter to return to the menu …")


def cli():
    ap = argparse.ArgumentParser(description="XAUUSD AI scalper")
    sub = ap.add_subparsers(dest="cmd")
    sub.add_parser("menu"); sub.add_parser("doctor"); sub.add_parser("watch"); sub.add_parser("demo")
    sub.add_parser("worker"); sub.add_parser("kill"); sub.add_parser("reset"); sub.add_parser("smoke")
    sub.add_parser("status")
    p = sub.add_parser("prepare"); p.add_argument("--years", type=float, default=3)
    p = sub.add_parser("import-csv"); p.add_argument("path"); p.add_argument("--utc-offset", type=float, default=0)
    p = sub.add_parser("import-codex"); p.add_argument("paths", nargs="+")
    p = sub.add_parser("background"); p.add_argument("action", choices=["start-demo", "start-watch", "status", "stop"])
    a = ap.parse_args()
    if a.cmd in (None, "menu"):
        return menu()
    if a.cmd == "worker":
        return worker()
    if a.cmd == "smoke":
        return smoke()
    if a.cmd == "reset":
        return reset()
    if a.cmd == "status":
        return status_screen()
    cfg = cfg_and_logs()
    if a.cmd == "doctor":
        return doctor(cfg)
    if a.cmd == "prepare":
        return prepare(cfg, a.years)
    if a.cmd == "import-csv":
        return import_csv(cfg, a.path, a.utc_offset)
    if a.cmd in ("watch", "demo"):
        return run_live(cfg, a.cmd)
    if a.cmd == "import-codex":
        return import_codex(cfg, a.paths)
    if a.cmd == "kill":
        return kill(cfg)
    if a.cmd == "background":
        import background
        if a.action.startswith("start"):
            background.start(APP, a.action.split("-")[1])
        elif a.action == "stop":
            background.stop(APP)
        print(json.dumps(background.status(APP), indent=2))


if __name__ == "__main__":
    sys.exit(cli() or 0)
