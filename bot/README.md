# TradV2 — XAUUSD AI Scalper for PU Prime MT5 (Windows 11, demo)

A self-learning gold scalper with a one-click dashboard. It trades XAUUSD on your PU Prime MT5 demo, runs in
the background, restarts at every Windows logon and retrains itself every weekend on everything it has collected.

## v2.17 — re-optimised Hyper Max 5, and a faster bot

**Hyper Max 5 exits re-optimised.** 28 exit and risk variants were tested: stop 2–4 × ATR, target 20/40 × ATR,
trailing on/off, 15 h vs 24 h hold, and 0.75 / 1 / 1.5 % risk. The winner was chosen on 2019–25 only and then
checked on the unseen Aug 2025 – Nov 2026 period.

The change: **hold trades up to 24 h instead of 15 h.**

| Hyper Max 5, 1 % risk | 2019 – 23 | 2024 – mid-25 | Aug 2025 – Nov 2026 (unseen) |
|---|---|---|---|
| €200, 15 h hold | +197 % | +1,303 % | +1,317 % |
| €200, **24 h hold** | **+265 %** | +1,304 % | +1,046 % |
| €1,000, 15 h hold | +142 % | +788 % | +325 % |
| €1,000, **24 h hold** | **+356 %** | **+907 %** | **+349 %** |

- **Verified end to end** through the app's own presets, signals and engine. The worst drawdown is still large,
  up to −88 %.
- **Rejected:**
  - trailing stops and 2 × ATR stops: worse in every period
  - 1.5 % risk: highest profit, but −96 % drawdowns
- **Fixed:** a style's own hold time was being overwritten by the default Perfect exits.

**Faster live bot.** It does the same work and makes identical decisions, checked on 2,250 simulated bars.
- **Data downloads:** the bot no longer re-downloads 13,000 one-minute bars, the related markets and 800 hourly
  bars every minute. It keeps them and only fetches the newest bars, with a full refresh on any gap, revised bar or
  reconnect, and every 60 minutes.
- **Processing speed:** each closed minute is about 25 % faster (≈ 190 ms instead of ≈ 260 ms on real gold data),
  and the slowest minutes are about 3 × faster.
- **Symbol list:** the MT5 symbol list is read once per connection.
- **Database:** position data is only written when it changes, instead of twice a second.

## v2.16 — 🎯🔥⚡ Hyper Max 5 (faster Hyper Max)

**What was tested:** 21 faster versions of Hyper Max, each at up to 3, 5 or 8 open trades, at €200 and €1,000,
with the news calendar on.
- extra 6 h, 3 h + 6 h and 1/2/3/6 h breakout channels
- 15-minute-chart channels (4 h / 8 h / 24 h)
- Hyper's every-minute "buy the dip in the trend" entries

**What failed:** every faster *entry* rule blew €200 in 2019–23. It added many more trades, about 2.2–4 a day,
but they were small, noisy breakouts.

**What worked:** letting Hyper Max hold **up to 5 trades** instead of 3.

| 1 % risk (max drawdown) | 2019 – 23 | 2024 – mid-25 | Aug 2025 – Nov 2026 | Trades/day |
|---|---|---|---|---|
| €200 🎯🔥⚡ Hyper Max 5 | +197 % (−70) | **+1,303 %** (−35) | **+1,317 %** (−54) | 1.9 |
| €200 🎯🔥 Hyper Max | +202 % (−60) | +1,114 % (−33) | +745 % (−50) | 1.6 |
| €1,000 🎯🔥⚡ Hyper Max 5 | +142 % (−85) | **+788 %** (−35) | **+325 %** (−41) | |
| €1,000 🎯🔥 Hyper Max | +176 % (−72) | +591 % (−29) | +140 % (−32) | |

**The trade-off:** much more profit in 2024–26, slightly less in the choppy 2019–23 period, and deeper drops
(up to −85 % at €1,000).

## v2.15.1 — startup diagnostics + backtest of the shipped code

- **Startup screen:** the dashboard now shows each startup step with a timer.
- **MT5 hang:** the bot gives up after 90 s with a clear message instead of hanging on "Connecting to MT5…".
- **Crashes:** they are saved, and the dashboard shows the last log lines whenever the background bot isn't
  running.
- **Faster startup:** the bot reads the MT5 symbol list once instead of 39 times.

**End-to-end backtest of the code you download:** dashboard style presets → config → the bot's signal code →
backtest engine, with real Standard costs and the built-in news calendar, 1 % risk. Max drawdown is in brackets.

| Style | Start | 2019 – 23 | 2024 – mid-25 | Aug 2025 – Nov 2026 | Trades/day |
|---|---|---|---|---|---|
| 🎯 Perfect | €200 | +64 % (−62) | +318 % (−26) | +265 % (−34) | 0.6 |
| 🎯 Perfect | €1,000 | +41 % (−46) | +155 % (−12) | +53 % (−18) | |
| 🎯⚡ Hyper Perfect | €200 | +116 % (−65) | +1,138 % (−53) | +602 % (−83) | 1.7 |
| 🎯⚡ Hyper Perfect | €1,000 | **−5 %** (−80) | +556 % (−34) | +125 % (−44) | |
| 🎯🐂 Hyper + H4 | €200 | +175 % (−69) | +933 % (−33) | +736 % (−40) | 1.25 |
| 🎯🐂 Hyper + H4 | €1,000 | +97 % (−67) | +559 % (−28) | +164 % (−35) | |
| 🎯🔥 **Hyper Max** | €200 | **+202 %** (−60) | **+1,114 %** (−33) | **+745 %** (−50) | 1.6 |
| 🎯🔥 **Hyper Max** | €1,000 | **+176 %** (−72) | **+722 %** (−31) | **+167 %** (−38) | |

- **The news calendar matters:** the same code run *without* it lost the whole €200 in 2019–23 with every Hyper
  style. Its no-trade windows before and after NFP / CPI / FOMC are essential, so keep them on.
- **Asia session:** turning off Asia-hours trading made every style worse, so it stays on.

## v2.15 — 🎯🔥 Hyper Max, new dashboard, and 1,000+ more strategy tests

**How the research was done:** four agents worked in parallel.
- One read what gold day traders and quant researchers report online (28 strategies, with sources).
- Three backtested whole strategy families on the same engine (real Standard costs, 2019–2026, €200 and €1,000,
  three periods):
  - **Session / range:** Asian-range and London/NY opening-range breakouts, previous day/week high/low, London-fix
    and time-of-day drift, the "Asian reopen drift", turn-of-month.
  - **Pullback / mean reversion:** RSI(2), Bollinger, EMA, VWAP, Fibonacci, stochastic, buy-the-down-day.
    **Nothing passed.**
  - **Trend indicators:** Supertrend, Keltner, Bollinger squeeze, MACD/EMA crosses, Turtle 20/55, ADX, Ichimoku,
    Heikin-Ashi, NR7, inside bars, momentum, and 200-day / 12-month filters.

**What came out:** two add-ons improved 🎯🐂 Hyper + H4. Together they make **🎯🔥 Hyper Max**, which is
Hyper + H4 plus:
- **Turtle 20-day breakout:** buy the first close above the highest high of the last 20 completed days.
- **Previous-day breakout in the 4-hour direction:** buy above yesterday's high in an H4 uptrend, sell below
  yesterday's low in an H4 downtrend. One per day.
- **Previous-week-high breakout:** buy. One per week.

| 1 % risk (max drawdown) | 2019 – 23 | 2024 – mid-25 | Aug 2025 – Nov 2026 |
|---|---|---|---|
| €200 🎯🔥 **Hyper Max** | **+202 %** (−60 %) | **+1,114 %** (−33 %) | **+745 %** (−50 %) |
| €200 🎯🐂 Hyper + H4 | +175 % (−69 %) | +933 % (−33 %) | +736 % (−40 %) |
| €1,000 🎯🔥 **Hyper Max** | **+176 %** (−72 %) | **+591 %** (−29 %) | **+140 %** (−32 %) |
| €1,000 🎯🐂 Hyper + H4 | +97 % (−67 %) | +524 % (−27 %) | +96 % (−35 %) |

- **Activity:** about 1.6 trades a day, up from 1.2.
- **Did not survive costs:**
  - opening-range breakouts
  - every pullback / mean-reversion rule
  - Supertrend / MACD / ADX / Ichimoku signals
  - time-of-day and day-of-week drifts (real, but smaller than the spread)
  - turn-of-month
- **Live check:** the live bot's Hyper Max signals matched the backtest signals at 300 of 300 checked moments.
- **Honest caveats:**
  - About 30 combinations were tried, so some of the gain is probably luck in selection.
  - With 2 pips of slippage instead of 0.5, 2019–23 turns flat or negative.
  - Multi-day trades pay swap, which is not modelled.

**Dashboard:**
- **Look:** a new design with glass cards, a live pulse and a spinning ring while trading.
- **Style picker:** grouped into *Profitable in backtests / Scalping / Experimental — lost in backtests*.
- **P&L chart:** gradient fill, zero line and hover tooltips.
- **Tables:** BUY/SELL chips.
- **Phones:** no sideways scrolling.

## v2.14 — 🎯🐂 Hyper + H4: bull/bear identifier on the 4-hour chart

**Tested:** 32 combinations.
- **Trend identifiers:** H4 EMA 20/50, H4 EMA 50/200, D1 EMA 20/50, or none.
- **Directions:** buy only, or buy and sell.
- **Chop filter:** H4 ADX > 20, on or off.
- **Open trades:** 1 or 3.

Each combination ran at €200 and €1,000 in three periods, on top of Hyper Perfect's 12/24/48-hour breakouts.

**Winner: H4 EMA 20/50, buy only, up to 3 trades.** It buys breakouts only while the 4-hour chart is bullish, and
it sits out bearish 4-hour markets.

| 1 % risk | 2019 – 23 | 2024 – mid-25 | Aug 2025 – Nov 2026 |
|---|---|---|---|
| €200 🎯🐂 Hyper + H4 | **+167 %** (dd −71 %) | +921 % (dd −34 %) | **+705 %** (dd −77 %) |
| €200 🎯⚡ Hyper Perfect | +116 % (dd −65 %) | +1,138 % (dd −53 %) | +602 % (dd −83 %) |
| €1,000 🎯🐂 Hyper + H4 | **+79 %** (dd −67 %) | +507 % (dd −27 %) | **+96 %** (dd −34 %) |
| €1,000 🎯⚡ Hyper Perfect | −18 % (dd −82 %) | +522 % | +64 % (dd −50 %) |

- **Trade frequency:** about 1.2 trades a day.
- **At both sizes:** it was the only fast variant profitable in every period at €200 *and* €1,000.
- **Selling in bear markets:** still **lost** in 2019–23, even with the H4 filter (€200 was blown). So the
  identifier is used to *stop buying* in bear markets, not to sell.
- **Also tested, not added (144 more backtests):**
  - **Sells:** they lost with every bull/bear identifier (H4, M15 20/50, M15 50/200, H4 + M15). Sell-only averaged
    −0.07 to −0.14 R per trade in 2019–23 and −0.02 to −0.22 R in 2024–25. They only made money from Aug 2025.
  - **A 15-minute identifier:** M15 20/50 was faster (about 1.6 trades a day) and good at €200. At €1,000 it lost
    5 % in 2019–23 (dd −82 %). H4 alone stays the best.
- **Live trend source:** the bot reads the 4-hour trend from about 800 hourly MT5 bars. Checked against the full
  history, it agreed at 300 of 300 random moments.

## v2.13 — 🎯⚡ Hyper Perfect (faster Perfect that still made money)

- **How it trades:** the same breakout as Perfect, but it watches the **12-hour, 24-hour and 48-hour** highs at once
  and can hold **up to 3 trades**. That is about 1.6–1.8 trades a day, roughly 3 × Perfect.
- **Why this one:** it was the best of 6 tested combinations.
- **Backtest (bot engine, real Standard costs, 1 % risk):**

  | Start | 2019 – 23 | 2024 – mid-25 | Aug 2025 – Nov 2026 |
  |---|---|---|---|
  | €200 Hyper Perfect | +116 % (dd −65 %) | **+1,138 %** (dd −53 %) | **+602 %** (dd −83 %) |
  | €200 Perfect | +64 % (dd −62 %) | +318 % (dd −26 %) | +265 % (dd −34 %) |
  | €1,000 Hyper Perfect | −18 % (dd −82 %) | +522 % | +64 % |
  | €1,000 Perfect | +41 % | +153 % | +22 % |

  - **Small accounts:** from €200 it was profitable in every period.
  - **Swings:** it has much bigger swings. It dropped −83 % before recovering in 2025–26.
  - **At €1,000:** it lost in 2019–23, because 3 open trades at full 1 % risk is a lot when the market chops.
  - **Unchanged:** it only buys, like Perfect.
- **Other combinations tested:** adding 6-hour channels, sells or 15-minute channels made it faster but **blew
  €200** in 2019–23.

## v2.12 — 🧠 trade memory: learns from its own wins and losses while trading

**How it learns:** after every closed trade, the bot files the result under its situation: BUY or SELL × session
(Asia / London / London–NY overlap / New York) × volatility (calm / normal / wild).

- **Losing situations:** once a situation has lost on average over 30 or more trades, the bot skips it.
- **Re-testing:** every 10th signal in a skipped situation is still taken, so the bot notices if it starts working.
- **Where it shows:** the dashboard (Market & last signal → Trade memory) shows what it avoids and what works.
- **Where it is kept:** the memory survives restarts and updates (`data\learning\memory.json`).

**Which styles use it:**
- **On for 🧺 Basket, ☠️ Extreme and 💀 Hyper.** Hyper starts with the lessons from the 2020–26 backtest.
- **Off for 🎯 Perfect, because it hurt there.** Breakouts often lose a few times right before a big winner.

Backtest results:

| | 2020–23 | 2024–mid-25 | Aug 2025–Nov 2026 |
|---|---|---|---|
| 💀 Hyper without memory | 438 trades/day, −$162/day | 367/day, −$134/day | 282/day, −$85/day |
| 💀 Hyper **with memory** | **58/day, −$21/day** | **53/day, −$20/day** | **48/day, −$16/day** |
| 🎯 Perfect (long, 1 %) without memory | +55 % | +204 % | +61 % |
| 🎯 Perfect with memory (not used) | +29 % | +167 % | +63 % |

**What the memory learned:** in every one of the 24 situations, Hyper lost about $0.13–$0.62 per 0.01-lot trade.
So the memory trims Hyper down to mostly re-tests, and it loses about 7 × less per day. It still does not turn
Hyper into a winner.

## v2.11 — bigger lots on surer trades (🎯 Perfect)

- **How it sizes trades:** Perfect now risks **1.5 × your Risk %** when the AI agrees with the breakout and
  **0.5 ×** when it does not.
- **Backtest (bot engine, long-only, 1 % base, AI predictions the model never saw in training):**

  | Start | Sizing | 2019 – 23 | 2024 – mid-25 | Aug 2025 – Jul 2026 |
  |---|---|---|---|---|
  | €1,000 | flat 1 % | +41 % (dd −46 %) | +153 % | +23 % |
  | €1,000 | **AI sizing** | **+50 % (dd −28 %)** | **+211 %** | **+72 %** |
  | €200 | flat 1 % | +64 % | +318 % | +292 % |
  | €200 | **AI sizing** | +58 % | **+381 %** | +292 % |

  - **Small accounts:** under about €500 most trades are already the minimum 0.01 lot, so sizing changes less.
- **Fast trades — tested, not used:** I tested the same idea on 💀 Hyper and every-minute AI trades. Even the AI's
  most confident minutes lost about $0.35 per trade, so bigger lots there only lose faster.

## v2.10 — directional 💀 Hyper

- **What changed:** Hyper now only trades **with the hourly trend**. In an uptrend it buys when a 1-minute candle
  dips, and in a downtrend it sells when one rises. Up to 3 trades are open at once (about 360 a day). Each trade
  has a 2 × ATR target, a 0.5 × ATR stop and a 60-minute limit.
- **Why:** on 20 Nov the old Hyper bought while the hourly trend and the headlines pointed down. The new version
  would only have sold that night.
- **Backtest (576 directional variants, 2020–2026):** none made money. The fast ones lose about $0.30–$0.37 per
  0.01-lot trade (the old Hyper lost $0.41–$0.45). That is roughly −$120 a day, so €200 was still blown in every
  period. The spread ($0.32 a trade) is bigger than any edge found in 1-minute candles.

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
