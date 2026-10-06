"""Integration tests: real HTTP against the in-repo mock Nobitex API.

These exercise the full client+downloader stack over sockets (pagination,
rate limits, 429 backoff, resume, validation, cancellation).
"""
from __future__ import annotations

import threading
from datetime import datetime, timezone

import pandas as pd
import pytest
import requests

from nobitex_adapter.downloader import DownloadRequest, Downloader, data_filename
from nobitex_adapter.nobitex_client import NobitexClient, NobitexError

pytestmark = pytest.mark.integration


def _dt(s: str) -> datetime:
    return datetime.strptime(s, "%Y-%m-%d").replace(tzinfo=timezone.utc)


@pytest.fixture
def client(mock_server_url) -> NobitexClient:
    c = NobitexClient(base_url=mock_server_url)
    yield c
    c.close()


# ------------------------------------------------------------------ discovery
def test_discover_markets_over_http(client):
    markets = client.discover_markets(quote="USDT")
    assert len(markets) > 30
    syms = {m.ft_symbol for m in markets}
    assert "BTC/USDT" in syms
    assert "ETH/USDT" in syms
    # closed markets are flagged inactive
    sushi = next(m for m in markets if m.ft_symbol == "SUSHI/USDT")
    assert sushi.active is False
    # deterministic ordering
    assert [m.symbol for m in markets] == sorted(m.symbol for m in markets)


def test_discover_usdt_only(client):
    markets = client.discover_markets(quote="USDT")
    assert all(m.quote == "USDT" for m in markets)


def test_candles_range_over_http(client):
    out = client.candles_range("BTCUSDT", "5", 1_700_000_000, 1_700_000_000 + 1200 * 300)
    assert 1100 <= len(out) <= 1200
    ts = [c.ts for c in out]
    assert ts == sorted(ts)
    assert len(set(ts)) == len(ts)  # no duplicates
    for c in out:
        assert c.high >= max(c.open, c.close)
        assert c.low <= min(c.open, c.close)


# ------------------------------------------------------------------ download
def test_download_resume_and_validate(tmp_path, client):
    start, end = _dt("2024-06-01"), _dt("2024-06-04")
    events: list[dict] = []

    def dl() -> Downloader:
        return Downloader(
            client, datadir=tmp_path / "data", manifest_dir=tmp_path / "manifests",
            report_dir=tmp_path / "reports", progress_cb=lambda ev: events.append(ev),
        )

    req = DownloadRequest(
        pairs=["BTC/USDT"], timeframes=["5m"], start=start, end=end,
        startup_candles={"5m": 50}, chunk_candles=500,
        drop_incomplete_last=False, end_is_open=False,
    )
    s1 = dl().download(req)
    t1 = s1.tasks[0]
    assert t1.status == "DONE"
    assert t1.rows > 800  # 3 days * 288 + 50 startup
    assert s1.ok
    path = data_filename(tmp_path / "data", "nobitex", "BTC/USDT", "5m")
    df1 = pd.read_feather(path)
    assert str(df1["date"].dtype) == "datetime64[ns, UTC]"
    assert t1.validation is not None and t1.validation.status == "PASS"

    # second pass: everything covered by manifest -> zero new rows, no refetch
    events.clear()
    s2 = dl().download(req)
    t2 = s2.tasks[0]
    assert t2.status == "DONE"
    assert t2.new_rows == 0
    assert t2.rows == t1.rows
    assert t2.chunks_skipped == t1.chunks_done + t1.chunks_skipped
    assert not any(e["event"] == "page" for e in events)  # nothing fetched

    # explicit dataset validation (the `validate` command path)
    reports = dl().validate_dataset(
        exchange="nobitex", pairs=["BTC/USDT"], timeframes=["5m"],
        start=start, end=end, end_is_open=False,
    )
    assert len(reports) == 1
    assert reports[0].status == "PASS"
    assert reports[0].rows >= 800


def test_download_multiple_timeframes(tmp_path, client):
    start, end = _dt("2024-06-01"), _dt("2024-06-03")
    summary = Downloader(
        client, datadir=tmp_path / "data", manifest_dir=tmp_path / "manifests",
        report_dir=tmp_path / "reports",
    ).download(DownloadRequest(
        pairs=["BTC/USDT", "ETH/USDT"], timeframes=["5m", "1h"],
        start=start, end=end,
        startup_candles={"5m": 20, "1h": 20}, chunk_candles=500,
        drop_incomplete_last=False, end_is_open=False,
    ))
    assert len(summary.tasks) == 4
    assert summary.ok
    # 2-day window + 20-candle warmup: 5m ~596 rows, 1h ~68 rows
    for pair in ("BTC/USDT", "ETH/USDT"):
        df5 = pd.read_feather(data_filename(tmp_path / "data", "nobitex", pair, "5m"))
        df1 = pd.read_feather(data_filename(tmp_path / "data", "nobitex", pair, "1h"))
        assert len(df5) > 500
        assert len(df1) > 40


# ------------------------------------------------------------------ 429 backoff
class _RLimitSession(requests.Session):
    """Inject the mock-only `rlimit` param so the mock enforces 429s."""

    def __init__(self, rlimit: int):
        super().__init__()
        self._rlimit = rlimit

    def get(self, url, params=None, **kw):
        if "/market/udf/history" in url:
            params = dict(params or {})
            params["rlimit"] = self._rlimit
        return super().get(url, params=params, **kw)


def test_download_recovers_from_429(tmp_path, mock_server_url):
    client = NobitexClient(base_url=mock_server_url,
                           session=_RLimitSession(rlimit=1),  # bucket after 10 reqs
                           max_retries=10)
    try:
        # 20 days of 5m -> ~12 page requests; bucket(rlimit=1) trips after 10,
        # so the 429s (backOff=2) must be ridden out by the client
        start, end = _dt("2024-06-01"), _dt("2024-06-21")
        summary = Downloader(
            client, datadir=tmp_path / "data", manifest_dir=tmp_path / "manifests",
            report_dir=tmp_path / "reports",
        ).download(DownloadRequest(
            pairs=["BTC/USDT"], timeframes=["5m"], start=start, end=end,
            startup_candles={"5m": 20}, chunk_candles=1000,
            drop_incomplete_last=False, end_is_open=False,
        ))
        assert summary.ok
        assert summary.tasks[0].status == "DONE"
        assert summary.tasks[0].rows > 5000
    finally:
        client.close()


# ------------------------------------------------------------------ cancel
def test_cancel_mid_download(tmp_path, mock_server_url):
    client = NobitexClient(base_url=mock_server_url)
    stop = threading.Event()

    def on_progress(ev: dict) -> None:
        if ev.get("event") == "chunk_done":
            stop.set()  # cancel after the first chunk completes

    try:
        dl = Downloader(
            client, datadir=tmp_path / "data", manifest_dir=tmp_path / "manifests",
            report_dir=tmp_path / "reports", progress_cb=on_progress, stop_event=stop,
        )
        start, end = _dt("2024-01-01"), _dt("2024-03-01")  # ~2 months of 5m
        with pytest.raises(NobitexError) as exc:
            dl.download(DownloadRequest(
                pairs=["BTC/USDT"], timeframes=["5m"], start=start, end=end,
                startup_candles={"5m": 20}, chunk_candles=100,
                drop_incomplete_last=False, end_is_open=False,
            ))
        assert "cancelled" in str(exc.value)
    finally:
        client.close()


# ------------------------------------------------------------------ rate limiter
def test_rate_limiter_paces_requests():
    import time

    from nobitex_adapter.nobitex_client import _RateLimiter

    lim = _RateLimiter({"/x": 20.0})  # 50ms interval
    t0 = time.monotonic()
    for _ in range(3):
        lim.wait("/x")
    elapsed = time.monotonic() - t0
    assert 0.08 <= elapsed <= 1.0  # two intervals of ~50ms
