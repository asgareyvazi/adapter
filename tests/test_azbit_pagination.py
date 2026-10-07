"""Unit tests: AZBit cursor pagination (1000-row cap walk, boundary dedupe,
stall termination, request bounds)."""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from conftest import FakeResponse, azbit_ohlc_rows, make_azbit_client
from nobitex_adapter.azbit_client import AzbitAPIError

START = int(datetime(2024, 6, 1, tzinfo=timezone.utc).timestamp())
END = int(datetime(2024, 6, 6, tzinfo=timezone.utc).timestamp())


def test_two_pages_concatenated_like_real_observation():
    """The real observation: page 1 = 1000 rows, page 2 = 430 rows."""
    p1 = azbit_ohlc_rows(START, 1000, 300)
    last1 = START + 999 * 300
    p2 = azbit_ohlc_rows(last1 + 300, 430, 300)
    client = make_azbit_client([FakeResponse(200, p1), FakeResponse(200, p2)])
    out = client.ohlc_range("BTC_USDT", "minutes5", START, END)
    assert len(out) == 1430
    ts = [c.ts for c in out]
    assert ts == sorted(ts)
    assert len(set(ts)) == 1430
    # cursor advanced to last_ts+1 for the second request
    assert client.session.calls[1]["params"]["start"] == "2024-06-04T11:15:01"


def test_short_page_terminates():
    rows = azbit_ohlc_rows(START, 10, 300)
    client = make_azbit_client([FakeResponse(200, rows)])
    out = client.ohlc_range("BTC_USDT", "minutes5", START, END)
    assert len(out) == 10
    assert len(client.session.calls) == 1


def test_empty_first_page_means_no_data():
    client = make_azbit_client([FakeResponse(200, [])])
    assert client.ohlc_range("BTC_USDT", "minutes5", START, END) == []
    assert len(client.session.calls) == 1


def test_boundary_duplicate_deduplicated():
    """page1 last ts == page2 first ts -> final count deduplicated."""
    p1 = azbit_ohlc_rows(START, 1000, 300)
    last1 = START + 999 * 300
    p2 = azbit_ohlc_rows(last1, 5, 300)  # first row re-fetches the boundary
    client = make_azbit_client([FakeResponse(200, p1), FakeResponse(200, p2)])
    out = client.ohlc_range("BTC_USDT", "minutes5", START, END)
    assert len(out) == 1000 + 4
    ts = [c.ts for c in out]
    assert len(set(ts)) == len(ts)


def test_cursor_stall_raises_not_loops():
    """Same full page twice -> hard error, never an infinite loop."""
    p1 = azbit_ohlc_rows(START, 1000, 300)
    client = make_azbit_client([FakeResponse(200, p1), FakeResponse(200, p1)],
                               max_requests=50)
    with pytest.raises(AzbitAPIError) as exc:
        client.ohlc_range("BTC_USDT", "minutes5", START, END)
    assert "stalled" in str(exc.value)
    assert len(client.session.calls) == 2


def test_max_requests_bound_raises_not_truncates():
    far_end = START + 10_000 * 300  # needs 10 full pages; bound is 3
    pages = [FakeResponse(200, azbit_ohlc_rows(START + i * 1000 * 300, 1000, 300))
             for i in range(5)]
    client = make_azbit_client(pages, max_requests=3)
    with pytest.raises(AzbitAPIError) as exc:
        client.ohlc_range("BTC_USDT", "minutes5", START, far_end)
    assert "max_requests" in str(exc.value)
    assert len(client.session.calls) == 3  # bounded, then loud


def test_out_of_order_pages_sorted_ascending():
    rows = azbit_ohlc_rows(START, 5, 300)
    rows.reverse()
    client = make_azbit_client([FakeResponse(200, rows)])
    out = client.ohlc_range("BTC_USDT", "minutes5", START, END)
    assert [c.ts for c in out] == sorted(c.ts for c in out)


def test_invalid_range_rejected():
    client = make_azbit_client([])
    with pytest.raises(Exception):
        client.ohlc_range("BTC_USDT", "minutes5", END, START)
    assert client.session.calls == []
