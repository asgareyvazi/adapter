"""Unit tests: gap/quality semantics for irregular AZBit-style series.

The validator must DISTINGUISH (not paper over): perfect series, one
missing candle, multi-hour gaps, irregular floating timestamps,
duplicates and reversed order — via counters + the `quality` verdict.
No synthetic candles are ever created: `validate` with repair=False
returns the input untouched.
"""
from __future__ import annotations

import pandas as pd
import pytest

from conftest import make_df
from nobitex_adapter.validator import validate

BASE = 1_716_720_000  # 2024-06-01 00:00:00 UTC


def _df(dates: list[int]) -> pd.DataFrame:
    n = len(dates)
    return pd.DataFrame({
        "date": dates,
        "open": [100.0] * n,
        "high": [101.0] * n,
        "low": [99.0] * n,
        "close": [100.5] * n,
        "volume": [5.0] * n,
    })


def test_perfect_5m_series_is_contiguous():
    df = make_df(BASE, 100, 300)
    out, rep = validate(df, "BTC/USDT", "5m")
    assert rep.quality == "CONTIGUOUS"
    assert rep.status == "PASS"
    assert rep.missing_intervals == 0
    assert len(out) == 100  # untouched


def test_one_missing_candle_is_gapped():
    dates = [BASE + i * 300 for i in range(100) if i != 50]
    _, rep = validate(_df(dates), "BTC/USDT", "5m")
    assert rep.quality == "GAPPED"
    assert rep.missing_intervals == 1
    assert len(rep.gap_ranges) == 1
    assert "missing" in rep.gap_ranges[0]


def test_multi_hour_gap_reported_with_range():
    dates = [BASE + i * 300 for i in range(10)]
    dates += [BASE + 10 * 300 + 4 * 3600 + i * 300 for i in range(10)]  # 4h hole
    _, rep = validate(_df(dates), "BTC/USDT", "5m")
    assert rep.quality == "GAPPED"
    assert rep.missing_intervals == 1
    assert "48 missing" in rep.gap_ranges[0]  # 14700s hole = 49 slots - 1 boundary


def test_irregular_floating_timestamps_are_gapped_not_silently_fixed():
    # real AZBit shape: 00:00:59, 00:06:37, ... (floating, ~303s avg)
    dates = [BASE + 59, BASE + 397, BASE + 690, BASE + 975, BASE + 1308]
    out, rep = validate(_df(dates), "BTC/USDT", "5m")
    assert rep.quality == "GAPPED"
    assert rep.candle_spacing_ok is False
    # validator never aligns/rounds: output equals input
    assert out["date"].tolist() == dates


def test_duplicate_timestamp_is_duplicate():
    dates = [BASE + i * 300 for i in range(10)]
    dates.insert(6, BASE + 5 * 300)  # in-order duplicate (monotonic, one repeat)
    out, rep = validate(_df(dates), "BTC/USDT", "5m")
    assert rep.duplicates == 1
    assert rep.non_monotonic == 0
    assert rep.quality == "DUPLICATE"
    assert len(out) == 11  # no silent removal without repair=True


def test_trailing_duplicate_is_out_of_order_first():
    # an appended repeat breaks monotonicity too: OUT_OF_ORDER wins by
    # priority, but the duplicate counter stays visible
    dates = [BASE + i * 300 for i in range(10)] + [BASE + 5 * 300]
    _, rep = validate(_df(dates), "BTC/USDT", "5m")
    assert rep.duplicates == 1
    assert rep.non_monotonic == 1
    assert rep.quality == "OUT_OF_ORDER"


def test_reversed_order_is_out_of_order():
    dates = [BASE + i * 300 for i in range(10)][::-1]
    _, rep = validate(_df(dates), "BTC/USDT", "5m")
    assert rep.non_monotonic == 9
    assert rep.quality == "OUT_OF_ORDER"
    assert rep.status == "FAIL"


def test_empty_is_empty():
    _, rep = validate(_df([]), "BTC/USDT", "5m")
    assert rep.quality == "EMPTY"
    assert rep.rows == 0


def test_broken_ohlc_is_invalid():
    df = make_df(BASE, 10, 300)
    df.loc[3, "high"] = 1.0  # high < open/close
    _, rep = validate(df, "BTC/USDT", "5m")
    assert rep.invalid_ohlc == 1
    assert rep.quality == "INVALID"


def test_negative_volume_is_invalid():
    df = make_df(BASE, 10, 300)
    df.loc[0, "volume"] = -2.0
    _, rep = validate(df, "BTC/USDT", "5m")
    assert rep.invalid_volume == 1
    assert rep.quality == "INVALID"


def test_quality_priority_invalid_beats_gapped():
    dates = [BASE + i * 300 for i in range(10) if i != 5]  # gap present...
    df = _df(dates)
    df.loc[0, "low"] = -1.0  # ...but INVALID wins
    _, rep = validate(df, "BTC/USDT", "5m")
    assert rep.quality == "INVALID"
