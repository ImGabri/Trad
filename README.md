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
| 🔮📡 **Signal AI** | 🎯 Perfect breakouts + the Telegram channel signals the signal-rating AI expects to win | €500 Dec 2025 → Nov 2026: +1,598 % (worst drop −35 %) |
| 🔮📡🔥 **Signal AI ×2** | the same in two signal lanes | +4,726 % (worst drop −58 %) |
| 🎯 **Perfect** | hourly trend breakouts, 1 % risk | profitable in 2019–23, 2024–25 and 2025–26 |
| 🎯🔥 **Hyper Max** / ⚡ **Hyper Max 5** | Perfect on 12/24/48 h channels + 4-hour trend + 20-day / previous day / week breakouts | profitable in every period at €200 and €1,000 |

Fast scalping styles (Basket, Extreme, Hyper, Hyper AI) are kept as experiments — they lost money in every backtest because the
$0.32 spread is larger than the edge in 1-minute moves. Past backtests do not guarantee future results; use a demo account.

## Legacy V1

The original tick scalper (V1) is still in the repository root (`START.cmd legacy`); it is no longer developed.
