"""Live integration tests against the REAL Nobitex public API.

SKIPPED by default in CI/sandboxes (no external network there). Run them on a
machine with internet access:

    python -m pytest tests/test_live_api.py -m live -q

They only use PUBLIC, read-only endpoints. No keys are ever sent.
"""
from __future__ import annotations

import os
import time

import pytest

from nobitex_adapter.nobitex_client import NobitexClient, NobitexError
from nobitex_adapter.symbols import quote_of
from nobitex_adapter.timeframes import from_nobitex_resolution

pytestmark = pytest.mark.live

pytestmark = pytest.mark.live


@pytest.fixture(autouse=True)
def _skip_when_mock_or_offline():
    base = os.environ.get("NOBITEX_API_BASE", "")
    if base.startswith(("http://127.0.0.1", "http://localhost")):
        pytest.skip(f"NOBITEX_API_BASE points at a local mock ({base})")


@pytest.fixture(scope="module")
def client():
    c = NobitexClient(timeout=20)
    yield c
    c.close()


def test_live_market_stats(client):
    """Discovery: /market/stats returns documented fields for real markets."""
    markets = client.discover_markets(quote="USDT")
    assert len(markets) >= 20
    syms = {m.ft_symbol for m in markets}
    # the flagship pairs must exist on Nobitex
    for expected in ("BTC/USDT", "ETH/USDT"):
        assert expected in syms, f"{expected} missing from live discovery"
    btc = next(m for m in markets if m.ft_symbol == "BTC/USDT")
    assert btc.quote == "USDT"
    assert btc.price > 0
    # normalization invariants on every discovered market
    for m in markets:
        assert quote_of(m.symbol) == m.quote
        assert m.ft_symbol == f"{m.base}/{m.quote}"


def test_live_candles_shape_and_spacing(client):
    """OHLCV: a real 1-day window of 5m candles has valid shape/spacing."""
    now = int(time.time())
    start = now - 24 * 3600
    candles = client.candles_range("BTCUSDT", "5", start, now)
    assert len(candles) >= 200  # ~288 minus the open candle and any gaps
    ts = [c.ts for c in candles]
    assert ts == sorted(ts)
    assert len(set(ts)) == len(ts)
    # 5m spacing dominates (real markets can have rare empty periods)
    gaps = [b - a for a, b in zip(ts, ts[1:])]
    assert sum(1 for g in gaps if g == 300) / len(gaps) > 0.95
    for c in candles:
        assert c.high >= max(c.open, c.close)
        assert c.low <= min(c.open, c.close)
        assert c.low > 0
        assert c.volume >= 0


def test_live_minute_history_limit(client):
    """Documented limitation: minute candles before ~2022-03-20 => no_data."""
    from nobitex_adapter.nobitex_client import NobitexNoData

    with pytest.raises(NobitexNoData):
        client.candles_page("BTCUSDT", "5", 1_500_000_000, 1_500_086_400, page=1)


def test_live_page_pagination(client):
    """A wide range paginates (multiple pages of <=500 candles)."""
    now = int(time.time())
    start = now - 3 * 86400
    n = 0
    page = 1
    pages_seen = 0
    while page <= 10:
        try:
            batch = client.candles_page("BTCUSDT", "5", start, now, page=page)
        except NobitexError:
            break
        pages_seen += 1
        n += len(batch)
        if len(batch) < 500:
            break
        page += 1
    assert n > 500
    assert pages_seen >= 2  # 3 days of 5m = ~864 candles -> at least 2 pages


def test_live_resolutions_documented_set(client):
    """Every documented resolution maps back to a freqtrade timeframe."""
    for res in ("1", "5", "15", "30", "60", "240", "D"):
        tf = from_nobitex_resolution(res)  # must not raise
        assert tf
