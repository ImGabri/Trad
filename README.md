# TradV2 — XAUUSD AI trading bot for PU Prime MT5 (Windows, demo)

**[⬇ Download the latest Windows version](https://github.com/ImGabri/Trad/releases/latest/download/TradV2-XAUUSD-Scalper-Windows-x64.zip)** · [All releases and checksums](https://github.com/ImGabri/Trad/releases)

1. Install **PU Prime MT5**, log in to your **demo** account with *Save password* ticked, and switch **Algo Trading** on.
2. Extract the zip to its own folder (e.g. `C:\TradV2`) and double-click **START.cmd**. Python and every library are included.
3. In the dashboard that opens, pick a strategy (the recommended one is preselected with its tested settings) and press **START**.

The bot runs in the background, restarts after every Windows logon, refuses real-money accounts and learns from every trade.
Full guide, every strategy with its backtest, and honest caveats: **[bot/README.md](bot/README.md)**.

## Strategies at a glance (backtests with real PU Prime Standard costs)

| Strategy | What it does | Backtest |
|---|---|---|
| 🔮📡 **Signal AI** (recommended) | 🎯 Perfect breakouts + the Telegram channel signals the signal-rating AI expects to win | Dec 2025 → Nov 2026: €200 +1,615 % · €500 +1,571 % · €1,000 +1,158 % (worst drop −29 % to −49 %), profitable in both halves |
| 🔮📡🔥 **Signal AI ×2** | the same plus a second signal lane (any distance, 24 h) | Dec 2025 → Nov 2026: €200 +5,160 % · €500 +2,731 % · €1,000 +2,148 % (worst drop about −62 %) |
| 🎯 **Perfect** | hourly trend breakouts, 1 % risk | €200: +64 % (2019–23) · +318 % (2024–25) · +265 % (2025–26) |
| 🎯🔥 **Hyper Max** / ⚡ **Hyper Max 5** | Perfect on 12/24/48 h channels + 4-hour trend + 20-day / previous day / week breakouts | profitable in all three long periods, but drops of −60 % to −88 % (Hyper Max 5 lost 77 % of €200 from Dec 2025) |

Channel signals without the rating AI (Signal Max 5, Signal Hyper …) and the fast scalping styles (Basket, Extreme, Hyper,
Hyper AI) are kept as experiments: they blew small accounts or lost money in backtests. Past backtests do not guarantee
future results; use a demo account.

## Legacy V1

The original tick scalper (V1) is still in the repository root (`START.cmd legacy`); it is no longer developed.
