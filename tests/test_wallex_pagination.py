"""Unit tests: Wallex cursor pagination (scripted HTTP, no network)."""
from __future__ import annotations

import threading

import pytest

from conftest import FakeResponse, make_wallex_client, wallex_history_payload
from nobitex_adapter.wallex_client import WallexError

pytestmark = pytest.mark.unit

START = 1_000_000
STEP = 300


def _page(ts_list, interval=STEP, price=100.0):
    """History payload with EXACT timestamps (cursor-walk control)."""
    n = len(ts_list)
    return {
        "s": "ok",
        "t": list(ts_list),
        "o": [f"{price:.4f}"] * n,
        "h": [f"{price * 1.01:.4f}"] * n,
        "l": [f"{price * 0.99:.4f}"] * n,
        "c": [f"{price * 1.005:.4f}"] * n,
        "v": ["1.0000"] * n,
    }


_EMPTY = {"s": "ok", "t": [], "o": [], "h": [], "l": [], "c": [], "v": []}


def test_single_page_covering_range_costs_one_request():
    end = START + 2 * STEP + 1  # last candle START+2*STEP >= end-1
    client = make_wallex_client(
        [FakeResponse(200, wallex_history_payload(3, START))])
    out = client.history_range("BTCUSDT", "5", START, end)
    assert [c.ts for c in out] == [START, START + STEP, START + 2 * STEP]
    assert client.request_count == 1


def test_two_cursor_pages_concatenated():
    end = START + 4 * STEP + 1
    p1 = _page([START, START + STEP, START + 2 * STEP])
    p2 = _page([START + 3 * STEP, START + 4 * STEP])
    client = make_wallex_client([FakeResponse(200, p1), FakeResponse(200, p2)])
    out = client.history_range("BTCUSDT", "5", START, end)
    assert [c.ts for c in out] == [START + i * STEP for i in range(5)]
    calls = client.session.calls
    assert calls[0]["params"]["from"] == START
    assert calls[1]["params"]["from"] == START + 2 * STEP + 1  # cursor advanced
    assert calls[1]["params"]["to"] == end


def test_empty_second_page_terminates():
    end = START + 10 * STEP
    p1 = _page([START, START + STEP])
    client = make_wallex_client(
        [FakeResponse(200, p1), FakeResponse(200, dict(_EMPTY))])
    out = client.history_range("BTCUSDT", "5", START, end)
    assert [c.ts for c in out] == [START, START + STEP]


def test_empty_first_page_means_no_data():
    client = make_wallex_client([FakeResponse(200, dict(_EMPTY))])
    assert client.history_range("BTCUSDT", "5", START, START + STEP) == []


def test_no_data_status_means_no_data():
    client = make_wallex_client([FakeResponse(200, {"s": "no_data"})])
    assert client.history_range("BTCUSDT", "5", START, START + STEP) == []


def test_boundary_duplicate_deduplicated():
    end = START + 10 * STEP
    p1 = _page([START, START + STEP])
    p2 = _page([START + STEP, START + 2 * STEP])  # server re-sends boundary
    client = make_wallex_client(
        [FakeResponse(200, p1), FakeResponse(200, p2),
         FakeResponse(200, dict(_EMPTY))])
    out = client.history_range("BTCUSDT", "5", START, end)
    assert [c.ts for c in out] == [START, START + STEP, START + 2 * STEP]


def test_cursor_stall_raises_not_loops():
    end = START + 10 * STEP
    p1 = _page([START, START + STEP])
    p2 = _page([START])  # older than cursor: server ignores `from`
    client = make_wallex_client([FakeResponse(200, p1), FakeResponse(200, p2)])
    with pytest.raises(WallexError, match="stalled"):
        client.history_range("BTCUSDT", "5", START, end)


def test_max_requests_bound_raises_not_truncates():
    end = START + 10 * STEP
    pages = [_page([START + i * STEP]) for i in range(5)]
    client = make_wallex_client([FakeResponse(200, p) for p in pages],
                                max_requests=2)
    with pytest.raises(WallexError, match="max_requests"):
        client.history_range("BTCUSDT", "5", START, end)


def test_out_of_order_rows_sorted_ascending():
    end = START + 2 * STEP + 1
    p = _page([START + 2 * STEP, START, START + STEP])
    client = make_wallex_client([FakeResponse(200, p)])
    out = client.history_range("BTCUSDT", "5", START, end)
    assert [c.ts for c in out] == [START, START + STEP, START + 2 * STEP]


def test_invalid_range_rejected():
    client = make_wallex_client([])
    with pytest.raises(WallexError, match="invalid range"):
        client.history_range("BTCUSDT", "5", START, START)


def test_stop_event_cancels():
    ev = threading.Event()
    ev.set()
    client = make_wallex_client([])
    with pytest.raises(WallexError, match="cancelled"):
        client.history_range("BTCUSDT", "5", START, START + STEP, stop_event=ev)
    assert client.request_count == 0
