# Trad

XAUUSD Trader for PU Prime MetaTrader 5. Trad sends market buy and sell requests on a demo account. It scores fresh bid/ask changes inside the current minute. A local machine-learning model learns from historical quotes, then separate buy and sell adjustments learn from completed demo trades.

**[Download Trad for Windows x64](https://github.com/ImGabri/Trad/releases/latest/download/Trad-Windows-x64.zip)** · [Releases and checksums](https://github.com/ImGabri/Trad/releases)

## First run on Windows

1. Open the PU Prime MetaTrader 5 desktop terminal on Windows x64. Sign in to your **demo** using your assigned server. Credentials stay in MT5. [PU Prime login instructions](https://helpcenter.puprime.com/hc/en-001/articles/18340367150095-How-do-I-find-or-add-my-trading-server-in-MetaTrader-4-MT4-MetaTrader-5-MT5)
2. Extract **Trad-Windows-x64.zip** into a permanent writable folder and double-click **START.cmd**. The release includes Python and the trading libraries. If running from a source checkout, install 64-bit Python 3.12 or 3.13 and the launcher will set up its own environment.
3. Choose **1 — Check demo connection and symbol**. Confirm the server, symbol, bid/ask, lot step and quote age. If it lists several gold symbols, enter the exact tradable Market Watch name in the `symbol` field of `settings.json`, e.g. `XAUUSD.s`. PU Prime uses different suffixes across account types. [PU Prime symbol guide](https://helpcenter.puprime.com/hc/en-001/articles/17043145073935-Why-am-I-unable-to-trade-certain-products-or-symbols)
4. Choose **2 — Download market data, train AI and backtest**. The default is 14 calendar days. Keep MT5 open while it synchronizes its history. Read the adaptive AI result alongside frozen AI and a five-minute momentum rule. Open `data/research/backtest.json` for the individual time periods, costs and coverage.
5. Choose **3 — Watch signals** to log decisions. Use **4 — Start automatic demo orders** when you want the bot to place demo trades. Option 4 requires *Algo Trading* on and the *Disable automated trading via external Python API* setting cleared in this demo terminal. The bot checks the broker, account mode, account identity and symbol before sending. [MetaQuotes Python trading setting](https://www.metatrader5.com/en/releasenotes/terminal/2125)
6. Choose **5 — Export closed-trade reviews** after trades close. Press **Ctrl+C** to stop the runner. Any open position stays visible in MT5 with its broker-side stop and target. The three-minute time exit needs the runner and terminal online.

**To have the backtest checked here**, choose **7 — Export market data only** and attach `data/market_data.zip` in chat. Option 2 also creates this file. It contains quotes, available market context, calendar records and contract information. Account credentials, login numbers, balances and private trade history stay on your computer.

## Run in the background

After training, open **8 — Manage Windows background runner**. Select **1** for watch mode or **2** for automatic demo orders. Windows starts the worker immediately and on future sign-ins. You can close the launcher, close ChatGPT, and lock the desktop. For continuous use while your own PC is off, run the same release and MT5 in a signed-in session on an always-on Windows VPS.

Return to **8 → 3** to see the latest heartbeat and connection state. **8 → 4** stops the worker and removes automatic startup. An in-flight request may finish. Existing demo positions retain their broker-side stop and target. Client-managed time exits resume when the runner starts again.

The task appears in Windows Task Scheduler as **Trad XAUUSD Demo** followed by your Windows user SID. It runs under your own account. It reconnects to the original terminal, demo account and symbol after an interruption and resumes the existing order journal. It reports an account mismatch if someone switches MT5 accounts. Closing your Windows session, sleeping the machine or shutting it down suspends trading. [Microsoft Task Scheduler logon triggers](https://learn.microsoft.com/en-us/windows/win32/taskschd/logon-trigger-example--xml-)

Trade decisions and closed trades stay under `data` in the extracted folder. Worker status and rotating diagnostic logs are in `%LOCALAPPDATA%\TradScalper`. To update versions, stop the background runner, keep your `data` directory and custom `settings.json`, replace the application files, then re-enable the mode you want.

## What happens on a quote

The runner polls the terminal with a default 50 ms pause and recovers recent quote changes. The first qualifying upward or downward price change in each two-second interval can trigger a decision immediately. An opposite impulse can qualify in the same interval. If multiple triggers arrive between polls, the runner evaluates the newest. It uses the last completed minute for context. A signal can request a short as readily as a buy.

The local model scores buy, sell and wait. It can skip an entry for price movement during the order check, a wide spread, a stale quote, a scheduled release, existing exposure, or insufficient room for the broker's minimum lot or stops. One position can be open on the symbol at a time. Stops and targets go into the market request. The entry journal records the request before submission, so restarting after an uncertain response preserves the original intent.

The 50 ms setting controls polling. Actual time to fill also includes terminal processing, the network and the broker. `data/activity.jsonl` contains the order-call round trip and the elapsed time from quote timestamp to response. Trade reviews include the broker's trigger-to-fill time. These times use UTC and depend on the workstation's clock.

## What the bot learns after a trade

The bot matches entry and exit deals by position and waits for the entire position to close. It sums broker-reported profit, commission, fees and swap associated with those deals. It reconstructs the entry spread, slippage, holding time, exit type and strongest base-model inputs. When quote history is available for a position held up to one day, it also measures the best and worst executable price moves during the trade.

For example, a trade can hit its price target and still lose money after fees. In that case its review records the loss after costs and lowers the score for similar setups on that side. A profitable short increases the short-side score for similar conditions. Each update uses a bounded logistic adjustment. Reviews record the score before and after applying that outcome. One observation provides feedback, and repeated outcomes establish whether a pattern holds up.

The learning state persists in `data/demo.sqlite`. A restart or partial close cannot count the same completed position twice. A position mixed with another strategy or manually enlarged receives a review and is excluded from the update. Unattributed account-level charges can only be reconciled on your broker statement.

## Data feeds

| Source | Inputs used | Availability |
|---|---|---|
| PU Prime gold ticks | Bid/ask, spread, quote arrival speed, directional quote changes, recent range, volatility, RSI, momentum, session time and 1–60 minute trends | MT5 demo history and current quotes |
| Other PU Prime symbols | Silver, EUR/USD, USD/JPY, dollar index, S&P 500, Nasdaq 100, WTI and VIX returns | Uses matching instruments on your demo server. Option 1 lists the symbols it found |
| ALFRED / FRED | Two- and ten-year Treasury yields, ten-year real yield, ten-year breakeven inflation, broad US dollar index and VIX | Optional local environment variable `FRED_API_KEY`. Uses first-release vintages with a conservative availability delay |
| US Bureau of Labor Statistics | Scheduled CPI, payrolls, producer prices, job openings, employment costs and import/export prices | Automatic public calendar refresh. Each schedule version is timestamped when first observed |
| Trading Economics | Scheduled, high-importance US releases with a confirmed time, including Fed events when the provider lists them | Optional `TE_API_KEY` with calendar access |
| Your archived event schedule | Additional or rescheduled news windows | Optional `data/events_manual.csv` with columns `event_id,known_utc,event_utc,name,active`. Use ISO timestamps with offsets and 1 or 0 for active |

The model joins reference-market closes only after their minutes have ended. Missing or stale market inputs are reported in the decision log and use training-sample medians. News windows come from schedules already known at the decision time. A new calendar download cannot reconstruct what traders knew before the first snapshot. A provider outage can leave a gap in news coverage. Provider status is printed during preparation and logged during a running session. Gold quote imbalance measures changes to this broker's quotes.

Source documentation is available from [MetaQuotes](https://www.mql5.com/en/docs/python_metatrader5/mt5copyticksrange_py), [FRED](https://fred.stlouisfed.org/docs/api/fred/series_observations.html), [BLS](https://www.bls.gov/schedule/) and [Trading Economics](https://docs.tradingeconomics.com/economic_calendar/country/).

## Reading the test result

Three successive holdout periods each train only on earlier triggers whose simulated outcomes were already known. The replay enters a buy at the ask and exits at the bid. It reverses those sides for a short. It applies the recorded spread, two slippage charges, a round-trip commission assumption and an entry latency assumption. Price gaps through a stop use the next quoted price. The adaptive replay updates on resolved simulated trades in time order. A zero-trade cash baseline is also reported.

The default cost assumptions are **$0.08 per ounce round trip in commission**, **$0.04 per ounce per side in slippage**, and **250 ms entry latency**. Replace them with what your account actually shows, then repeat option 2 or rerun the offline backtest. The report uses USD for a fixed one-ounce position and closed-trade drawdown. It omits historical margin, financing, broker rejections, lot rounding and account-equity limits. A quote replay can differ from a broker fill. Shadow and demo logs are the place to measure that difference.

| Default demo control | Value |
|---|---|
| Intended risk at the initial stop | 0.1% of current demo equity, capped at 0.10 lots |
| Daily equity stop | 1% below the first equity the runner observed that UTC day. New entries pause until the next UTC day |
| Maximum entries | 60 per UTC day, with a two-second wait after a close |
| Holding time | Up to 180 seconds before the runner requests a time exit. Stops, targets, gaps and disconnections affect the actual duration |
| News window | Ten minutes before through five minutes after a known event. The runner also accounts for the planned holding time |

Change these defaults in `settings.json`. Changes to strategy or cost settings require retraining. The risk amount is an estimate at the stop price.

## Command line

For the Windows release use `runtime\python.exe` from the extracted folder. A source checkout uses `.venv\Scripts\python.exe` after the launcher sets it up. Offline research also runs in a regular Python environment on Linux or macOS.

```text
python -m pip install -r requirements.txt
python goldbot.py doctor
python goldbot.py prepare --days 14
python goldbot.py watch
python goldbot.py demo
python goldbot.py background --mode watch
python goldbot.py background --mode demo
python goldbot.py background --mode status
python goldbot.py background --mode stop
python goldbot.py reviews
python goldbot.py backtest --bundle data/market_data.zip
python goldbot.py smoke
python -m unittest discover -s tests -v
```

If several MT5 installations are open, set `MT5_PATH` locally to the exact PU Prime `terminal64.exe` path. If demo history is short, open the symbol in Market Watch and request more dates under **View → Symbols → Ticks**, then rerun preparation. If the journal reports an unresolved submission, check that account's Orders, Deals and History in MT5. The runner reconciles a matching broker record before allowing another entry. Keep the same extracted folder between sessions so its learning and order history stay together.

If a terminal interruption occurred before a request reached the broker, stop the runner and use `python goldbot.py reconcile` to list the pending tag. After verifying the request is absent in MT5, `python goldbot.py reconcile --release TAG` checks available broker records and retires the local intent. This operation preserves the trade reviews and learning state.
