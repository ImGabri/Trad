# TradV2 — XAUUSD AI Scalper for PU Prime MT5 (Windows 11, demo)

A self-learning gold scalper with a one-click dashboard. It trades XAUUSD on your PU Prime MT5 demo, runs in
the background, restarts at every Windows logon and retrains itself every weekend on everything it has collected.

## Start (3 steps)

1. Install **PU Prime MT5**, log in to your **demo** account with **Save password** ticked, and click
   **Algo Trading** so it turns green.
2. Download **TradV2-XAUUSD-Scalper-Windows-x64.zip**, right-click → **Extract All…** to e.g. `C:\TradV2`, and
   double-click **START.cmd**. Python and all libraries are included. Extracting takes seconds (about 40 files). The first START unpacks the bundled Python in about 20 seconds.
3. In the dashboard that opens, press **▶ Start trading**.
   The first start downloads ~7 years of gold history, trains the AI and tunes the rules (10–30 min). Then the bot
   starts trading on its own.

The setup also creates an **XAU Scalper** shortcut on your desktop and in the Start menu. You can close the browser
tab: the bot keeps running, stops Windows from going to sleep while it trades, and starts again after every logon
(launching MT5 itself if needed). **■ Stop** turns all of that off.
**Close all & halt** is the kill switch. Telegram `/stop` or an empty `data\STOP` file do the same.

## How it trades

| | |
|---|---|
| Entries | M15 trend (EMA 50/200 + ADX) → price at a key level (VWAP, prior H1 high/low, Fibonacci 0.618/0.786) → M1 momentum trigger. LightGBM AI confidence gate. Optional AI-only fast entries. |
| 1 % exit | Closes a trade at **+1 % of equity**. If the AI still expects the move to continue, it locks half of that gain with the stop instead and lets it run, leaving as soon as the AI stops agreeing. Checked twice a second. |
| Other exits | ATR take-profit, ATR stop-loss (always at the broker), trailing stop, time stop, Friday 21:00 UTC flat. |
| News | Verified 2019–2026 US calendar (NFP, CPI, PPI, FOMC, retail sales, GDP, PCE, JOLTS, ISM) plus a live ForexFactory feed. No entries 15→5 min before an event (stops tightened). One AI "news scalp" in the ±5 min window. Wider stops for 15 min after. |
| Risk | 0.5 % per trade. Small-account mode trades 0.01 lot when that risks ≤ 10 % (a €50 start). 15 % daily loss halt, max 2 open / 20 trades a day, spread and slippage filters, survival hard stop, no averaging down, demo accounts only. |

## How it learns from itself

* **Keeps all data:** your broker's bars, every tick spread, every AI prediction and every trade with its entry
  conditions go into `data\xau.db`. Older years come from a public OANDA M1 dataset, cached in `data\hf_cache`.
  Nothing is thrown away, and every retrain uses more data than the last one.
* **Measures its real costs:** it learns the spread by hour of the week and its actual slippage from your own
  fills, and backtests use those numbers.
* **Learns which of its own trades win:** a self-learning gate is first trained on thousands of simulated
  walk-forward trades, then updated after every real closed trade (real trades count 5×). It is only switched on
  if it improves results on trades it has never seen. It then vetoes entries it expects to lose.
* **Retrains weekly:** while gold is closed at the weekend it downloads the new data, retrains the AI and re-tunes
  the rules. New rules are adopted only if they are profitable on both the tuning period and a separate validation
  period. Each old model is archived in `data\models\`, and every retrain is logged in `data\learning_log.jsonl`.
* **Drift guard:** it pauses entries if the live win rate falls 15 points below the backtest (`/resume` to override).

## Honest expectations

On a Standard account every trade pays a fixed **$0.32** spread. With €50 the smallest trade (0.01 lot = 1 oz)
already risks ~7 % at a normal stop. The research on 7.9 years of data (see the report) shows how hard it is to beat
those costs. The bot adopts tuned rules only when they pass the validation test, and the dashboard shows whether
they did. Treat the demo as the real test. The bot refuses real-money accounts.

## Files

`START.cmd` opens the dashboard · `MENU.cmd` opens the advanced text menu · `config.yaml` holds every setting (the
dashboard's settings override it via `data\user.yaml`) · `data\report\report.html` is the latest backtest report ·
`data\logs\` keeps JSON logs for 7 days.

Command line (from the extracted folder):

```bash
runtime\python.exe main.py doctor
runtime\python.exe main.py prepare --years 7
runtime\python.exe main.py background start-demo
runtime\python.exe main.py kill
runtime\python.exe main.py smoke
```
