"""Data pipeline: MT5 connection, bar/tick download, symbol resolution, SQLite storage, CSV/yfinance fallbacks.

All timestamps leaving this module are tz-aware UTC and refer to the bar OPEN time.
MT5 returns broker *server* time (PUPrime is typically UTC+2 winter / UTC+3 summer), so every
MT5 timestamp is shifted by the detected or configured server offset.
"""
from __future__ import annotations

import logging
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable

import numpy as np
import pandas as pd

try:  # Windows-only package; module stays importable for backtests on any OS.
    import MetaTrader5 as mt5
except ImportError:  # pragma: no cover
    mt5 = None

log = logging.getLogger(__name__)

# Austria timezone (Europe/Vienna): UTC+1 winter, UTC+2 summer (DST handled by MT5 server offset)
AUSTRIA_TZ_NAME = "Europe/Vienna"

TIMEFRAMES = {"M1": "TIMEFRAME_M1", "M5": "TIMEFRAME_M5", "M15": "TIMEFRAME_M15", "H1": "TIMEFRAME_H1"}
TF_MINUTES = {"M1": 1, "M5": 5, "M15": 15, "H1": 60}
RATES_TAIL = 16           # bars re-read per update of a cached window (≥ 1 bar of overlap after up to 14 new ones)
RATES_FULL_REFRESH = 60   # a cached window is downloaded in full again after this many tail updates
SYMBOLS_TTL_S = 300       # mt5.symbols_get() is read once per connect (it lists every symbol of the server)


# --------------------------------------------------------------------------------------- MT5
class MT5Connection:
    """Owns the MT5 IPC session with retry/reconnect logic (§7: retry every 30 s for up to 10 min)."""

    def __init__(self, cfg):
        if mt5 is None:
            raise RuntimeError("MetaTrader5 package not installed (Windows + MT5 terminal required)")
        self.cfg = cfg
        self.server_offset: timedelta | None = None
        self.generation = 0            # +1 on every successful (re)connect: invalidates the caches below
        self._rates_cache: dict = {}   # (symbol, tf, count) -> {"df", "gen", "updates"}
        self._symbols: tuple[float, list] | None = None

    def connect(self) -> bool:
        s = self.cfg.secrets
        kwargs = {}
        if self.cfg.broker.terminal_path:
            kwargs["path"] = self.cfg.broker.terminal_path
        if s.mt5_login:
            kwargs.update(login=s.mt5_login, password=s.mt5_password, server=s.mt5_server)
        ok = self._initialize_with_timeout(kwargs, float(getattr(self.cfg.monitoring, "connect_timeout_s", 90)))
        if not ok:
            log.error("mt5.initialize failed: %s", self.last_error or mt5.last_error())
            return False
        info = mt5.account_info()
        term = mt5.terminal_info()
        if info is None or term is None:
            return False
        if not term.trade_allowed:
            log.warning("Algo Trading is disabled in the terminal — enable the green 'Algo Trading' button")
        log.info("Connected: login=%s server=%s balance=%.2f leverage=1:%s",
                 info.login, info.server, info.balance, info.leverage)
        self.generation += 1
        self._rates_cache.clear()
        self._symbols = None
        self._resolve_offset()
        return True

    last_error = ""

    def _initialize_with_timeout(self, kwargs: dict, timeout_s: float) -> bool:
        """mt5.initialize can block for ever when the terminal shows a dialog, is updating or is not logged in.
        Run it in a helper thread and give up after timeout_s with a clear message instead of hanging silently."""
        import threading
        box = {}
        th = threading.Thread(target=lambda: box.setdefault("ok", bool(mt5.initialize(**kwargs))), daemon=True)
        th.start()
        th.join(timeout_s)
        if th.is_alive():
            self.last_error = (f"MT5 did not answer within {int(timeout_s)} s. Open the PU Prime MT5 window: close any "
                               "pop-up/update dialog, make sure you are logged in to your demo account, then press STOP "
                               "and START.")
            return False
        self.last_error = "" if box.get("ok") else str(mt5.last_error())
        return bool(box.get("ok"))

    def is_alive(self) -> bool:
        return mt5 is not None and mt5.terminal_info() is not None and mt5.account_info() is not None

    def ensure_connected(self, on_give_up=None) -> bool:
        if self.is_alive():
            return True
        interval = self.cfg.monitoring.reconnect_interval_s
        deadline = time.monotonic() + self.cfg.monitoring.reconnect_timeout_s
        while time.monotonic() < deadline:
            log.warning("MT5 IPC down, reconnecting…")
            mt5.shutdown()
            if self.connect():
                return True
            time.sleep(interval)
        log.critical("MT5 reconnection failed for %ss", self.cfg.monitoring.reconnect_timeout_s)
        if on_give_up:
            on_give_up()
        return False

    def shutdown(self):
        mt5.shutdown()

    # Server time handling -------------------------------------------------------------
    def _resolve_offset(self):
        cfg_off = self.cfg.broker.server_utc_offset_hours
        if cfg_off != "auto":
            self.server_offset = timedelta(hours=int(cfg_off))
            return
        
        # Check if user wants to skip market open check for uninterrupted trading
        if self.cfg.strategy.skip_market_open_check:
            # Use configured offset or default Austria-appropriate offset
            if cfg_off != "auto":
                self.server_offset = timedelta(hours=int(cfg_off))
            else:
                # Default to UTC+2 for PU Prime gold, adjustable for Austria timezone
                log.info("skip_market_open_check enabled — using default offset UTC+2")
                self.server_offset = timedelta(hours=2)
            return
        
        try:  # the account's real gold symbol (e.g. XAUUSD.s) — plain "XAUUSD" may not quote on this account
            sym = self.resolve_gold(self.cfg.symbols.primary)
        except RuntimeError:
            sym = self.cfg.symbols.primary
        mt5.symbol_select(sym, True)
        tick = mt5.symbol_info_tick(sym)
        if tick is None or _market_closed_now():
            log.warning("Cannot auto-detect server offset (market closed?) — assuming UTC+2")
            self.server_offset = timedelta(hours=2)
            return
        hours = round((tick.time - time.time()) / 3600)
        self.server_offset = timedelta(hours=hours)
        log.info("Detected MT5 server offset UTC%+d", hours)

    def to_utc(self, server_epoch: np.ndarray | pd.Series) -> pd.DatetimeIndex:
        idx = pd.to_datetime(np.asarray(server_epoch), unit="s", utc=True)
        return idx - (self.server_offset or timedelta(0))

    def to_server(self, dt_utc: datetime) -> datetime:
        # copy_*_range interprets naive datetimes as UTC epoch; feed it server wall time.
        return (dt_utc + (self.server_offset or timedelta(0))).replace(tzinfo=timezone.utc)

    # Symbols ------------------------------------------------------------------------------
    def symbols(self) -> list:
        """mt5.symbols_get(), read once per connection (startup resolves gold and ~30 context markets from it)."""
        now = time.monotonic()
        if self._symbols is None or now - self._symbols[0] > SYMBOLS_TTL_S:
            self._symbols = (now, list(mt5.symbols_get() or []))
        return self._symbols[1]

    def resolve_symbol(self, candidates: Iterable[str], suffix: str = "", names: dict | None = None) -> str | None:
        """First candidate present on the server. PU Prime adds account-type suffixes (e.g. XAUUSD.s),
        so each alias is tried with the primary symbol's suffix first, then bare, then any suffix."""
        if names is None:
            names = {s.name.upper(): s.name for s in self.symbols()}
        for c in candidates:
            for name in dict.fromkeys([c + suffix, c]):
                real = names.get(name.upper())
                if real and mt5.symbol_select(real, True):
                    return real
        for c in candidates:
            for up, real in names.items():
                if up.startswith(c.upper() + ".") and mt5.symbol_select(real, True):
                    return real
        return None

    def resolve_context(self, cfg, suffix: str) -> dict[str, str | None]:
        """{market: PU Prime symbol or None} for every market in config `context`."""
        ctx = getattr(cfg, "context", None)
        names = vars(ctx) if ctx is not None else {}
        allnames = {s.name.upper(): s.name for s in self.symbols()}  # read the symbol list ONCE
        return {k: self.resolve_symbol(v.mt5, suffix, allnames) for k, v in names.items()}

    def resolve_gold(self, wanted: str = "XAUUSD") -> str:
        """Standard 24/5 gold with full trading rights; never the XAUUSD247 contract."""
        full = getattr(mt5, "SYMBOL_TRADE_MODE_FULL", 4)
        hits = [s for s in self.symbols()
                if s.name.upper().startswith(wanted.upper()) and "247" not in s.name and s.trade_mode == full]
        exact = [s for s in hits if s.name.upper() == wanted.upper()]
        pick = (exact or sorted(hits, key=lambda s: len(s.name)))[:1]
        if not pick:
            raise RuntimeError(f"No tradable {wanted} symbol (excluding XAUUSD247) on this server")
        mt5.symbol_select(pick[0].name, True)
        return pick[0].name

    def symbol_spec(self, symbol: str) -> dict:
        si = mt5.symbol_info(symbol)
        if si is None:
            raise ValueError(f"symbol {symbol} not found")
        return dict(name=si.name, digits=si.digits, point=si.point, contract_size=si.trade_contract_size,
                    volume_min=si.volume_min, volume_step=si.volume_step, volume_max=si.volume_max,
                    stops_level=si.trade_stops_level, freeze_level=si.trade_freeze_level,
                    filling_mode=si.filling_mode, trade_mode=si.trade_mode, spread=si.spread,
                    tick_value=si.trade_tick_value, tick_size=si.trade_tick_size,
                    currency_profit=si.currency_profit, path=si.path)

    # Downloads --------------------------------------------------------------------------
    def rates_range(self, symbol: str, tf: str, start: datetime, end: datetime,
                    chunk_days: int = 30) -> pd.DataFrame:
        tf_const = getattr(mt5, TIMEFRAMES[tf])
        frames, cur = [], start
        while cur < end:
            nxt = min(cur + timedelta(days=chunk_days), end)
            arr = mt5.copy_rates_range(symbol, tf_const, self.to_server(cur), self.to_server(nxt))
            if arr is not None and len(arr):
                frames.append(pd.DataFrame(arr))
            cur = nxt
        if not frames:
            log.warning("No %s %s data %s→%s: %s", symbol, tf, start, end, mt5.last_error())
            return _empty_bars()
        return self._bars_frame(pd.concat(frames, ignore_index=True), symbol)

    def rates_latest(self, symbol: str, tf: str, count: int) -> pd.DataFrame:
        arr = mt5.copy_rates_from_pos(symbol, getattr(mt5, TIMEFRAMES[tf]), 0, count)
        if arr is None or not len(arr):
            return _empty_bars()
        df = self._bars_frame(pd.DataFrame(arr), symbol)
        return df.iloc[:-1]  # last bar is still forming — never use it for signals

    def rates_latest_cached(self, symbol: str, tf: str, count: int) -> pd.DataFrame:
        """Same frame as rates_latest(symbol, tf, count), without re-downloading the whole window on every call:
        the closed bars are kept and only the newest RATES_TAIL bars are read and appended. The re-read bars must
        overlap the kept ones exactly, otherwise (gap of more than RATES_TAIL - 2 bars, a revised bar, a shorter
        history than `count`) it downloads the full window, as it also does after a reconnect and every
        RATES_FULL_REFRESH updates."""
        key = (symbol, tf, count)
        c = self._rates_cache.get(key)
        if c is not None and c["gen"] == self.generation and c["updates"] < RATES_FULL_REFRESH:
            merged = _append_overlapping(c["df"], self.rates_latest(symbol, tf, RATES_TAIL))
            if merged is not None:
                c["df"], c["updates"] = merged.iloc[-(count - 1):], c["updates"] + 1
                return c["df"]
        df = self.rates_latest(symbol, tf, count)
        if len(df) == count - 1:  # a full window: later calls can extend it
            self._rates_cache[key] = dict(df=df, gen=self.generation, updates=0)
        else:  # history still loading / shorter than asked: never extend a partial window
            self._rates_cache.pop(key, None)
        return df

    def ticks_range(self, symbol: str, start: datetime, end: datetime) -> pd.DataFrame:
        arr = mt5.copy_ticks_range(symbol, self.to_server(start), self.to_server(end), mt5.COPY_TICKS_ALL)
        if arr is None or not len(arr):
            return pd.DataFrame(columns=["bid", "ask", "last", "volume", "flags"])
        df = pd.DataFrame(arr)
        df.index = self.to_utc(df["time_msc"] / 1000.0)
        df.index.name = "time"
        return df[["bid", "ask", "last", "volume", "flags"]]

    def _bars_frame(self, df: pd.DataFrame, symbol: str) -> pd.DataFrame:
        df = df.drop_duplicates("time")
        df.index = self.to_utc(df["time"])
        df.index.name = "time"
        si = mt5.symbol_info(symbol)
        point = si.point if si else 0.01
        out = df[["open", "high", "low", "close", "tick_volume"]].astype(float)
        out["spread"] = df["spread"].astype(float) * point  # MT5 bar spread is in points → convert to $
        return out.sort_index()


def _append_overlapping(old: pd.DataFrame, new: pd.DataFrame) -> pd.DataFrame | None:
    """old + the bars of `new` after old's last bar, if new starts inside old and every overlapping bar is
    identical (same times and values) to old's last bars; None when that cannot be proven (→ full download)."""
    if old.empty or new.empty or list(new.columns) != list(old.columns) or new.index.dtype != old.index.dtype:
        return None
    last = old.index[-1]
    ov = new[new.index <= last]
    if len(ov) == 0 or len(ov) > len(old):
        return None
    tail = old.iloc[-len(ov):]
    if not (tail.index.equals(ov.index) and all(tail[c].dtype == ov[c].dtype for c in old.columns)
            and np.array_equal(tail.to_numpy(), ov.to_numpy(), equal_nan=True)):
        return None
    add = new.iloc[len(ov):]
    if len(add) and not (add.index[0] > last and add.index.is_monotonic_increasing):
        return None
    return old if add.empty else pd.concat([old, add])


def remember_terminal(data_dir: Path):
    """Save the running terminal's path so the background worker can launch MT5 itself after a reboot."""
    import json
    ti = mt5.terminal_info() if mt5 else None
    if ti is None:
        return
    p = Path(data_dir) / "local.json"
    try:
        cur = json.loads(p.read_text()) if p.exists() else {}
    except ValueError:
        cur = {}
    exe = str(Path(ti.path) / "terminal64.exe")
    if cur.get("terminal_path") != exe:
        cur["terminal_path"] = exe
        p.write_text(json.dumps(cur, indent=2))


def acct_per_usd(symbol: str) -> float:
    """Account-currency value of a $1 P&L (e.g. ≈0.86 for a EUR account). MT5 does the FX conversion."""
    if mt5 is None:
        return 1.0
    acc = mt5.account_info()
    if acc is None or str(acc.currency).upper() == "USD":
        return 1.0
    si, tick = mt5.symbol_info(symbol), mt5.symbol_info_tick(symbol)
    if si is None or tick is None or not tick.bid:
        return 1.0
    prof = mt5.order_calc_profit(mt5.ORDER_TYPE_BUY, symbol, 1.0, tick.bid, tick.bid + 1.0)
    return float(prof) / si.trade_contract_size if prof else 1.0


def account_equity_usd(symbol: str) -> float | None:
    acc = mt5.account_info() if mt5 else None
    if acc is None:
        return None
    return float(acc.equity) / acct_per_usd(symbol)


def _market_closed_now() -> bool:
    now = datetime.now(timezone.utc)
    return now.weekday() == 5 or (now.weekday() == 4 and now.hour >= 22) or (now.weekday() == 6 and now.hour < 22)


def _epoch(idx: pd.DatetimeIndex, unit_s: float) -> pd.Index:
    return ((idx - pd.Timestamp(0, tz="UTC")) / pd.Timedelta(seconds=unit_s)).astype("int64")


def _empty_bars() -> pd.DataFrame:
    return pd.DataFrame(columns=["open", "high", "low", "close", "tick_volume", "spread"],
                        index=pd.DatetimeIndex([], tz="UTC", name="time"))


# ------------------------------------------------------------------------------ tick features
def tick_microstructure(ticks: pd.DataFrame, freq: str = "1min", window: int = 20) -> pd.DataFrame:
    """Per-bar microstructure from MT5 ticks.

    Gold CFDs carry no exchange volume, so order flow is approximated from quote changes:
    an uptick in mid = buyer-initiated, downtick = seller-initiated (tick rule).
    - trade_dir_ratio: share of upticks among the last `window` direction-changing ticks
    - ofi: Cont–Kukanov-style order-flow imbalance from bid/ask moves, summed per bar
    """
    if ticks.empty:
        return pd.DataFrame(columns=["spread_mean", "spread_max", "tick_count", "trade_dir_ratio", "ofi"])
    t = ticks.copy()
    mid = (t["bid"] + t["ask"]) / 2
    d = np.sign(mid.diff()).fillna(0)
    moved = d.replace(0, np.nan)
    ups = (moved > 0).astype(float).where(moved.notna())
    t["dir_ratio"] = ups.rolling(window, min_periods=1).mean().ffill()
    t["ofi"] = np.sign(t["bid"].diff()).fillna(0) + np.sign(t["ask"].diff()).fillna(0)
    t["spread"] = t["ask"] - t["bid"]
    g = t.resample(freq, label="left", closed="left")
    return pd.DataFrame({
        "spread_mean": g["spread"].mean(),
        "spread_max": g["spread"].max(),
        "tick_count": g["bid"].count(),
        "trade_dir_ratio": g["dir_ratio"].last(),
        "ofi": g["ofi"].sum(),
    }).dropna(subset=["spread_mean"])


# ---------------------------------------------------------------------------------- storage
class Store:
    """SQLite store: bars, spread log, trade journal, model predictions."""

    SCHEMA = """
    CREATE TABLE IF NOT EXISTS bars (symbol TEXT, tf TEXT, time INTEGER, open REAL, high REAL, low REAL,
        close REAL, tick_volume REAL, spread REAL, PRIMARY KEY (symbol, tf, time));
    CREATE TABLE IF NOT EXISTS spread_log (time_ms INTEGER, symbol TEXT, bid REAL, ask REAL, spread REAL);
    CREATE INDEX IF NOT EXISTS ix_spread_time ON spread_log(time_ms);
    CREATE TABLE IF NOT EXISTS trades (ticket INTEGER PRIMARY KEY, symbol TEXT, side TEXT, lots REAL,
        open_time TEXT, open_price REAL, sl REAL, tp REAL, spread_at_entry REAL, slippage REAL,
        confidence REAL, session TEXT, reason_open TEXT, close_time TEXT, close_price REAL,
        pnl REAL, commission REAL, reason_close TEXT, mode TEXT, features TEXT);
    CREATE TABLE IF NOT EXISTS predictions (time TEXT PRIMARY KEY, p_up REAL, p_down REAL,
        confidence REAL, model_version TEXT, signal INTEGER, acted INTEGER);
    """

    def __init__(self, path: str | Path):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(path), check_same_thread=False)
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.executescript(self.SCHEMA)
        cols = {r[1] for r in self.conn.execute("PRAGMA table_info(trades)")}
        if "features" not in cols:  # databases created by older versions
            self.conn.execute("ALTER TABLE trades ADD COLUMN features TEXT")
            self.conn.commit()

    def save_bars(self, symbol: str, tf: str, df: pd.DataFrame):
        if df.empty:
            return
        epoch = _epoch(df.index, 1)
        rows = zip([symbol] * len(df), [tf] * len(df), epoch.tolist(), *(df[c].tolist() for c in
                   ["open", "high", "low", "close", "tick_volume", "spread"]))
        self.conn.executemany("INSERT OR REPLACE INTO bars VALUES (?,?,?,?,?,?,?,?,?)", rows)
        self.conn.commit()

    def load_bars(self, symbol: str, tf: str, start: datetime | None = None, end: datetime | None = None) -> pd.DataFrame:
        q, p = "SELECT time, open, high, low, close, tick_volume, spread FROM bars WHERE symbol=? AND tf=?", [symbol, tf]
        if start:
            q += " AND time>=?"; p.append(int(start.timestamp()))
        if end:
            q += " AND time<?"; p.append(int(end.timestamp()))
        df = pd.read_sql_query(q + " ORDER BY time", self.conn, params=p)
        df.index = pd.to_datetime(df.pop("time").astype("int64"), unit="s", utc=True)
        df.index.name = "time"
        # Always numeric: an EMPTY query returns object columns, and with pandas 3 concatenating that with real
        # bars turns every price column into object ("unorderable types" crash in features).
        return df.apply(pd.to_numeric, errors="coerce").astype("float64")

    def last_bar_time(self, symbol: str, tf: str) -> datetime | None:
        r = self.conn.execute("SELECT MAX(time) FROM bars WHERE symbol=? AND tf=?", (symbol, tf)).fetchone()
        return datetime.fromtimestamp(r[0], timezone.utc) if r and r[0] else None

    def log_spreads(self, symbol: str, ticks: pd.DataFrame):
        if ticks.empty:
            return
        ms = _epoch(ticks.index, 1e-3).tolist()
        self.conn.executemany("INSERT INTO spread_log VALUES (?,?,?,?,?)",
                              zip(ms, [symbol] * len(ticks), ticks["bid"].tolist(), ticks["ask"].tolist(),
                                  (ticks["ask"] - ticks["bid"]).tolist()))
        self.conn.commit()

    def record_trade(self, **row):
        cols = ",".join(row)
        self.conn.execute(f"INSERT OR REPLACE INTO trades ({cols}) VALUES ({','.join('?' * len(row))})",
                          list(row.values()))
        self.conn.commit()

    def record_prediction(self, **row):
        cols = ",".join(row)
        self.conn.execute(f"INSERT OR REPLACE INTO predictions ({cols}) VALUES ({','.join('?' * len(row))})",
                          list(row.values()))
        self.conn.commit()


def sync_history(conn: MT5Connection, store: Store, symbol: str, tfs=("M1", "M5", "M15", "H1"),
                 years: float = 3.0) -> dict:
    """Incrementally download bars into SQLite. Returns {tf: (first, last, rows)}."""
    now = datetime.now(timezone.utc)
    report = {}
    for tf in tfs:
        last = store.last_bar_time(symbol, tf)
        start = last if last else now - timedelta(days=int(365.25 * years))
        df = conn.rates_range(symbol, tf, start, now)
        store.save_bars(symbol, tf, df)
        full = store.load_bars(symbol, tf)
        report[tf] = (full.index.min(), full.index.max(), len(full))
        log.info("%s %s: %s rows %s → %s", symbol, tf, len(full), full.index.min(), full.index.max())
    return report


# --------------------------------------------------------------------------------- offline data
def load_csv_bars(path: str | Path, tz_offset_hours: float = 0.0, default_spread: float | None = None) -> pd.DataFrame:
    """Load M1 bars from CSV (MT5 export, Dukascopy, HistData…). Needs a datetime column (or date+time)
    plus open/high/low/close; volume and spread optional. Use when the broker's M1 history < 3 years."""
    df = pd.read_csv(path, sep=None, engine="python")
    df.columns = [c.strip("<>").lower() for c in df.columns]
    if "date" in df.columns and "time" in df.columns:
        ts = pd.to_datetime(df["date"].astype(str) + " " + df["time"].astype(str))
    else:
        col = next(c for c in df.columns if c in ("datetime", "time", "timestamp", "gmt time", "date"))
        ts = pd.to_datetime(df[col], dayfirst=False)
    ts = ts - pd.Timedelta(hours=tz_offset_hours)
    df.index = pd.DatetimeIndex(ts).tz_localize("UTC") if ts.dt.tz is None else pd.DatetimeIndex(ts).tz_convert("UTC")
    df.index.name = "time"
    vol = next((c for c in ("tick_volume", "tickvol", "volume", "vol") if c in df.columns), None)
    out = df[["open", "high", "low", "close"]].astype(float)
    out["tick_volume"] = df[vol].astype(float) if vol else 1.0
    out["spread"] = df["spread"].astype(float) if "spread" in df.columns else (default_spread or np.nan)
    return out[~out.index.duplicated()].sort_index()


def fetch_yfinance(ticker: str, interval: str = "1m", period: str = "7d") -> pd.Series:
    """Correlated-asset fallback. NOTE: Yahoo keeps only ~7 days of 1m and ~60 days of 5m bars, so it
    is suitable for live features but NOT for a 3-year training set."""
    import yfinance as yf  # optional dependency
    df = yf.download(ticker, interval=interval, period=period, progress=False, auto_adjust=False)
    if df.empty:
        return pd.Series(dtype=float)
    close = df["Close"].squeeze()
    close.index = close.index.tz_convert("UTC") if close.index.tz else close.index.tz_localize("UTC")
    return close.rename(ticker)


def import_codex_bundle(path: str | Path, store: "Store", data_dir: Path, gold_name: str = "XAUUSD") -> dict:
    """Import the Codex build's market_data.zip (tick arrays + events) and markets.csv (1-min context closes)."""
    import io
    import json
    import zipfile
    path = Path(path)
    out = {}
    if path.suffix == ".csv":  # markets.csv: known_ms,series,value (value known at end of the minute)
        mk = pd.read_csv(path)
        names = {"dollar_index": "USDX", "sp500": "SP500", "nasdaq100": "NAS100", "vix": "VIX", "silver": "XAGUSD",
                 "eurusd": "EURUSD", "usdjpy": "USDJPY", "wti": "USOUSD"}
        for series, g in mk.groupby("series"):
            t = pd.to_datetime(g["known_ms"], unit="ms", utc=True) - pd.Timedelta(minutes=1)
            v = g["value"].to_numpy(float)
            bars = pd.DataFrame({"open": v, "high": v, "low": v, "close": v, "tick_volume": 1.0, "spread": np.nan},
                                index=pd.DatetimeIndex(t, name="time"))
            store.save_bars(names.get(series, series.upper()), "M1", bars[~bars.index.duplicated()])
            out[series] = len(bars)
        return out
    z = zipfile.ZipFile(path)
    if "market_data.zip" in z.namelist():  # the download wraps the bundle once more
        z = zipfile.ZipFile(io.BytesIO(z.read("market_data.zip")))
    src = json.loads(z.read("source.json"))
    load = lambda n: np.load(io.BytesIO(z.read(n)))
    t = pd.to_datetime(load("ms.npy"), unit="ms", utc=True)
    tk = pd.DataFrame({"bid": load("bid.npy"), "ask": load("ask.npy")}, index=pd.DatetimeIndex(t, name="time"))
    g = tk.resample("1min", label="left", closed="left")
    m1 = pd.DataFrame({"open": g["bid"].first(), "high": g["bid"].max(), "low": g["bid"].min(),
                       "close": g["bid"].last(), "tick_volume": g["bid"].count().astype(float),
                       "spread": (tk["ask"] - tk["bid"]).resample("1min").mean()}).dropna(subset=["close"])
    store.save_bars(src.get("symbol", gold_name), "M1", m1)
    if gold_name != src.get("symbol"):
        store.save_bars(gold_name, "M1", m1)
    out["gold_bars"] = len(m1)
    if "events.csv" in z.namelist():
        ev = pd.read_csv(io.BytesIO(z.read("events.csv")))
        ev = ev[ev.get("active", 1) == 1]
        cal = pd.DataFrame({"time_utc": pd.to_datetime(ev["event_ms"], unit="ms", utc=True), "title": ev["name"]})
        p = data_dir / "events_manual.csv"
        if p.exists():
            old = pd.read_csv(p)
            old["time_utc"] = pd.to_datetime(old["time_utc"], utc=True)
            cal = pd.concat([old, cal])
        cal.drop_duplicates().sort_values("time_utc").to_csv(p, index=False)
        out["events"] = len(cal)
    return out
