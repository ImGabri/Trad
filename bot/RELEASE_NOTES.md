## TradV2 XAUUSD AI Trader 3.0.1 — smoother updates and retrains

**Download:** `TradV2-XAUUSD-Scalper-Windows-x64.zip` below → extract → double-click **START.cmd**. Demo accounts only.

### Fixed in 3.0.1
- **No more false "The background bot is not answering".** After a retrain or a restart, the first minute downloads the market history (MT5 may first sync it from the broker) and grades the channel signals. The bot now keeps its heartbeat during that minute and during retrains, and the dashboard shows *Loading market history…* or *Training*.
- **Retrains never change a preset strategy.** The rule tuner only adjusts the scalping rules (Active / Careful). Perfect, Hyper Max, Signal AI and the others always run with their own tested settings. Weekly retrains are faster because the rule search is skipped for them. A `tuned.yaml` saved by an earlier retrain under a breakout strategy is ignored.
- **Updating is seamless.** Starting the new version closes the old dashboard (from 3.0.1 on) or opens next to it, and restarts the background bot if it still runs older code. An open dashboard tab reloads itself when the dashboard restarts, so its buttons never go stale ("forbidden").
- **Clearer retrain report.** It now says that the "strategy" row is the style's own entries. The Telegram channel-signal trades are graded live (dashboard → Signals) and are not part of that report. A noisy chart log line is gone.

---

## 3.0.0 — perfecting pass

### What's new
- **Simpler dashboard.** One card holds START/STOP, demo/watch, equity, today's result and the current strategy with its backtest. **Change strategy** opens every strategy ranked in one table (best profit, most trades a day or safest). Setup collapses to one line once it's done. Trades, Signals and Market are now tabs. Refreshing the page is ~40× faster.
- **🔮📡 Signal AI is preselected on a fresh install**, with its tested settings. Every strategy still sets all of its own settings when you pick it.
- **Bug audit: 25 findings, all fixed and covered by tests.** The main ones:
  - Bitcoin, ETH, US30, DAX, oil and FX posts were read as gold signals. This was 309 of 3,487 historical signals.
  - Edited posts could be traded twice.
  - Stale order posts were traded at market.
  - When MT5 briefly listed no positions, the notes on open trades were deleted.
  - After a weekend start, the broker's time offset was never re-checked.
  - Slow Telegram fetches blocked the trading loop.
  - Windows file locks could crash the bot.
  - The dashboard Reset button sometimes did nothing.
  - An empty password box erased the saved MT5 password.
- **Signal trades follow the same safety rules as the strategy's own trades:** Friday flat, closed market, spread filter, news blocks and the daily loss limit. They also have:
  - their own cap of 10 open and 60 per day;
  - no trailing (they keep their tested levels);
  - a refusal of any stop more than 3 % of the price away;
  - on small accounts, the 0.01 lot only if it risks at most 25 %.
- **The signal-rating AI** now judges each signal once, on the market as it was at the post time (the way it was trained), and keeps what it learned across restarts and updates.

### Honest re-test on the cleaned channel history (Dec 2025 → Nov 2026)
| Strategy | €200 | €500 | €1,000 |
|---|---|---|---|
| 🔮📡 **Signal AI** (recommended) | +1,615 % (worst −49 %) | +1,571 % (−35 %) | +1,158 % (−29 %) |
| 🔮📡🔥 **Signal AI ×2** | +5,160 % (−62 %) | +2,731 % (−62 %) | +2,148 % (−61 %) |
| 🎯 Perfect alone | +99 % (−42 %) | +48 % (−30 %) | +31 % (−20 %) |

Both Signal AI presets were profitable in both halves of the window at every account size.

The channel styles without the AI were much weaker on clean data: Signal Max 5 and Signal Hyper Max 5 blew €200. v2.22 had shown Signal AI ×2 at +4,560 % (€500); part of that came from the mis-read non-gold posts.

The channel data covers 2025–26 only, and about 250 combinations were compared, so expect real results to be lower. Use a demo account.
