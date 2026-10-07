"""Unit tests: AZBit symbol/timeframe mapping, HTTP layer, response parsing."""
from __future__ import annotations

from datetime import datetime, timezone

import pytest
import requests

from conftest import FakeResponse, azbit_ohlc_rows, make_azbit_client
from nobitex_adapter.azbit_client import (
    AzbitAPIError,
    AzbitError,
    AzbitRateLimitedError,
    parse_azbit_date,
    parse_ohlc_payload,
)
from nobitex_adapter.symbols import SymbolError, azbit_to_freqtrade, freqtrade_to_azbit
from nobitex_adapter.timeframes import (
    AZBIT_INTERVALS,
    TimeframeError,
    from_azbit_interval,
    normalize_timeframes,
    to_azbit_interval,
)

START = int(datetime(2024, 6, 1, tzinfo=timezone.utc).timestamp())


# ------------------------------------------------------------ symbol mapping
def test_freqtrade_to_azbit_generic():
    assert freqtrade_to_azbit("BTC/USDT") == "BTC_USDT"
    assert freqtrade_to_azbit("eth/usdt") == "ETH_USDT"
    assert freqtrade_to_azbit("SOL/USDT") == "SOL_USDT"
    assert freqtrade_to_azbit("BTC/ETH") == "BTC_ETH"
    # generic: works for ANY base/quote, nothing hardcoded
    assert freqtrade_to_azbit("1INCH/USDT") == "1INCH_USDT"


@pytest.mark.parametrize("bad", ["BTC", "", "BTC/", "/USDT", "BTC__USDT", "BTC/USDT/ETH", None, 5])
def test_freqtrade_to_azbit_rejects_bad(bad):
    with pytest.raises(SymbolError):
        freqtrade_to_azbit(bad)


def test_azbit_to_freqtrade_generic():
    assert azbit_to_freqtrade("BTC_USDT") == "BTC/USDT"
    assert azbit_to_freqtrade("btc_usdt") == "BTC/USDT"
    assert azbit_to_freqtrade("ETH_BTC") == "ETH/BTC"
    # idempotent on ft-style input
    assert azbit_to_freqtrade("BTC/USDT") == "BTC/USDT"
    assert azbit_to_freqtrade("btc/usdt") == "BTC/USDT"


@pytest.mark.parametrize("bad", ["BTCUSDT", "", "_USDT", "BTC_", "___", None, 5])
def test_azbit_to_freqtrade_rejects_bad(bad):
    with pytest.raises(SymbolError):
        azbit_to_freqtrade(bad)


def test_azbit_symbol_roundtrip():
    for ft in ("BTC/USDT", "ETH/USDT", "SOL/USDT", "1INCH/USDT", "BTC/ETH"):
        assert azbit_to_freqtrade(freqtrade_to_azbit(ft)) == ft


# --------------------------------------------------------- timeframe mapping
def test_azbit_interval_mapping_documented_values():
    # exact mapping required by the mission, validated against the docs'
    # interval vocabulary (year/month/day/hour4/hour/minutes30/minutes15/
    # minutes5/minutes3/minute)
    assert to_azbit_interval("5m") == "minutes5"
    assert to_azbit_interval("15m") == "minutes15"
    assert to_azbit_interval("1h") == "hour"
    assert to_azbit_interval("4h") == "hour4"
    assert to_azbit_interval("1d") == "day"
    assert to_azbit_interval("1m") == "minute"
    assert to_azbit_interval("30m") == "minutes30"


def test_azbit_interval_roundtrip():
    for tf, iv in AZBIT_INTERVALS.items():
        assert from_azbit_interval(iv) == tf


def test_azbit_unsupported_timeframes_raise_not_hardcoded():
    # AZBit has no 3h/6h/12h/2d/3d: must raise, never silently map
    for tf in ("3h", "6h", "12h", "2d", "3d", "1w"):
        with pytest.raises(TimeframeError):
            to_azbit_interval(tf)
    with pytest.raises(TimeframeError):
        from_azbit_interval("hour3")


def test_normalize_timeframes_exchange_aware():
    # same canonical boundary, exchange-selected support set
    assert normalize_timeframes("5m,15m,1h,4h,1d", exchange="azbit") == [
        "5m", "15m", "1h", "4h", "1d"]
    assert normalize_timeframes("5m 15m 1h", exchange="azbit") == ["5m", "15m", "1h"]
    assert normalize_timeframes("1d", exchange="azbit") == ["1d"]  # never char-split
    with pytest.raises(TimeframeError) as exc:
        normalize_timeframes("5m,3h", exchange="azbit")
    assert "'3h'" in str(exc.value) and "AZBit" in str(exc.value)
    # default behaviour unchanged (Nobitex set)
    assert normalize_timeframes("5m,3h") == ["5m", "3h"]


# --------------------------------------------------------- timestamp parsing
def test_parse_date_naive_assumed_utc():
    assert parse_azbit_date("2024-06-01T00:00:59") == START + 59


def test_parse_date_z_and_millis():
    # docs example shape: millis + Z; sub-second is TRUNCATED, never rounded
    assert parse_azbit_date("2024-06-01T00:00:59.999Z") == START + 59
    assert parse_azbit_date("2024-06-01T00:00:59Z") == START + 59


def test_parse_date_offsets():
    assert parse_azbit_date("2024-06-01T03:30:59+03:30") == START + 59
    assert parse_azbit_date("2024-06-01T00:00:59+00:00") == START + 59


def test_parse_date_unix_numbers():
    assert parse_azbit_date(START + 59) == START + 59
    assert parse_azbit_date((START + 59) * 1000) == START + 59  # millis magnitude


@pytest.mark.parametrize("bad", ["", "not-a-date", "2024-13-99T99:99:99", None, True, 123,
                                 "1990-01-01T00:00:00", "2050-01-01T00:00:00"])
def test_parse_date_rejects_bad(bad):
    with pytest.raises(AzbitAPIError):
        parse_azbit_date(bad)


# ------------------------------------------------------------ payload parsing
def test_parse_valid_payload():
    rows = azbit_ohlc_rows(START, 3, 300)
    out = parse_ohlc_payload(rows, ctx="t")
    assert len(out) == 3
    assert out[0].ts == START
    assert out[0].high >= max(out[0].open, out[0].close)
    assert out[0].low <= min(out[0].open, out[0].close)
    assert out[0].volume == 5.0
    assert out[0].volume_base == pytest.approx(500.0)
    assert out[0].raw_date == "2024-06-01T00:00:00"


def test_parse_empty_array_is_valid_no_data():
    assert parse_ohlc_payload([]) == []


def test_parse_missing_volumeBase_defaults_zero():
    rows = azbit_ohlc_rows(START, 1)
    del rows[0]["volumeBase"]
    out = parse_ohlc_payload(rows)
    assert out[0].volume_base == 0.0


@pytest.mark.parametrize("field", ["open", "max", "min", "close", "volume"])
def test_parse_null_field_rejected(field):
    rows = azbit_ohlc_rows(START, 1)
    rows[0][field] = None
    with pytest.raises(AzbitAPIError):
        parse_ohlc_payload(rows)


def test_parse_missing_date_rejected():
    rows = azbit_ohlc_rows(START, 1)
    del rows[0]["date"]
    with pytest.raises(AzbitAPIError):
        parse_ohlc_payload(rows)


@pytest.mark.parametrize("bad", ["oops", 42, True, None, {"weird": 1}, [{"date": "x"}]])
def test_parse_malformed_payloads_rejected(bad):
    with pytest.raises(AzbitAPIError):
        parse_ohlc_payload(bad)


def test_parse_error_envelope_rejected():
    with pytest.raises(AzbitAPIError) as exc:
        parse_ohlc_payload({"Code": 110401, "Message": "Unknown pair"})
    assert "110401" in str(exc.value)


def test_parse_string_numbers_accepted():
    rows = azbit_ohlc_rows(START, 1)
    rows[0]["open"] = "100.5"
    rows[0]["volume"] = "7"
    out = parse_ohlc_payload(rows)
    assert out[0].open == 100.5
    assert out[0].volume == 7.0


def test_parse_non_finite_rejected():
    rows = azbit_ohlc_rows(START, 1)
    rows[0]["close"] = float("inf")
    with pytest.raises(AzbitAPIError):
        parse_ohlc_payload(rows)


def test_parse_preserves_row_order_and_duplicates():
    rows = azbit_ohlc_rows(START, 3, 300)
    shuffled = [rows[2], rows[0], rows[0]]  # out-of-order + duplicate, verbatim
    out = parse_ohlc_payload(shuffled)
    assert [c.ts for c in out] == [START + 600, START, START]


# ------------------------------------------------------- URL/params encoding
def test_ohlc_page_builds_documented_request():
    rows = azbit_ohlc_rows(START, 2, 300)
    client = make_azbit_client([FakeResponse(200, rows)])
    out = client.ohlc_page("BTC_USDT", "minutes5", START, START + 600)
    assert len(out) == 2
    call = client.session.calls[0]
    assert call["url"] == "http://test.azbit.local/api/ohlc"
    assert call["params"] == {
        "interval": "minutes5",
        "currencyPairCode": "BTC_USDT",
        "start": "2024-06-01T00:00:00",
        "end": "2024-06-01T00:10:00",
    }


# ------------------------------------------------------------- HTTP behavior
def test_retry_on_network_error_then_success():
    rows = azbit_ohlc_rows(START, 1)
    sleeps: list[float] = []
    client = make_azbit_client(
        [requests.ConnectionError("down"), FakeResponse(200, rows)],
        sleep=sleeps.append,
    )
    out = client.ohlc_page("BTC_USDT", "minutes5", START, START + 300)
    assert len(out) == 1
    assert sleeps == [1.0]


def test_retry_exhausted_raises_with_context():
    client = make_azbit_client(
        [requests.ConnectionError("down")] * 6, sleep=lambda s: None, max_retries=5
    )
    with pytest.raises(AzbitError) as exc:
        client.ohlc_page("BTC_USDT", "minutes5", START, START + 300)
    assert "/api/ohlc" in exc.value.context()


def test_429_honors_retry_after_then_succeeds():
    rows = azbit_ohlc_rows(START, 1)
    r429 = FakeResponse(200, {"Code": 1, "Message": "slow down"})
    r429.status_code = 429
    r429.headers = {"Retry-After": "3"}
    r429.text = "rate limited"
    sleeps: list[float] = []
    client = make_azbit_client([r429, FakeResponse(200, rows)], sleep=sleeps.append)
    out = client.ohlc_page("BTC_USDT", "minutes5", START, START + 300)
    assert len(out) == 1
    assert sleeps == [3.0]


def test_429_exhausted_raises_rate_limited():
    r429 = FakeResponse(200, {})
    r429.status_code = 429
    r429.headers = {}
    r429.text = "slow"
    client = make_azbit_client([r429] * 6, sleep=lambda s: None, max_retries=1)
    with pytest.raises(AzbitRateLimitedError) as exc:
        client.ohlc_page("BTC_USDT", "minutes5", START, START + 300)
    assert exc.value.retry_after == 5.0


def test_500_retries_then_succeeds():
    rows = azbit_ohlc_rows(START, 1)
    bad = FakeResponse(500, {}, text="boom")
    sleeps: list[float] = []
    client = make_azbit_client([bad, FakeResponse(200, rows)], sleep=sleeps.append)
    assert len(client.ohlc_page("BTC_USDT", "minutes5", START, START + 300)) == 1
    assert sleeps == [1.0]


def test_400_raises_immediately():
    bad = FakeResponse(400, {}, text="bad request")
    client = make_azbit_client([bad])
    with pytest.raises(AzbitError):
        client.ohlc_page("BTC_USDT", "minutes5", START, START + 300)
    assert len(client.session.calls) == 1


def test_200_error_envelope_raises():
    client = make_azbit_client([FakeResponse(200, {"Code": 110401, "Message": "no pair"})])
    with pytest.raises(AzbitAPIError):
        client.ohlc_page("BTC_USDT", "minutes5", START, START + 300)


# ------------------------------------------------------- market discovery
def _pairs_payload():
    return [
        {"currencyPairCode": "BTC_USDT", "isActive": True},
        {"currencyPairCode": "ETH_USDT", "isActive": True},
        {"currencyPairCode": "DOGE_USDT", "isActive": False},
        {"currencyPairCode": "BTC_ETH", "isActive": True},
    ]


def _tickers_payload():
    return [
        {"currencyPairCode": "BTC_USDT", "price": 67000.0, "volume24h": 123.5,
         "priceChangePercentage24h": 1.2, "bidPrice": 66999.0, "askPrice": 67001.0,
         "low24h": 66000.0, "high24h": 68000.0, "price24hAgo": 66000.0},
        {"currencyPairCode": "ETH_USDT", "price": 3500.0, "volume24h": 999.0,
         "priceChangePercentage24h": -0.5},
    ]


def test_discover_markets_via_reference():
    client = make_azbit_client(
        [FakeResponse(200, _pairs_payload()), FakeResponse(200, _tickers_payload())]
    )
    markets = client.discover_markets(quote="USDT")
    assert [m.ft_symbol for m in markets] == ["BTC/USDT", "DOGE/USDT", "ETH/USDT"]
    btc = markets[0]
    assert btc.symbol == "BTC_USDT" and btc.base == "BTC" and btc.quote == "USDT"
    assert btc.active is True
    assert btc.price == 67000.0
    assert next(m for m in markets if m.ft_symbol == "DOGE/USDT").active is False


def test_discover_markets_quote_filter():
    client = make_azbit_client(
        [FakeResponse(200, _pairs_payload()), FakeResponse(200, _tickers_payload())]
    )
    markets = client.discover_markets(quote="ETH")
    assert [m.ft_symbol for m in markets] == ["BTC/ETH"]


def test_discover_markets_falls_back_to_tickers():
    bad = FakeResponse(404, {}, text="not found")
    client = make_azbit_client([bad, FakeResponse(200, _tickers_payload())])
    markets = client.discover_markets()
    assert [m.ft_symbol for m in markets] == ["BTC/USDT", "ETH/USDT"]
    assert all(m.active for m in markets)


def test_discover_markets_unknown_quote_is_empty_not_error():
    client = make_azbit_client(
        [FakeResponse(200, _pairs_payload()), FakeResponse(200, _tickers_payload())]
    )
    assert client.discover_markets(quote="XXX") == []
