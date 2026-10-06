"""Unit tests: NobitexClient retry, backoff, rate-limit and error handling."""
from __future__ import annotations

import pytest
import requests

from conftest import FakeResponse, candles_payload, make_client
from nobitex_adapter.nobitex_client import (
    NobitexAPIError,
    NobitexError,
    NobitexNoData,
    NobitexRateLimitedError,
)


def test_retry_on_5xx_then_success():
    sleeps: list[float] = []
    client = make_client(
        [FakeResponse(500, None, "oops"), FakeResponse(500, None, "oops"),
         FakeResponse(200, {"status": "ok", "stats": {}})],
        sleeps=sleeps,
    )
    out = client._get("/market/stats")
    assert out["status"] == "ok"
    assert len(client.session.calls) == 3
    # exponential backoff: 1s then 2s
    assert sleeps == [1.0, 2.0]


def test_retry_on_network_error_then_success():
    sleeps: list[float] = []
    client = make_client(
        [requests.ConnectionError("boom"), FakeResponse(200, {"s": "ok"})],
        sleeps=sleeps,
    )
    out = client._get("/market/udf/history")
    assert out["s"] == "ok"
    assert sleeps == [1.0]


def test_gives_up_after_max_retries():
    sleeps: list[float] = []
    client = make_client(
        [requests.ConnectionError("boom")] * 6,  # 1 + 5 retries
        sleeps=sleeps,
    )
    with pytest.raises(NobitexError) as exc:
        client._get("/market/udf/history")
    assert "retries" in str(exc.value)
    assert len(client.session.calls) == 6
    assert len(sleeps) == 5
    assert sleeps == [1.0, 2.0, 4.0, 8.0, 16.0]


def test_429_honors_server_backoff():
    sleeps: list[float] = []
    client = make_client(
        [FakeResponse(429, {"code": "TooManyRequests", "backOff": 2, "limit": 5}),
         FakeResponse(200, {"s": "ok"})],
        sleeps=sleeps,
    )
    out = client._get("/market/udf/history")
    assert out["s"] == "ok"
    assert sleeps == [2.0]  # server-specified backOff, not the default schedule


def test_429_exhausts_retries_with_rate_error():
    client = make_client(
        [FakeResponse(429, {"code": "TooManyRequests", "backOff": 1})] * 6,
    )
    with pytest.raises(NobitexRateLimitedError) as exc:
        client._get("/market/udf/history")
    assert exc.value.back_off == 1.0


def test_json_failed_status_is_api_error():
    client = make_client([FakeResponse(200, {"status": "failed", "code": "BadSymbol",
                                             "message": "unknown symbol"})])
    with pytest.raises(NobitexAPIError) as exc:
        client._get("/market/udf/history", params={"symbol": "NOPE"})
    assert exc.value.code == "BadSymbol"
    assert "unknown symbol" in str(exc.value)


def test_client_error_400_no_retry():
    client = make_client([FakeResponse(400, None, "bad request")])
    with pytest.raises(NobitexError) as exc:
        client._get("/market/udf/history")
    assert exc.value.http_status == 400
    assert len(client.session.calls) == 1  # no retry for 4xx


def test_error_carries_request_context():
    client = make_client([FakeResponse(500, None, "x")] * 6)
    with pytest.raises(NobitexError) as exc:
        client._get("/v2/trades/BTCUSDT", params={"page": 1})
    assert exc.value.endpoint == "/v2/trades/BTCUSDT"
    assert exc.value.params == {"page": 1}
    assert exc.value.http_status == 500


def test_no_data_from_history_raises_nodata():
    client = make_client([FakeResponse(200, {"s": "no_data"})])
    with pytest.raises(NobitexNoData):
        client.candles_page("BTCUSDT", "5", 0, 1000, page=1)


def test_candles_page_params_documented():
    """candles_page must send the documented query params (unix seconds)."""
    client = make_client([FakeResponse(200, candles_payload(1, 100, 300))])
    client.candles_page("BTCUSDT", "5", 1_700_000_000, 1_700_001_500, page=2)
    call = client.session.calls[0]
    assert call["url"].endswith("/market/udf/history")
    assert call["params"] == {
        "symbol": "BTCUSDT",
        "resolution": "5",
        "from": 1_700_000_000,
        "to": 1_700_001_500,
        "page": 2,
    }


def test_market_stats_maps_to_stats_key():
    client = make_client([FakeResponse(200, {"status": "ok",
                                             "stats": {"btc-usdt": {"latest": 1.0}}})])
    stats = client.market_stats(dst="USDT")
    assert "btc-usdt" in stats
    # dstCurrency must be forwarded
    assert client.session.calls[0]["params"] == {"dstCurrency": "USDT"}


def test_market_stats_missing_stats_object():
    client = make_client([FakeResponse(200, {"status": "ok"})])
    with pytest.raises(NobitexAPIError):
        client.market_stats()
