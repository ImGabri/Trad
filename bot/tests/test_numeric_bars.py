"""Regression tests for numeric normalization of broker/public OHLCV history."""
import sys
from datetime import date
from pathlib import Path
from unittest.mock import patch

import numpy as np
import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

import history  # noqa: E402
from features import build_features, obv  # noqa: E402
from synthetic import synthetic_m1  # noqa: E402


def test_obv_accepts_object_dtype_numeric_strings():
    idx = pd.date_range("2025-01-01", periods=4, freq="1min", tz="UTC")
    bars = pd.DataFrame({
        "close": pd.Series([2900.0, "2900.2", 2900.1, "2900.4"], index=idx, dtype=object),
        "tick_volume": pd.Series([10.0, "11.0", 12.0, "13.0"], index=idx, dtype=object),
    }, index=idx)

    result = obv(bars)

    assert result.tolist() == pytest.approx([0.0, 11.0, -1.0, 12.0])
    assert np.issubdtype(result.dtype, np.floating)


def test_build_features_normalizes_mixed_broker_public_columns():
    bars = synthetic_m1(days=1)
    for col in ("open", "high", "low", "close", "tick_volume", "spread"):
        bars[col] = bars[col].map(str)  # object-dtype values, as in mixed imports

    features = build_features(bars)

    assert len(features) == len(bars)
    assert features["obv_slope"].iloc[50:].notna().any()
    assert np.issubdtype(features["close"].dtype, np.floating)
    assert np.issubdtype(features["tick_volume"].dtype, np.floating)


def test_history_coerces_strings_before_validation(tmp_path):
    raw = pd.DataFrame({
        "time": [1735689600, 1735689660, 1735689720],
        "open": pd.Series(["2900.0", 2900.2, "bad"], dtype=object),
        "high": pd.Series(["2900.4", "2900.1", "x"], dtype=object),
        "low": pd.Series([2899.8, 2900.3, None], dtype=object),
        "close": pd.Series(["2900.2", "2900.1", "bad"], dtype=object),
        "volume": pd.Series(["10", 11, "bad"], dtype=object),
    })
    months = [(2025, 1, "1/OANDA_TEST_1_2025-01-01_to_2025-01-31.parquet")]
    with patch.object(history, "list_months", return_value=months), \
            patch.object(history, "_get", return_value=b"parquet"), \
            patch("pandas.read_parquet", return_value=raw):
        bars = history.download("bunqi/OANDA_TEST", tmp_path, date(2025, 1, 1), say=lambda *_: None)

    assert len(bars) == 1
    assert bars.iloc[0]["close"] == pytest.approx(2900.2)
    assert bars.iloc[0]["volume"] == pytest.approx(10.0)
    assert all(np.issubdtype(bars[col].dtype, np.floating)
               for col in ("open", "high", "low", "close", "volume"))


def test_to_broker_bid_returns_numeric_columns():
    idx = pd.date_range("2025-01-01", periods=2, freq="1min", tz="UTC")
    mid = pd.DataFrame({
        "open": pd.Series(["2900.0", 2900.2], index=idx, dtype=object),
        "high": pd.Series([2900.4, "2900.5"], index=idx, dtype=object),
        "low": pd.Series(["2899.8", 2900.0], index=idx, dtype=object),
        "close": pd.Series([2900.2, "2900.3"], index=idx, dtype=object),
        "volume": pd.Series(["10", 11], index=idx, dtype=object),
    }, index=idx)

    bars = history.to_broker_bid(mid, -0.16, 0.32)

    assert bars["close"].tolist() == pytest.approx([2900.04, 2900.14])
    assert bars["tick_volume"].tolist() == pytest.approx([10.0, 11.0])
    assert all(np.issubdtype(bars[col].dtype, np.floating)
               for col in ("open", "high", "low", "close", "tick_volume", "spread"))
