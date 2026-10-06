"""Unit tests: timeframe parsing and Nobitex resolution mapping."""
from __future__ import annotations

import pytest

from nobitex_adapter.timeframes import (
    NOBITEX_RESOLUTIONS,
    X8_BASE_TIMEFRAME,
    X8_BTC_INFO_TIMEFRAMES,
    X8_INFORMATIVE_TIMEFRAMES,
    from_nobitex_resolution,
    parse_timeframe,
    startup_candles,
    to_nobitex_resolution,
    TimeframeError,
)


@pytest.mark.parametrize("tf,seconds", [
    ("1m", 60), ("5m", 300), ("15m", 900), ("30m", 1800),
    ("1h", 3600), ("3h", 10800), ("4h", 14400), ("12h", 43200),
    ("1d", 86400), ("2d", 172800), ("3d", 259200), ("1w", 604800),
])
def test_parse_timeframe_seconds(tf, seconds):
    assert parse_timeframe(tf).seconds == seconds


def test_parse_timeframe_case_and_spaces():
    assert parse_timeframe(" 5M ").seconds == 300


@pytest.mark.parametrize("bad", ["", "5", "m5", "5x", "abc", "1.5m", "-5m", None, 5])
def test_parse_timeframe_invalid(bad):
    with pytest.raises(TimeframeError):
        parse_timeframe(bad)


def test_nobitex_resolution_mapping():
    assert to_nobitex_resolution("5m") == "5"
    assert to_nobitex_resolution("1h") == "60"
    assert to_nobitex_resolution("4h") == "240"
    assert to_nobitex_resolution("1d") == "D"
    assert to_nobitex_resolution("2d") == "2D"


def test_resolution_roundtrip():
    for tf, res in NOBITEX_RESOLUTIONS.items():
        assert from_nobitex_resolution(res) == tf


def test_from_nobitex_resolution_unknown():
    with pytest.raises(TimeframeError):
        from_nobitex_resolution("99")


def test_startup_candles():
    assert startup_candles("5m") >= 800  # X8 uses 800
    assert startup_candles("1d") >= 200  # long EMA warmup on 1d
    assert startup_candles("1w") == 200  # unknown -> default


def test_x8_timeframe_constants():
    assert X8_BASE_TIMEFRAME == "5m"
    assert set(X8_INFORMATIVE_TIMEFRAMES) == {"15m", "1h", "4h", "1d"}
    assert X8_BTC_INFO_TIMEFRAMES == ("4h",)
