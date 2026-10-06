"""Unit tests: OHLCV payload parsing (malformed-data detection)."""
from __future__ import annotations

import pytest

from conftest import candles_payload
from nobitex_adapter.nobitex_client import (
    NobitexAPIError,
    NobitexError,
    NobitexNoData,
    parse_candles_payload,
)


def test_parse_ok_payload():
    p = candles_payload(3, start_ts=1_700_000_000, interval=300)
    out = parse_candles_payload(p, ctx="test")
    assert len(out) == 3
    assert out[0].ts == 1_700_000_000
    assert out[1].ts == 1_700_000_300
    assert out[0].open == 100.0
    assert out[0].high == pytest.approx(101.0)
    assert out[0].low == pytest.approx(99.0)
    assert out[0].close == pytest.approx(100.5)
    assert out[0].volume == 10.0


def test_parse_no_data():
    with pytest.raises(NobitexNoData):
        parse_candles_payload({"s": "no_data"}, ctx="test")


def test_parse_failed_status():
    with pytest.raises(NobitexAPIError) as exc:
        parse_candles_payload({"s": "failed", "errmsg": "bad symbol"}, ctx="test")
    assert "bad symbol" in str(exc.value)


def test_parse_non_dict_payload():
    with pytest.raises(NobitexError):
        parse_candles_payload([1, 2, 3], ctx="test")


def test_parse_column_length_mismatch():
    p = candles_payload(3, 1_700_000_000)
    p["o"] = p["o"][:2]  # open column shorter than t
    with pytest.raises(NobitexAPIError) as exc:
        parse_candles_payload(p, ctx="test")
    assert "mismatch" in str(exc.value)


def test_parse_malformed_float():
    p = candles_payload(2, 1_700_000_000)
    p["h"][0] = "not-a-number"
    with pytest.raises(NobitexError):
        parse_candles_payload(p, ctx="test")


def test_parse_numeric_strings_accepted():
    p = candles_payload(2, 1_700_000_000)
    p["o"][0] = "123.5"  # some deployments return numeric strings
    p["t"][0] = "1700000000"
    out = parse_candles_payload(p, ctx="test")
    assert out[0].open == 123.5
    assert out[0].ts == 1_700_000_000


def test_parse_null_values_rejected():
    p = candles_payload(2, 1_700_000_000)
    p["c"][1] = None
    with pytest.raises(NobitexError):
        parse_candles_payload(p, ctx="test")


def test_parse_empty_ok():
    out = parse_candles_payload({"s": "ok", "t": [], "o": [], "h": [], "l": [], "c": [], "v": []})
    assert out == []
