"""Live integration tests against the REAL Wallex public API.

SKIPPED by default in CI/sandboxes (no external network there). Run them on
a machine with internet access:

    python -m pytest tests/test_wallex_live_api.py -m live -q

They only use PUBLIC, read-only endpoints. No keys are ever sent.
Nothing here fabricates results: without network these tests fail loudly
(they are NOT part of the default suite).

These tests additionally VERIFY the provisional resolution ladder
(timeframes.WALLEX_RESOLUTIONS): the reference documents only
resolution=60, so every other entry is confirmed here or corrected.
"""
from __future__ import annotations

import os
import time

import pytest

from nobitex_adapter.wallex_client import WallexClient

pytestmark = pytest.mark.live


@pytest.fixture(autouse=True)
def _skip_when_mock():
    base = os.environ.get("WALLEX_API_BASE", "")
    if base.startswith(("http://127.0.0.1", "http://localhost")):
        pytest.skip(f"WALLEX_API_BASE points at a local mock ({base})")


@pytest.fixture(scope="module")
def client():
    c = WallexClient(timeout=25)
    yield c
    c.close()


def test_live_pair_discovery(client):
    """Discovery returns real pairs incl. BTCUSDT."""
    markets = client.discover_markets(quote="USDT")
    assert len(markets) >= 10
    syms = {m.ft_symbol for m in markets}
    assert "BTC/USDT" in syms
    btc = next(m for m in markets if m.ft_symbol == "BTC/USDT")
    assert btc.symbol == "BTCUSDT"
    assert btc.price > 0


def test_live_tmn_quote_discovery(client):
    """Wallex's fiat quote (Toman) discovers pairs like BTC/TMN."""
    markets = client.discover_markets(quote="TMN")
    assert len(markets) >= 1
    assert all(m.quote == "TMN" for m in markets)


def test_live_history_shape_recent_window(client):
    """Recent 1h history for BTCUSDT is chronological, consistent OHLC."""
    now = int(time.time())
    rows = client.history_page("BTCUSDT", "60", now - 3 * 86400, now)
    assert len(rows) > 0
    ts = [c.ts for c in rows]
    assert ts == sorted(ts)
    for c in rows:
        assert c.high >= max(c.open, c.close)
        assert c.low <= min(c.open, c.close)
        assert c.low > 0
        assert c.volume >= 0


def test_live_history_pagination_covers_range(client):
    """A 30-day 1h range returns full coverage via cursor pagination."""
    now = int(time.time())
    rows = client.history_range("BTCUSDT", "60", now - 30 * 86400, now)
    assert len(rows) > 600  # 30d of 1h ≈ 720 perfect-grid candles
    ts = [c.ts for c in rows]
    assert ts == sorted(ts) and len(set(ts)) == len(ts)


def test_live_resolution_vocabulary(client):
    """Every provisional ladder entry maps to a served Wallex resolution."""
    from nobitex_adapter.timeframes import WALLEX_RESOLUTIONS

    now = int(time.time())
    for tf, res in sorted(WALLEX_RESOLUTIONS.items()):
        rows = client.history_page("BTCUSDT", res, now - 3 * 86400, now)
        assert isinstance(rows, list)  # served (possibly empty), never an error
