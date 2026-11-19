# TradV2 — XAUUSD AI Scalper for PUPrime MT5 (Windows, demo)

A complete, ready-to-run bot: multi-timeframe scalping rules (M15 bias → M5 key level → M1 trigger),
a LightGBM AI confidence filter, hard risk rules, news-aware trading, Telegram alerts and a Windows
background runner. Design: `ARCHITECTURE.md`. All settings: `config.yaml`.

## Quick start (Windows PC, no VPS needed)

1. Install the **PU Prime MT5** terminal, log in to your **demo** and tick *Save password*. Click **Algo Trading**
   so it turns green. Leave MT5 open.
2. Download **TradV2-XAUUSD-Scalper-Windows-x64.zip**, extract it to a permanent folder (e.g. `C:\TradV2`) and
   double-click **START.cmd**. Python and every library are bundled — nothing to install.
3. Menu **1** — checks the connection and shows the gold symbol (`XAUUSD.s` on Standard accounts), spread, margin and history.
4. *(optional)* Menu **13** — import the Codex build's `market_data.zip` and `markets.csv`. This adds its ticks, context markets and BLS calendar.
5. Menu **2** — downloads up to 3 years of M1 history from MT5, trains the AI walk-forward and backtests it against
   buy-and-hold, an EMA cross and random entries. Opens `data\report\report.html`.
6. Menu **3** (watch, no orders) or **4** (demo orders) runs in the window. Menu **5** runs demo trading in the background.
   It restarts at every Windows logon, so you can close the window. Menu **7** shows status and **8** stops it.
7. Menu **11** — Telegram alerts. Paste your @BotFather token and message the bot once. Commands:
   `/status /pause /resume /stop`.

**Kill switch:** use menu **9**, Telegram `/stop`, or create an empty file `data\STOP`. Each one closes all bot positions and
halts the bot. Menu **10** clears it.

The PC must stay on, awake and logged in with MT5 open. Set Windows *Power → Sleep: Never*. Orders always carry a
broker-side SL and TP, so open trades stay protected if the PC goes down.

## Safety rails (hard-coded in `risk.py`, tunable in `config.yaml`)

0.5 % risk per trade · 2 % daily loss halt · max 2 positions / 20 trades a day · survival hard stop when equity ≤ 1.5 ×
(margin + SL loss of 0.01 lot) · spread filter (> 2× 20-bar average) · 3-pip slippage cap · no averaging down · flat by
Friday 21:00 UTC · no entries Fri 21–22 and Sun 22–23 UTC · demo accounts only.

## Account type matters

`XAUUSD.s` (Standard) has no commission but a fixed **$0.32** spread. An **ECN** demo (5–15¢ spread + $1/side/lot) costs about
half as much per trade, which matters a lot for scalping. If you open one, set `account_type: ECN` in `config.yaml` and the bot
finds the right symbol on its own.

## Command line

```bash
runtime\python.exe main.py doctor
runtime\python.exe main.py prepare --years 3
runtime\python.exe main.py import-codex C:\path\market_data.zip C:\path\markets.csv
runtime\python.exe main.py demo
runtime\python.exe main.py background start-demo
runtime\python.exe main.py kill
runtime\python.exe main.py smoke
```

Logs are JSON lines in `data\logs` (7-day rotation). Trades, predictions and the tick spread log are in `data\xau.db` (SQLite).
