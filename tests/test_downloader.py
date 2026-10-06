"""Unit tests: downloader chunking, filenames, dedup/merge, resume,
incomplete-last-candle handling."""
from __future__ import annotations

import json
import threading
from datetime import datetime, timezone
from unittest import mock

import pandas as pd
import pytest

from conftest import make_df
from nobitex_adapter.downloader import (
    CANDLES_PER_PAGE,
    DownloadRequest,
    Downloader,
    data_filename,
    pair_to_filename,
)
from nobitex_adapter.nobitex_client import Candle, NobitexError, NobitexNoData


# ------------------------------------------------------------------ helpers
def _dt(s: str) -> datetime:
    return datetime.strptime(s, "%Y-%m-%d").replace(tzinfo=timezone.utc)


def _candles(start_ts: int, n: int, interval: int = 300, price: float = 100.0):
    return [Candle(ts=start_ts + i * interval, open=price, high=price * 1.01,
                   low=price * 0.99, close=price * 1.005, volume=5.0) for i in range(n)]


class FakeClient:
    """Stub client with real page semantics over a deterministic candle set.

    Mirrors the documented API: `candles_page` returns one <=500-candle page
    of [start_ts, end_ts) and raises NobitexNoData when the window has no
    candles (the documented `s: no_data` answer).
    """

    def __init__(self, candles: list[Candle]):
        self._candles = sorted(candles, key=lambda c: c.ts)
        self.calls = 0

    def candles_page(self, symbol, resolution, start_ts, end_ts, page=1, **kw):
        self.calls += 1
        sel = [c for c in self._candles if start_ts <= c.ts < end_ts]
        chunk = sel[(page - 1) * CANDLES_PER_PAGE : page * CANDLES_PER_PAGE]
        if not chunk:
            raise NobitexNoData("no data", code="NoData")
        return chunk

    def candles_range(self, symbol, resolution, start_ts, end_ts, **kw):
        out: list[Candle] = []
        page = 1
        while True:
            try:
                batch = self.candles_page(symbol, resolution, start_ts, end_ts, page)
            except NobitexNoData:
                break
            out.extend(batch)
            if len(batch) < CANDLES_PER_PAGE:
                break
            page += 1
        return out


def _downloader(tmp_path, client, events=None, stop_event=None) -> Downloader:
    return Downloader(
        client,
        datadir=tmp_path / "data",
        manifest_dir=tmp_path / "manifests",
        report_dir=tmp_path / "reports",
        progress_cb=(lambda ev: events.append(ev)) if events is not None else None,
        stop_event=stop_event,
    )


# ------------------------------------------------------------------ files
def test_pair_to_filename():
    assert pair_to_filename("BTC/USDT") == "BTC_USDT"
    assert pair_to_filename("1INCH/USDT") == "1INCH_USDT"


def test_data_filename_layout():
    p = data_filename("/tmp/dd", "nobitex", "BTC/USDT", "5m")
    assert p.as_posix() == "/tmp/dd/nobitex/BTC_USDT-5m.feather"


def test_request_validate_rejects_bad_input():
    with pytest.raises(ValueError):
        DownloadRequest(pairs=[], timeframes=["5m"],
                        start=_dt("2024-01-01"), end=_dt("2024-01-02")).validate()
    with pytest.raises(ValueError):
        DownloadRequest(pairs=["BTC/USDT"], timeframes=[],
                        start=_dt("2024-01-01"), end=_dt("2024-01-02")).validate()
    with pytest.raises(ValueError):
        DownloadRequest(pairs=["BTC/USDT"], timeframes=["5m"],
                        start=_dt("2024-01-02"), end=_dt("2024-01-01")).validate()


# ------------------------------------------------------------------ chunks
def test_chunks_cover_range():
    start, end = 1_700_000_000, 1_700_000_000 + 100 * 300
    chunks = list(Downloader._chunks(start, end, chunk_candles=10, interval=300))
    assert chunks[0][0] == start
    assert chunks[-1][1] == end
    # contiguous, non-overlapping
    for (a0, a1), (b0, b1) in zip(chunks, chunks[1:]):
        assert a1 == b0
    assert len(chunks) == 10


def test_chunks_capped_at_365_days():
    # 5000 candles of 1d would be 5000 days; cap at 365
    start = 1_500_000_000
    chunks = list(Downloader._chunks(start, start + 5000 * 86400,
                                     chunk_candles=5000, interval=86400))
    assert all(b - a <= 365 * 86400 for a, b in chunks)
    assert len(chunks) >= 2


# ------------------------------------------------------------------ download
def _preseed(tmp_path, start_ts: int, n: int, interval: int = 300):
    """Write an existing feather (as a prior download would have)."""
    path = data_filename(tmp_path / "data", "nobitex", "BTC/USDT", "5m")
    df = make_df(start_ts, n, interval)
    path.parent.mkdir(parents=True, exist_ok=True)
    out = df.copy()
    out["date"] = pd.to_datetime(out["date"].astype("int64"), unit="s").dt.tz_localize("UTC")
    out.to_feather(path)
    return path


def test_download_writes_sorted_deduped_feather(tmp_path):
    """Merge with an existing file must dedupe overlapping candles and stay
    strictly chronological."""
    start = _dt("2024-01-01")
    end = _dt("2024-01-02")
    start_ts = int(start.timestamp())
    _preseed(tmp_path, start_ts, 50)  # existing: candles 0..49
    client = FakeClient(_candles(start_ts + 30 * 300, 71, 300))  # new: 30..100 (overlap 30..49)
    dl = _downloader(tmp_path, client)
    summary = dl.download(DownloadRequest(
        pairs=["BTC/USDT"], timeframes=["5m"], start=start, end=end,
        startup_candles={"5m": 0}, chunk_candles=2000,
        drop_incomplete_last=False, end_is_open=False,
    ))
    task = summary.tasks[0]
    assert task.status == "DONE"
    assert task.duplicates_removed == 20  # 30..49 appear in both
    path = data_filename(tmp_path / "data", "nobitex", "BTC/USDT", "5m")
    assert path.is_file()
    df = pd.read_feather(path)
    assert list(df.columns) == ["date", "open", "high", "low", "close", "volume"]
    ts = df["date"].astype("int64") // 10**9
    assert (ts.diff().dropna() > 0).all()  # strictly chronological
    assert len(df) == 101  # 0..100
    # freqtrade 2026.x layout: tz-aware UTC
    assert str(df["date"].dtype) == "datetime64[ns, UTC]"


def test_download_drops_incomplete_last_candle(tmp_path):
    """A candle still open at `end` (left by a previous partial run) is
    dropped when drop_incomplete_last + end_is_open are set."""
    start = _dt("2024-01-01")
    end = _dt("2024-01-02")
    start_ts = int(start.timestamp())
    end_ts = int(end.timestamp())
    # pre-existing file contains the open candle at exactly `end_ts`
    _preseed(tmp_path, start_ts, 289)  # candles 0..288; candle 288 opens at end_ts
    client = FakeClient(_candles(start_ts, 289, 300))
    dl = _downloader(tmp_path, client)
    dl.download(DownloadRequest(
        pairs=["BTC/USDT"], timeframes=["5m"], start=start, end=end,
        startup_candles={"5m": 0}, chunk_candles=2000,
        drop_incomplete_last=True, end_is_open=True,
    ))
    df = pd.read_feather(data_filename(tmp_path / "data", "nobitex", "BTC/USDT", "5m"))
    last_ts = int(df["date"].iloc[-1].timestamp())
    assert last_ts + 300 <= end_ts  # last stored candle is fully closed
    assert len(df) == 288


def test_download_emits_progress_events(tmp_path):
    start, end = _dt("2024-01-01"), _dt("2024-01-02")
    events: list[dict] = []
    client = FakeClient(_candles(int(start.timestamp()), 20, 300))
    dl = _downloader(tmp_path, client, events=events)
    dl.download(DownloadRequest(
        pairs=["BTC/USDT"], timeframes=["5m"], start=start, end=end,
        startup_candles={"5m": 0}, chunk_candles=2000,
    ))
    kinds = {e["event"] for e in events}
    assert "job_start" in kinds
    assert "task_start" in kinds
    assert "task_done" in kinds
    assert "job_done" in kinds
    assert any(e["event"] == "page" for e in events)


def test_download_resume_skips_covered_chunks(tmp_path):
    start, end = _dt("2024-01-01"), _dt("2024-01-02")
    start_ts = int(start.timestamp())
    candles = _candles(start_ts, 100, 300)

    # first pass
    c1 = FakeClient(candles)
    s1 = _downloader(tmp_path, c1).download(DownloadRequest(
        pairs=["BTC/USDT"], timeframes=["5m"], start=start, end=end,
        startup_candles={"5m": 0}, chunk_candles=2000,
        drop_incomplete_last=False, end_is_open=False,
    ))
    assert s1.tasks[0].chunks_done >= 1

    # second pass: same range; all chunks already covered -> no new HTTP
    c2 = FakeClient(candles)
    s2 = _downloader(tmp_path, c2).download(DownloadRequest(
        pairs=["BTC/USDT"], timeframes=["5m"], start=start, end=end,
        startup_candles={"5m": 0}, chunk_candles=2000,
        drop_incomplete_last=False, end_is_open=False,
    ))
    task = s2.tasks[0]
    assert task.chunks_skipped >= 1
    assert c2.calls == 0  # nothing fetched
    assert task.new_rows == 0
    assert task.rows == s1.tasks[0].rows  # file unchanged in size


def test_force_ignores_manifest(tmp_path):
    start, end = _dt("2024-01-01"), _dt("2024-01-02")
    start_ts = int(start.timestamp())
    candles = _candles(start_ts, 100, 300)
    c1 = FakeClient(candles)
    _downloader(tmp_path, c1).download(DownloadRequest(
        pairs=["BTC/USDT"], timeframes=["5m"], start=start, end=end,
        startup_candles={"5m": 0}, chunk_candles=2000,
        drop_incomplete_last=False, end_is_open=False,
    ))
    c2 = FakeClient(candles)
    s2 = _downloader(tmp_path, c2).download(DownloadRequest(
        pairs=["BTC/USDT"], timeframes=["5m"], start=start, end=end,
        startup_candles={"5m": 0}, chunk_candles=2000,
        drop_incomplete_last=False, end_is_open=False, force=True,
    ))
    assert c2.calls > 0  # re-fetched despite manifest
    assert s2.tasks[0].chunks_skipped == 0


def test_no_data_range_is_a_hard_failure_with_diagnostics(tmp_path):
    """Zero candles must NEVER be a silent success: the task is ERROR, the
    summary is not ok, and the error carries the exact empty requests seen."""
    start, end = _dt("2024-01-01"), _dt("2024-01-02")

    class NoDataClient:
        def candles_page(self, *a, **kw):
            raise NobitexNoData("no data", code="NoData")

    dl = _downloader(tmp_path, NoDataClient())
    summary = dl.download(DownloadRequest(
        pairs=["BTC/USDT"], timeframes=["5m"], start=start, end=end,
        startup_candles={"5m": 0}, chunk_candles=2000,
        drop_incomplete_last=False, end_is_open=False,
    ))
    task = summary.tasks[0]
    assert task.status == "ERROR"
    assert not summary.ok
    assert "ZERO candles" in task.error
    # the diagnostic shows what was actually requested
    assert "no_data" in task.error
    assert "res=5" in task.error  # 5m -> Nobitex resolution "5"
    # and it points at the raw probe command
    assert "ohlcv-probe" in task.error


def test_cancel_stops_download(tmp_path):
    start, end = _dt("2024-01-01"), _dt("2024-06-01")
    stop = threading.Event()
    stop.set()
    client = FakeClient([])
    dl = _downloader(tmp_path, client, stop_event=stop)
    with pytest.raises(NobitexError) as exc:
        dl.download(DownloadRequest(
            pairs=["BTC/USDT"], timeframes=["5m"], start=start, end=end,
            startup_candles={"5m": 0}, chunk_candles=100,
        ))
    assert "cancelled" in str(exc.value)
    assert client.calls == 0


def test_validation_report_saved_per_dataset(tmp_path):
    start, end = _dt("2024-01-01"), _dt("2024-01-02")
    client = FakeClient(_candles(int(start.timestamp()), 100, 300))
    dl = _downloader(tmp_path, client)
    dl.download(DownloadRequest(
        pairs=["BTC/USDT"], timeframes=["5m"], start=start, end=end,
        startup_candles={"5m": 0}, chunk_candles=2000,
        drop_incomplete_last=False, end_is_open=False,
    ))
    reports = list((tmp_path / "reports").rglob("*BTC_USDT*"))
    assert reports, "expected a validation report file"
    content = reports[0].read_text(encoding="utf-8")
    assert "BTC/USDT" in content


# ------------------------------------------------- adaptive narrow windows
class WideRefusingClient:
    """Simulates an API that answers no_data for ranges wider than 10
    candles (5m) but answers narrow windows correctly — i.e. an
    undocumented per-request range limit. The downloader must still fetch
    everything via adaptive narrowing."""

    def __init__(self, candles: list[Candle]):
        self._candles = sorted(candles, key=lambda c: c.ts)
        self.calls = 0

    def candles_page(self, symbol, resolution, start_ts, end_ts, page=1, **kw):
        self.calls += 1
        interval = 300  # 5m
        if end_ts - start_ts > 10 * interval + interval:
            raise NobitexNoData("no data (range too wide)", code="NoData")
        sel = [c for c in self._candles if start_ts <= c.ts < end_ts]
        if not sel:
            raise NobitexNoData("no data", code="NoData")
        return sel


def test_adaptive_narrowing_beats_wide_range_limit(tmp_path):
    """All candles must arrive even though only <=10-candle windows are
    answered (the 5m/15m-zero-candles defense)."""
    start, end = _dt("2024-01-01"), _dt("2024-01-02")  # 288 candles of 5m
    start_ts = int(start.timestamp())
    client = WideRefusingClient(_candles(start_ts, 288, 300))
    dl = _downloader(tmp_path, client)
    summary = dl.download(DownloadRequest(
        pairs=["BTC/USDT"], timeframes=["5m"], start=start, end=end,
        startup_candles={"5m": 0}, chunk_candles=2000,
        drop_incomplete_last=False, end_is_open=False,
    ))
    task = summary.tasks[0]
    assert task.status == "DONE", task.error
    assert task.rows == 288
    assert summary.ok
    # narrowing actually happened (far more requests than 1-2 pages)
    assert client.calls > 30


def test_empty_region_not_crawled_candle_by_candle(tmp_path):
    """When a region is truly empty (no candles at all), the jump-to-full-
    window logic must keep the request count low (no 10-candle crawling)."""
    start, end = _dt("2024-01-01"), _dt("2024-01-08")  # 7 days, all empty

    class AlwaysNoData:
        def __init__(self):
            self.calls = 0

        def candles_page(self, symbol, resolution, start_ts, end_ts, page=1, **kw):
            self.calls += 1
            raise NobitexNoData("no data", code="NoData")

    client = AlwaysNoData()
    dl = _downloader(tmp_path, client)
    summary = dl.download(DownloadRequest(
        pairs=["BTC/USDT"], timeframes=["5m"], start=start, end=end,
        startup_candles={"5m": 0}, chunk_candles=2000,
        drop_incomplete_last=False, end_is_open=False,
    ))
    assert summary.tasks[0].status == "ERROR"
    assert not summary.ok
    # 7 days of 5m = 2016 candles = ~5 full-width windows; each empty window
    # costs the bounded narrowing ladder (~7 no_data requests) then jumps to
    # the full window end. Crawling 10 candles at a time would need 200+.
    assert client.calls < 60, client.calls
    assert client.calls < 2016 // 10 // 4  # far below per-candle-group crawling
