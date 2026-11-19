# TradV2 Scalper v2.0.3 — numeric-history hotfix

## Fixed

Preparing seven years of history could stop in `features.obv()` with:

```text
TypeError: unorderable types for comparison
```

Broker M1 bars and public OANDA monthly files can produce object-dtype columns containing a mixture of numeric values and numeric strings. `np.sign()` cannot compare those mixed Python objects.

## Changes

- `bot/history.py`: coerce OHLC and volume values with `pd.to_numeric(..., errors="coerce")` **before** validating candles. Drop rows with missing/invalid OHLC values, inverted high/low ranges or non-positive closes. Normalize the OANDA-to-broker conversion as well.
- `bot/prepare.py`: normalize OHLCV and spread columns after merging older public bars with broker bars.
- `bot/features.py`: normalize the same columns once at feature-builder entry, protecting every indicator; make `obv()` defensive for direct callers.
- `bot/backtest.py`, `bot/learning.py`, `bot/strategy.py`, `bot/features.py`: replace deprecated pandas `dayofweek` accessors.
- `bot/tests/test_numeric_bars.py`: four offline regression tests covering OBV, full feature generation, monthly-history validation and broker-price conversion.
- `bot/VERSION`: `2.0.3`.

## Verification

- All **22 V2 tests** pass, including the four new regressions.
- All **41 root tests** pass (one Windows-only scheduler test skipped).
- The release ZIP is assembled by the existing `bot/packaging/build_windows.py` builder with the embedded Python runtime and Windows dependencies.

## Upgrade

Stop the bot, extract the new ZIP into a fresh folder, then copy your existing `data` folder and any customized `config.yaml` / `.env` files. Start `START.cmd` and run preparation again. Your broker history, cached public history, model archive and trade journal are preserved.

The SHA-256 checksum for the Windows x64 ZIP is attached to this release.