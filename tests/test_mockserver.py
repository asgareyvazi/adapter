"""Contract tests for the in-repo mock Nobitex API (the sandbox stand-in for
the public endpoints, used by integration/e2e tests)."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from nobitex_adapter.mockserver import app

client = TestClient(app)


def test_market_stats_usdt():
    r = client.get("/market/stats", params={"dstCurrency": "USDT"})
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    stats = body["stats"]
    assert "btc-usdt" in stats
    entry = stats["btc-usdt"]
    for k in ("isClosed", "latest", "volumeSrc", "volumeDst"):
        assert k in entry
    # no IRT markets leak into a USDT query
    assert not any(k.endswith("-irt") for k in stats)


def test_market_stats_closed_market_flagged():
    r = client.get("/market/stats", params={"dstCurrency": "USDT"})
    stats = r.json()["stats"]
    assert stats["sushi-usdt"]["isClosed"] is True
    assert stats["btc-usdt"]["isClosed"] is False


def test_history_columnar_ok():
    r = client.get("/market/udf/history", params={
        "symbol": "BTCUSDT", "resolution": "5", "from": 1_700_000_000,
        "to": 1_700_000_000 + 100 * 300, "page": 1,
    })
    assert r.status_code == 200
    p = r.json()
    assert p["s"] == "ok"
    n = len(p["t"])
    assert 0 < n <= 100
    for col in ("o", "h", "l", "c", "v"):
        assert len(p[col]) == n
    # OHLC consistency
    for i in range(n):
        assert p["h"][i] >= max(p["o"][i], p["c"][i])
        assert p["l"][i] <= min(p["o"][i], p["c"][i])
        assert p["v"][i] >= 0
    # 5m spacing
    assert all(p["t"][i + 1] - p["t"][i] == 300 for i in range(n - 1))


def test_history_max_500_per_page():
    r = client.get("/market/udf/history", params={
        "symbol": "BTCUSDT", "resolution": "5", "from": 1_700_000_000,
        "to": 1_700_000_000 + 10_000 * 300, "page": 1,
    })
    assert len(r.json()["t"]) == 500


def test_history_pagination():
    base = 1_700_000_000
    p1 = client.get("/market/udf/history", params={
        "symbol": "BTCUSDT", "resolution": "5", "from": base,
        "to": base + 1200 * 300, "page": 1}).json()
    p2 = client.get("/market/udf/history", params={
        "symbol": "BTCUSDT", "resolution": "5", "from": base,
        "to": base + 1200 * 300, "page": 2}).json()
    assert p1["s"] == "ok" and p2["s"] == "ok"
    assert len(p1["t"]) == 500
    assert len(p2["t"]) == 500
    # page 2 continues where page 1 left off
    assert p2["t"][0] > p1["t"][-1]


def test_history_no_data_before_minute_start():
    """Minute candles exist only from ~2022-03-20 (documented limitation)."""
    r = client.get("/market/udf/history", params={
        "symbol": "BTCUSDT", "resolution": "5",
        "from": 1_500_000_000,  # 2017
        "to": 1_500_086_400,
        "page": 1,
    })
    assert r.json()["s"] == "no_data"


def test_history_daily_available_earlier():
    """Daily candles exist from 2019 (mirrors real exchange depth)."""
    r = client.get("/market/udf/history", params={
        "symbol": "BTCUSDT", "resolution": "D",
        "from": 1_550_000_000, "to": 1_550_000_000 + 10 * 86400, "page": 1,
    })
    assert r.json()["s"] == "ok"


def test_history_unknown_symbol():
    r = client.get("/market/udf/history", params={
        "symbol": "ZZZZNOPE", "resolution": "5",
        "from": 1_700_000_000, "to": 1_700_001_500, "page": 1,
    })
    body = r.json()
    assert body.get("s") == "error"  # documented failure shape
    assert "Invalid" in body.get("errmsg", "")


def test_rate_limit_429_with_backoff():
    """With a tiny rlimit the mock returns 429 + backOff (documented shape)."""
    got_429 = False
    for i in range(30):
        r = client.get("/market/stats", params={"dstCurrency": "USDT", "rlimit": 1})
        if r.status_code == 429:
            body = r.json()
            assert body["status"] == "failed"
            assert body["code"] == "TooManyRequests"
            assert isinstance(body["backOff"], (int, float)) and body["backOff"] > 0
            got_429 = True
            break
    assert got_429, "rlimit=1 should trigger at least one 429 in 30 calls"


def test_orderbook_and_trades_shapes():
    r = client.get("/v3/orderbook/BTCUSDT")
    assert r.status_code == 200
    body = r.json()
    assert "bids" in body and "asks" in body

    r = client.get("/v2/trades/BTCUSDT")
    assert r.status_code == 200
    assert "trades" in r.json()
