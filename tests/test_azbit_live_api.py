"""Live integration tests against the REAL AZBit public API.

SKIPPED by default in CI/sandboxes (no external network there — the Arena
sandbox cannot reach data.azbit.com). Run them on a machine with internet
access:

    python -m pytest tests/test_azbit_live_api.py -m live -q

They only use PUBLIC, read-only endpoints. No keys are ever sent.
Nothing here fabricates results: without network these tests fail loudly
(they are NOT part of the default suite).
"""
from __future__ import annotations

import os
import time

import pytest

from nobitex_adapter.azbit_client import AzbitClient

pytestmark = pytest.mark.live


@pytest.fixture(autouse=True)
def _skip_when_mock():
    base = os.environ.get("AZBIT_API_BASE", "")
    if base.startswith(("http://127.0.0.1", "http://localhost")):
        pytest.skip(f"AZBIT_API_BASE points at a local mock ({base})")


@pytest.fixture(scope="module")
def client():
    c = AzbitClient(timeout=25)
    yield c
    c.close()


def test_live_pair_discovery(client):
    """Discovery returns real pairs incl. BTC_USDT."""
    markets = client.discover_markets(quote="USDT")
    assert len(markets) >= 10
    syms = {m.ft_symbol for m in markets}
    assert "BTC/USDT" in syms
    btc = next(m for m in markets if m.ft_symbol == "BTC/USDT")
    assert btc.symbol == "BTC_USDT"
    assert btc.price > 0


def test_live_ohlc_shape_2024_window(client):
    """The mission's real observation, re-verified: June-2024 5m history
    exists for BTC_USDT (non-empty, chronological rows)."""
    start = int(time.mktime(time.strptime("2024-06-01", "%Y-%m-%d")))
    end = int(time.mktime(time.strptime("2024-06-02", "%Y-%m-%d")))
    rows = client.ohlc_page("BTC_USDT", "minutes5", start, end)
    assert len(rows) > 0
    ts = [c.ts for c in rows]
    assert ts == sorted(ts)
    for c in rows:
        assert c.high >= max(c.open, c.close)
        assert c.low <= min(c.open, c.close)
        assert c.low > 0
        assert c.volume >= 0


def test_live_ohlc_pagination_covers_range(client):
    """A 5-day 5m range returns MORE than one capped response can hold."""
    start = int(time.mktime(time.strptime("2024-06-01", "%Y-%m-%d")))
    end = int(time.mktime(time.strptime("2024-06-06", "%Y-%m-%d")))
    rows = client.ohlc_range("BTC_USDT", "minutes5", start, end)
    assert len(rows) > 1000  # 5d of 5m ≈ 1440 perfect-grid candles
    ts = [c.ts for c in rows]
    assert ts == sorted(ts) and len(set(ts)) == len(ts)


def test_live_interval_vocabulary(client):
    """Every X8 timeframe maps to a served AZBit interval."""
    from nobitex_adapter.timeframes import to_azbit_interval

    now = int(time.time())
    for tf in ("5m", "15m", "1h", "4h", "1d"):
        iv = to_azbit_interval(tf)
        rows = client.ohlc_page("BTC_USDT", iv, now - 3 * 86400, now)
        assert isinstance(rows, list)  # served (possibly empty), never an error
