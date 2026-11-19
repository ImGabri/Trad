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

## v2.9.1 — 🔄 Reset button

**🔄 Reset daily limits & stops** is in Setup (Telegram: `/reset`).
- **What it clears:**
  - It starts a fresh trading day from your current equity, so the daily loss limit and the trade count go back
    to zero.
  - It clears the kill switch, the survival stop and the drift pause.
- **When it applies:** within a second if the bot is running. Open trades are not touched.

## v2.9 — buys AND sells

- **☠️ Extreme and 💀 Hyper now trade with the candles.**
  - **What changed:** they used to bet against the last candles, so on a falling chart they kept buying.
  - **Now:** falling candles mean SELL and rising candles mean BUY.
  - **Backtest:** still no profit. Each 0.01-lot trade loses about $0.42, and €100 ends at about €12 (Extreme) or is
    gone within a day (Hyper).
- **🎯⇅ Perfect + Sell** is a new style. It runs the same hourly breakout as Perfect, but it also **sells** the first
  break below the 24-hour low while the hourly trend is down.

  | Start (1 % risk) | 2019 – 2023 | 2024 – mid-2025 | Aug 2025 – Nov 2026 |
  |---|---|---|---|
  | €100 | **blown** (−94 %) | +565 % | +915 % |
  | €500 | −31 % | +179 % | +191 % |
  | €1,000 | −29 % | +162 % | +62 % |

  - **How it compares:** it beat buy-only Perfect in 2025–26, but it lost money in the choppier 2019–23 years.
    🎯 Perfect (buy-only) stays the default.

## v2.7 — 🎯 Perfect, ☠️ Extreme and 30 more markets

- **🎯 Perfect** (the default style) is the best strategy found in all backtests, Turbo with its tested settings.
  Press **▶ Start** and it does everything else. Choosing any style button clears old manual tweaks so the preset
  is applied exactly as tested.
  - Backtest (€100 start, 1 % risk, real Standard costs): **+125 %** in 2019–23, **+617 %** in 2024–mid-25 and
    **+529 %** from Aug 2025 to Nov 2026. The worst drawdown was −84 %.
- **☠️ Extreme** makes a new trade every few minutes:
  - After 2 one-minute candles of the same colour, it trades the other way (about 110 trades a day, 24 h).
  - It holds one position: target 0.25 × ATR, stop 1 × ATR, closed after 15 minutes at the latest.
  - It was the busiest of 432 tested "crazy" variants (a new trade every 1–5 minutes). None of the 432 made money in
    all three periods.
  - **Backtest:** 55–72 % of trades win, but each one loses about $0.38 on average, which is the spread. A €100
    account fell to about €12 in every period from 2020 to 2026. It is a demo experiment, and the dashboard asks you
    to confirm.
- **💀 Hyper** is the fastest setting there is:
  - It opens a new standalone trade every minute, betting against the last 1-minute candle.
  - Up to 10 trades are open at once, about 1,200 trades a day around the clock.
  - Each trade has its own 0.25 × ATR target, 1 × ATR stop and a 15-minute limit.
  - **Backtest (64 tested variants, 2020–2026):** none made money. 49–70 % of trades win, but each one loses about
    $0.40 on average, the spread plus slippage. That is roughly −$500 a day at 0.01 lot, so a €100 account was
    **gone within the first trading day** in every period. It is a demo experiment, and the dashboard asks you to
    confirm.
- **🧺 Basket** now has unlimited trades a day, and it buys and sells.
- **More data for the AI:**
  - **Currencies:** GBP, CHF, CAD, NZD, EUR/JPY, AUD/JPY, GBP/JPY and EUR/CHF.
  - **Gold priced in other currencies:** EUR, JPY and AUD.
  - **Metals and energy:** platinum, palladium, copper, Brent crude and natural gas.
  - **Stock indices:** Dow, Russell 2000, DAX, Euro Stoxx 50, FTSE, Nikkei, Hang Seng and China A50.
  - **Crypto:** Bitcoin and Ether.
  - **Bonds and volatility:** US 10-year and 2-year notes, the Bund and the VIX.
  - **Which markets count:** each market is only used if your PU Prime server offers it. The AI starts using
    them at its next retrain (every weekend, or **Retrain AI now**).

## v2.6 — Turbo is now the default (best of 768 variants)

**Turbo:**
- **Entry:** buys the first break above the highest high of the last 24 hourly bars, only while the hourly EMA20 is
  above the EMA50.
- **Exits:** the stop is 3 × ATR, the target is 40 × ATR, and it holds up to 15 hours with no trailing.
- **Sizing:** risks 1 % per trade.

Backtest with real Standard costs, the bot's own default config, and three separate periods:

| Start | 2019 – 2023 | 2024 – mid-2025 | Aug 2025 – Nov 2026 | Worst drawdown |
|---|---|---|---|---|
| €50 | blown | +1,233 % | blown | — |
| €100 | +125 % | +617 % | +529 % | −84 % |
| €250 | +45 % | +261 % | +212 % | −57 % |
| €500 | +72 % | +178 % | +106 % | −35 % |
| €1,000 | +40 % | +153 % | +22 % | −46 % |
| €5,000 | +54 % | +193 % | +54 % | −47 % |

- **Account size:** every period is profitable from €100 up. €50 is too small, because one 0.01-lot trade risks
  25–50 % of the account. The dashboard warns below €100.
- **Daily average:** about 0.15–0.25 % per day. That is far from 1 % a day, and it comes with real drawdowns.
- **Market direction:** Turbo is long-only, and the edge comes from gold's trends.

**Free headline analyst (no API key).** The Claude API analyst is removed. Every 15 minutes the bot reads Google
News (gold, Fed and dollar searches, which also carry Reuters, CNBC, Kitco and Bloomberg), Investing.com (three
feeds), MarketWatch, BBC Business, the Financial Times, FXEmpire, Seeking Alpha, and the Fed and ECB press
releases. It removes stories repeated across outlets and scores the gold impact. It only vetoes a trade on a strong
opposite reading.

## 🚀 Turbo — the fastest €50 growth found

Research on 7.9 years of data tested about 4,000 variants of 1-minute and 15-minute scalping, your dip-buying
basket, session breakouts, news momentum and trend-following. Each method then ran a "€50 race": 150 random
30-day windows (2024–26), compounding from €50 with real Standard costs.

| Method | Doubled €50 within 30 days | Blown | Average after 30 days |
|---|---|---|---|
| **🚀 Turbo (hourly trend breakout, wide stops)** | **37–40 %** | **≤ 2 %** | **+36 to +52 %** |
| Dip-buying basket (your manual style) | 0 % | 100 % | −83 % |
| 15-min scalping | 0 % | 6 % | −21 % |
| Default scalping rules | 0 % | 2 % | −27 % |

How Turbo trades:
- **Entry:** buys when gold breaks above the highest price of the last 24 hours, only while the hourly trend is up.
- **Position:** one trade at a time.
- **Exits:** a wide stop that trails behind a winning trade, with a hold of up to 10 hours.

Caveats:
- **It depends on the bull market:** it is long-only, and its edge came from gold's 2024–25 uptrend. In a falling
  market it mostly sits out or loses small.
- **One full stop-loss can cost up to about half of a €50 account.**
- **Switching it on:** press 🚀 Turbo in the dashboard. Active or Careful switch back to scalping.

## Backtest by account size (Jan 2024 → May 2026, real Standard costs, lots grow with the account)

| Start | 🚀 Turbo → end | Turbo max drawdown | ⚡ Active scalping | 🧺 Basket |
|---|---|---|---|---|
| €50 | €931 (+1,761 %) | −66 % | blown | blown |
| €100 | €981 (+881 %) | −35 % | blown | blown |
| €500 | €1,381 (+176 %) | −15 % | blown | blown |
| €1,000 | €1,881 (+88 %) | −12 % | −90 % | blown |
| €5,000 | €6,412 (+28 %) | −4 % | −86 % | blown |
| €10,000 | €12,929 (+29 %) | −4 % | −86 % | blown |

- **Small accounts swing harder:** they trade at least 0.01 lot, so each trade is a large share of the account.
- **Larger accounts use 0.5 % risk per trade** (adjustable in Settings).
- **Turbo depends on the market direction:** it only buys, and this period was gold's bull market.

## 🧺 Basket (opt-in, demo experiment)

- **How it trades:**
  - Buys after 5 rising 5-minute candles and sells after 5 falling ones.
  - Adds a second position 0.75 × ATR further against the trade.
  - The whole basket closes together at the average entry ± 1 × ATR, or at the stop 6 × ATR from the first entry.
  - It is closed after 4 hours at the latest.
- **Backtest result:** none of 1,296 tested variants made money. This is the least-losing one: it wins about 80 %
  of baskets but loses about $0.36 per basket on average, and it lost at every account size above.
- **Settings:** the `basket:` section in config.yaml.

## Market analyst — "what happens next?"

* **15-minute chart model:** it reads the 15-minute chart (trend, momentum, volatility, where price sits in the
  day's range), the hourly trend, silver, the dollar, Nasdaq and the news calendar, and predicts the next 60 minutes.
  It is trained walk-forward and retrains every weekend. It vetoes trades it strongly disagrees with.
  Honest out-of-sample result on 2020–2026: AUC 0.521. When it is at least 55 % sure, it calls the next hour right
  55.9 % of the time.
* **Headline analyst (free, no key):** every 15 minutes it reads the latest headlines from 11 free sources (see v2.6
  above). It scores their impact on gold and returns a bias, a confidence, and trade conditions (normal / caution /
  avoid). "Avoid" pauses new entries around high-impact releases, and a strong opposite reading vetoes a trade. Each
  call is graded 60 minutes later.

## Proving real demo trades

* **Place test trade** (dashboard → Setup) opens a 0.01-lot BUY on your demo with a broker-side SL/TP, checks it,
  and closes it after 5 seconds. It reports fill price, speed and P&L.
* **⚡ Active** style (default) trades every rule setup the AI agrees with (a few trades a day). **🛡 Careful** only
  takes high-confidence setups. If MT5 rejects a filling mode, orders retry with another one automatically.

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
