# Trad v1.1.0 — online learning from public real-time data

## What's new in V1

**Online learning from real-time public data (new `online_learning.py`).** A background poller fetches recent intraday quotes from key-free public finance endpoints (gold futures as the XAUUSD proxy, plus silver, the dollar index, S&P 500, Nasdaq 100, WTI, EUR/USD and VIX as context). It scores impulse setups with the same feature pipeline as the demo runner, tracks each online signal until its stop, target or time exit resolves on later public prices, and updates the side-specific score adjustments from every resolved outcome. Weights persist in `data/online_learning.json` and survive restarts, so the bot keeps learning between demo sessions even when the terminal is quiet.

**Safest possible design.** The public feed is strictly off the order path: demo entries keep using MT5 quotes, online signals never place orders, network failures are logged and skipped, and no account credentials leave the machine. Online signals are skipped inside scheduled news windows, deduplicated per minute, and resolved only on prices observed after the trigger.

**Polish.**
- Dropped the deprecated `dayofweek` accessor (Pandas 2.x warning).
- New test suite `tests/test_online_learning.py` (12 tests, all offline: no broker, no network).
- Bumped `VERSION` to `1.1.0`.

## What changed

| Area | Change |
|---|---|
| `online_learning.py` | New: public feed poller, online signal pipeline, outcome resolution, side-weight updates |
| `engine.py` | Runner ticks the online learner every service cycle; emits `online_signal` / `online_resolve` |
| `strategy.py` | `day_of_week` replaces deprecated `dayofweek` |
| `tests/test_online_learning.py` | New: 12 offline tests |
| `README.md` | Documents public real-time feeds and online learning |
| `VERSION` | `1.1.0` |

## How to use

Run the demo as usual (`python goldbot.py watch` or `demo`, or the Windows launcher options 3/4). The online learner starts automatically with the runner and logs `online_signal` and `online_resolve` entries to `data/activity.jsonl`. Inspect current weights with `python -c "import online_learning as ol; ..."` against `data/online_learning.json`.

## Installation

Stop any running runner, keep your `data` folder and `settings.json`, replace the application files with this release, then restart. No new dependencies.

GitHub provides the complete source archive for this release. The V1 Windows ZIP can be produced on a 64-bit Python 3.13 Windows builder with `python packaging/build_windows.py`; the V2.0.2 Windows bundle and its checksum are published alongside it.