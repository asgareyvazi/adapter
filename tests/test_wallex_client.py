"""Unit tests: Wallex symbols/timeframes/client (scripted HTTP, no network)."""
from __future__ import annotations

import pytest
import requests

from conftest import (
    FakeResponse,
    make_wallex_client,
    wallex_history_payload,
    wallex_markets_payload,
)
from nobitex_adapter.symbols import (
    SymbolError,
    freqtrade_to_wallex,
    wallex_to_freqtrade,
)
from nobitex_adapter.timeframes import (
    TimeframeError,
    from_wallex_resolution,
    normalize_timeframes,
    to_wallex_resolution,
)
from nobitex_adapter.wallex_client import (
    WallexAPIError,
    WallexError,
    WallexNoData,
    WallexRateLimitedError,
    parse_history_payload,
)

pytestmark = pytest.mark.unit


# ------------------------------------------------------------------ symbols
def test_wallex_to_freqtrade_usdt_and_tmn():
    assert wallex_to_freqtrade("BTCUSDT") == "BTC/USDT"
    assert wallex_to_freqtrade("BTCTMN") == "BTC/TMN"
    assert wallex_to_freqtrade("btctmn") == "BTC/TMN"
    assert wallex_to_freqtrade("BTC/USDT") == "BTC/USDT"  # passthrough


def test_freqtrade_to_wallex_generic():
    assert freqtrade_to_wallex("BTC/USDT") == "BTCUSDT"
    assert freqtrade_to_wallex("ETH/TMN") == "ETHTMN"


@pytest.mark.parametrize("bad", ["", "BTC", "BTCUSDTX", "BTC/", 123, None])
def test_wallex_symbol_rejects_bad(bad):
    with pytest.raises(SymbolError):
        wallex_to_freqtrade(bad)
    with pytest.raises(SymbolError):
        freqtrade_to_wallex(bad if isinstance(bad, str) else "nope")


def test_wallex_symbol_roundtrip():
    for ft in ("BTC/USDT", "ETH/TMN", "SOL/USDT", "DOGE/TMN"):
        assert wallex_to_freqtrade(freqtrade_to_wallex(ft)) == ft


# --------------------------------------------------------------- timeframes
def test_wallex_resolution_documented_minute_ladder():
    assert to_wallex_resolution("1m") == "1"
    assert to_wallex_resolution("5m") == "5"
    assert to_wallex_resolution("15m") == "15"
    assert to_wallex_resolution("30m") == "30"
    assert to_wallex_resolution("1h") == "60"  # the docs' only example value
    assert to_wallex_resolution("4h") == "240"
    assert to_wallex_resolution("1d") == "D"


def test_wallex_resolution_roundtrip():
    for tf in ("1m", "5m", "15m", "30m", "1h", "3h", "4h", "6h", "12h", "1d", "2d", "3d"):
        assert from_wallex_resolution(to_wallex_resolution(tf)) == tf


def test_wallex_unknown_timeframe_raises():
    with pytest.raises(TimeframeError):
        to_wallex_resolution("1w")
    with pytest.raises(TimeframeError):
        from_wallex_resolution("W")


def test_normalize_timeframes_wallex_supports_full_ladder():
    # Wallex serves 3h/6h/12h/2d/3d (unlike AZBit) — provisional, see
    # timeframes.WALLEX_RESOLUTIONS.
    assert normalize_timeframes("5m,3h,2d", exchange="wallex") == ["5m", "3h", "2d"]
    with pytest.raises(TimeframeError) as exc:
        normalize_timeframes("5m,1w", exchange="wallex")
    assert "Wallex" in str(exc.value)


# ------------------------------------------------------------------- parsing
def test_parse_valid_payload_number_strings():
    candles = parse_history_payload(wallex_history_payload(3, 1000), ctx="t")
    assert [c.ts for c in candles] == [1000, 1300, 1600]
    assert candles[0].open == 100.0
    assert candles[0].volume == 10.0


def test_parse_empty_arrays_is_valid_no_data():
    assert parse_history_payload(
        {"s": "ok", "t": [], "o": [], "h": [], "l": [], "c": [], "v": []}
    ) == []


def test_parse_no_data_raises_nodata():
    with pytest.raises(WallexNoData):
        parse_history_payload({"s": "no_data"})


def test_parse_error_status_raises_api_error():
    with pytest.raises(WallexAPIError) as exc:
        parse_history_payload({"s": "error", "errmsg": "Unknown symbol 'XX'"})
    assert "Unknown symbol" in str(exc.value)


def test_parse_column_mismatch_rejected():
    bad = wallex_history_payload(3, 1000)
    bad["c"] = bad["c"][:2]
    with pytest.raises(WallexAPIError):
        parse_history_payload(bad)


def test_parse_malformed_numeric_rejected():
    bad = wallex_history_payload(2, 1000)
    bad["o"][1] = "not-a-number"
    with pytest.raises(WallexAPIError):
        parse_history_payload(bad)


def test_parse_malformed_timestamp_rejected():
    bad = wallex_history_payload(2, 1000)
    bad["t"][0] = "yesterday"
    with pytest.raises(WallexAPIError):
        parse_history_payload(bad)


def test_parse_non_object_rejected():
    with pytest.raises(WallexAPIError):
        parse_history_payload([1, 2, 3])


def test_parse_docs_example_shape():
    """The exact documented response shape parses (t + o/h/l/c/v strings)."""
    payload = {
        "s": "ok",
        "t": [1654351200, 1654354800],
        "c": ["948896452.0000000000", "960015360.0000000000"],
        "o": ["950625912.0000000000", "948896452.0000000000"],
        "h": ["954590146.0000000000", "961381312.0000000000"],
        "l": ["948896452.0000000000", "948896452.0000000000"],
        "v": ["0.2314400000", "0.3148050000"],
    }
    candles = parse_history_payload(payload)
    assert [c.ts for c in candles] == [1654351200, 1654354800]
    assert candles[0].close == pytest.approx(948896452.0)
    assert candles[1].volume == pytest.approx(0.314805)


# ------------------------------------------------------------------ requests
def test_history_page_builds_documented_request():
    client = make_wallex_client([FakeResponse(200, wallex_history_payload(1, 1000))])
    out = client.history_page("BTCUSDT", "60", 1000, 2000)
    assert len(out) == 1
    call = client.session.calls[0]
    assert call["url"] == "http://test.wallex.local/v1/udf/history"
    assert call["params"] == {
        "symbol": "BTCUSDT", "resolution": "60", "from": 1000, "to": 2000}


def test_retry_on_network_error_then_success():
    err = requests.ConnectionError("down")
    client = make_wallex_client([err, FakeResponse(200, wallex_history_payload(1, 1000))])
    assert len(client.history_page("BTCUSDT", "60", 1000, 2000)) == 1
    assert client.request_count == 2


def test_retry_exhausted_raises_with_context():
    client = make_wallex_client(
        [requests.ConnectionError("down")] * 6, max_retries=5)
    with pytest.raises(WallexError) as exc:
        client.history_page("BTCUSDT", "60", 1000, 2000)
    assert "/v1/udf/history" in exc.value.context()


def test_429_honors_retry_after_header_then_succeeds():
    sleeps: list[float] = []
    limited = FakeResponse(429, {"success": False, "message": "slow down"})
    limited.headers = {"Retry-After": "2"}
    client = make_wallex_client(
        [limited, FakeResponse(200, wallex_history_payload(1, 1000))],
        sleep=sleeps.append,
    )
    assert len(client.history_page("BTCUSDT", "60", 1000, 2000)) == 1
    assert sleeps == [2.0]


def test_429_honors_body_hint_then_succeeds():
    sleeps: list[float] = []
    client = make_wallex_client(
        [FakeResponse(429, {"success": False, "retry_after": 3}),
         FakeResponse(200, wallex_history_payload(1, 1000))],
        sleep=sleeps.append,
    )
    client.history_page("BTCUSDT", "60", 1000, 2000)
    assert sleeps == [3.0]


def test_429_exhausted_raises_rate_limited():
    client = make_wallex_client(
        [FakeResponse(429, {"success": False})] * 6, max_retries=5)
    with pytest.raises(WallexRateLimitedError) as exc:
        client.history_page("BTCUSDT", "60", 1000, 2000)
    assert exc.value.retry_after == 5.0  # documented default, no hint sent


def test_500_retries_then_succeeds():
    client = make_wallex_client(
        [FakeResponse(500, {}, "boom"),
         FakeResponse(200, wallex_history_payload(1, 1000))])
    assert len(client.history_page("BTCUSDT", "60", 1000, 2000)) == 1


def test_400_raises_immediately():
    client = make_wallex_client([FakeResponse(400, {}, "bad resolution")])
    with pytest.raises(WallexError):
        client.history_page("BTCUSDT", "W", 1000, 2000)
    assert client.request_count == 1  # no retry on client errors


def test_success_false_envelope_raises():
    client = make_wallex_client(
        [FakeResponse(200, {"success": False, "message": "bad symbol"})])
    with pytest.raises(WallexAPIError) as exc:
        client.markets()
    assert "bad symbol" in str(exc.value)


def test_non_object_json_raises():
    client = make_wallex_client([FakeResponse(200, [1, 2])])
    with pytest.raises(WallexAPIError):
        client.markets()


def test_env_base_url_override(monkeypatch):
    monkeypatch.setenv("WALLEX_API_BASE", "http://mock.local:9")
    client = make_wallex_client([], base_url=None)  # None -> env fallback
    assert client.base_url == "http://mock.local:9"


# ----------------------------------------------------------------- discovery
def test_discover_markets_parses_symbols_and_stats():
    client = make_wallex_client(
        [FakeResponse(200, wallex_markets_payload("BTCUSDT", "BTCTMN"))])
    markets = client.discover_markets()
    assert [m.ft_symbol for m in markets] == ["BTC/TMN", "BTC/USDT"]  # sorted
    btc = next(m for m in markets if m.ft_symbol == "BTC/USDT")
    assert (btc.symbol, btc.base, btc.quote) == ("BTCUSDT", "BTC", "USDT")
    assert btc.price == 100.0
    assert btc.volume_base == pytest.approx(12.5)
    assert btc.volume_quote == pytest.approx(1250.0)
    assert btc.day_change_pct == 1.5
    assert btc.active is True


def test_discover_markets_quote_filter():
    client = make_wallex_client(
        [FakeResponse(200, wallex_markets_payload("BTCUSDT", "BTCTMN"))])
    assert [m.ft_symbol for m in client.discover_markets(quote="TMN")] == ["BTC/TMN"]


def test_discover_markets_unknown_quote_is_empty_not_error():
    client = make_wallex_client(
        [FakeResponse(200, wallex_markets_payload("BTCUSDT"))])
    assert client.discover_markets(quote="EUR") == []


def test_discover_markets_missing_symbols_object():
    client = make_wallex_client([FakeResponse(200, {"success": True, "result": {}})])
    with pytest.raises(WallexAPIError):
        client.discover_markets()


def test_discover_markets_falls_back_to_symbol_split():
    payload = {"success": True, "result": {"symbols": {
        "BTCUSDT": {"symbol": "BTCUSDT", "stats": {"lastPrice": "5"}}}}}
    client = make_wallex_client([FakeResponse(200, payload)])
    (m,) = client.discover_markets()
    assert m.ft_symbol == "BTC/USDT" and m.price == 5.0
