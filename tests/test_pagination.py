"""Unit tests: page-walking pagination in candles_range."""
from __future__ import annotations

import threading

import pytest

from conftest import FakeResponse, candles_payload, make_client
from nobitex_adapter.nobitex_client import NobitexError


def _page(n: int, start_ts: int, interval: int = 300) -> FakeResponse:
    return FakeResponse(200, candles_payload(n, start_ts, interval))


def test_single_page_range():
    client = make_client([_page(10, 1000)])
    out = client.candles_range("BTCUSDT", "5", 1000, 1000 + 10 * 300, page_size=500)
    assert len(out) == 10
    assert [c.ts for c in out] == [1000 + i * 300 for i in range(10)]


def test_walks_pages_until_short_page():
    """500 + 500 + 100 => stops after the short third page."""
    p1 = _page(500, 0)
    p2 = _page(500, 500 * 300)
    p3 = _page(100, 1000 * 300)
    client = make_client([p1, p2, p3])
    out = client.candles_range("BTCUSDT", "5", 0, 1100 * 300, page_size=500)
    assert len(out) == 1100
    assert len(client.session.calls) == 3
    # pages requested in order
    assert [c["params"]["page"] for c in client.session.calls] == [1, 2, 3]


def test_stops_on_no_data_mid_walk():
    client = make_client([_page(500, 0), FakeResponse(200, {"s": "no_data"})])
    out = client.candles_range("BTCUSDT", "5", 0, 10_000, page_size=500)
    assert len(out) == 500


def test_stops_on_empty_ok():
    client = make_client([FakeResponse(200, candles_payload(0, 0))])
    out = client.candles_range("BTCUSDT", "5", 0, 10_000, page_size=500)
    assert out == []


def test_range_sorted_chronologically():
    """Pages may interleave; the merged result must be ts-sorted and unique."""
    a = candles_payload(500, 0)
    b = candles_payload(100, 500 * 300)  # short page -> walk stops
    client = make_client([FakeResponse(200, a), FakeResponse(200, b)])
    out = client.candles_range("BTCUSDT", "5", 0, 1000 * 300, page_size=500)
    ts = [c.ts for c in out]
    assert ts == sorted(ts)
    assert len(set(ts)) == 600


def test_progress_cb_invoked_per_page():
    seen: list[tuple[int, int]] = []
    client = make_client([_page(500, 0), _page(10, 500 * 300)])
    client.candles_range(
        "BTCUSDT", "5", 0, 600 * 300, page_size=500,
        progress_cb=lambda total, page: seen.append((total, page)),
    )
    assert seen == [(500, 1), (510, 2)]


def test_stop_event_cancels_walk():
    stop = threading.Event()
    stop.set()
    client = make_client([])  # must never be called
    with pytest.raises(NobitexError) as exc:
        client.candles_range("BTCUSDT", "5", 0, 10_000, stop_event=stop)
    assert "cancelled" in str(exc.value)
    assert client.session.calls == []


def test_unbounded_range_terminates():
    """A page that keeps returning full pages must still terminate via
    no_data/short page (guards against infinite loops)."""
    responses = [_page(500, i * 500 * 300) for i in range(4)]
    responses.append(FakeResponse(200, {"s": "no_data"}))
    client = make_client(responses)
    out = client.candles_range("BTCUSDT", "5", 0, 10**12, page_size=500)
    assert len(out) == 2000
