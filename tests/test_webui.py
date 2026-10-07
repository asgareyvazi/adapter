"""End-to-end tests for the Backtest Manager GUI API.

Drives the FastAPI app exactly the way the browser does: discover ->
download -> validate -> results/history, including job polling, log tails
and cancellation. Uses the in-repo mock Nobitex API for market data and an
isolated tmp repo layout (never touches the real user_data).
"""
from __future__ import annotations

import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

pytestmark = pytest.mark.integration

TINY_STRATEGY = '''
from freqtrade.strategy import IStrategy


class TinyStrategy(IStrategy):
    """Minimal strategy for GUI-level backtest plumbing tests."""

    timeframe = "5m"
    can_short = False
    minimal_roi = {"0": 0.01}
    stoploss = -0.05
    startup_candle_count = 10

    def populate_indicators(self, dataframe, metadata):
        dataframe["sma"] = dataframe["close"].rolling(5).mean()
        return dataframe

    def populate_entry_trend(self, dataframe, metadata):
        dataframe.loc[
            (dataframe["sma"] > 0) & (dataframe["volume"] > 0), "enter_long"] = 1
        return dataframe

    def populate_exit_trend(self, dataframe, metadata):
        dataframe.loc[dataframe["sma"].shift(1) > dataframe["sma"], "exit_long"] = 1
        return dataframe
'''


@pytest.fixture
def gui(mock_server_url, tmp_repo, monkeypatch):
    """App + TestClient on an isolated repo root, pointed at the mock API."""
    monkeypatch.setenv("NOBITEX_API_BASE", mock_server_url)
    # isolate the market cache the discover job writes
    cache = tmp_repo / "user_data" / "nobitex_gui" / "markets_cache.json"
    monkeypatch.setenv("NOBITEX_ADAPTER_MARKETS_CACHE", str(cache))

    from nobitex_adapter.webui.app import create_app

    app = create_app(root=tmp_repo)
    with TestClient(app) as client:
        yield client


def _wait_job(client: TestClient, job_id: str, *, timeout: float = 180.0) -> dict:
    deadline = time.time() + timeout
    last: dict = {}
    while time.time() < deadline:
        last = client.get(f"/api/jobs/{job_id}").json()
        if last["status"] != "running":
            return last
        time.sleep(1.0)
    raise AssertionError(f"job {job_id} still running: {last}")


# ------------------------------------------------------------------ basics
def test_status(gui):
    r = gui.get("/api/status")
    assert r.status_code == 200
    body = r.json()
    assert body["app"] == "nobitex-adapter"
    assert body["exchange"] == "nobitex"
    assert body["futures"] is False
    assert "NO REAL ORDERS" in body["mode"]
    assert "http" not in str(body)  # never leak URLs to the client


def test_strategies_discovered(gui):
    r = gui.get("/api/strategies")
    assert r.status_code == 200
    names = [s["name"] for s in r.json()["strategies"]]
    assert "TinyStrategy" in names
    tiny = next(s for s in r.json()["strategies"] if s["name"] == "TinyStrategy")
    assert tiny["timeframe"] == "5m"


def test_exchanges_spot_only(gui):
    r = gui.get("/api/exchanges")
    ex = r.json()["exchanges"]
    nobitex = next(e for e in ex if e["id"] == "nobitex")
    assert nobitex["status"] == "ready"
    assert nobitex["spot"] is True
    assert nobitex["futures"] is False
    azbit = next(e for e in ex if e["id"] == "azbit")
    assert azbit["status"] == "ready"
    assert azbit["spot"] is True
    assert azbit["futures"] is False
    for e in ex:
        if e["id"] not in ("nobitex", "azbit"):
            assert e["status"] == "planned"  # others disabled in the UI


def test_presets(gui):
    r = gui.get("/api/presets")
    assert r.status_code == 200
    presets = r.json()["presets"]
    assert set(presets) == {"1M", "3M", "6M", "1Y", "2Y", "3Y"}
    for p in presets.values():
        assert p["start"] < p["end"]


def test_static_index_served(gui):
    r = gui.get("/")
    assert r.status_code == 200
    assert "text/html" in r.headers["content-type"]
    assert "Backtest Manager" in r.text


# ------------------------------------------------------------------ workflow
def test_full_workflow_discover_download_validate(gui):
    # 1) discover
    r = gui.post("/api/jobs", json={"kind": "discover", "params": {"quote": "USDT"}})
    assert r.status_code == 200
    job = _wait_job(gui, r.json()["job_id"])
    assert job["status"] == "done", job.get("error")
    assert len(job["result"]["markets"]) > 30

    # markets are now served from the cache
    markets = gui.get("/api/markets").json()["markets"]
    assert any(m["ft_symbol"] == "BTC/USDT" for m in markets)

    # 2) download (small controlled range)
    r = gui.post("/api/jobs", json={
        "kind": "download",
        "params": {"pairs": "BTC/USDT", "timeframes": ["5m", "1h"],
                   "start": "2024-06-01", "end": "2024-06-08"},
    })
    assert r.status_code == 200
    job = _wait_job(gui, r.json()["job_id"], timeout=300)
    assert job["status"] == "done", job.get("error")
    assert job["result"]["all_valid"] is True
    # 1 week: 5m ~2866 rows + 1h ~568 rows (incl. startup lead-in)
    assert job["result"]["total_rows"] > 3000

    # 3) validate
    r = gui.post("/api/jobs", json={
        "kind": "validate",
        "params": {"pairs": "BTC/USDT", "timeframes": ["5m", "1h"],
                   "start": "2024-06-01", "end": "2024-06-08"},
    })
    job = _wait_job(gui, r.json()["job_id"])
    assert job["status"] == "done", job.get("error")
    assert job["result"]["all_ok"] is True

    # 4) download history reflects the datasets
    items = gui.get("/api/data-history").json()["items"]
    assert any(i["pair"] == "BTC/USDT" and i["timeframe"] == "5m" for i in items)
    assert all("." not in i["timeframe"] for i in items)  # no '.json' leak


def test_job_log_tail_and_offset(gui):
    r = gui.post("/api/jobs", json={"kind": "discover", "params": {"quote": "USDT"}})
    job_id = r.json()["job_id"]
    _wait_job(gui, job_id)
    full = gui.get(f"/api/jobs/{job_id}/log").json()
    assert full["total"] > 0
    # offset: only the tail after N
    tail = gui.get(f"/api/jobs/{job_id}/log", params={"after": full["total"] - 2}).json()
    assert len(tail["lines"]) <= 2


def test_cancel_download_job(gui):
    r = gui.post("/api/jobs", json={
        "kind": "download",
        "params": {"pairs": "BTC/USDT", "timeframes": ["5m"],
                   "start": "2022-05-01", "end": "2025-12-31"},  # 3.5 years: long
    })
    job_id = r.json()["job_id"]
    # let it download a few chunks
    time.sleep(15)
    assert gui.get(f"/api/jobs/{job_id}").json()["status"] == "running"
    c = gui.post(f"/api/jobs/{job_id}/cancel")
    assert c.status_code == 200 and c.json()["cancelled"] is True
    job = _wait_job(gui, job_id, timeout=60)
    assert job["status"] == "cancelled"
    # double cancel is a no-op
    assert gui.post(f"/api/jobs/{job_id}/cancel").json()["cancelled"] is False


def test_unknown_job_404(gui):
    assert gui.get("/api/jobs/deadbeef").status_code == 404


def test_unknown_job_kind_400(gui):
    r = gui.post("/api/jobs", json={"kind": "warpdrive", "params": {}})
    assert r.status_code == 400


# ------------------------------------------------------------------ backtest via GUI job
def test_backtest_job_subprocess(tmp_path, mock_server_url, monkeypatch):
    """The GUI backtest runs the CLI in a subprocess; the zip must land in the
    app's own export dir and be served by /api/results."""
    repo = tmp_path / "repo"
    strategies = repo / "user_data" / "strategies"
    strategies.mkdir(parents=True)
    (strategies / "TinyStrategy.py").write_text(TINY_STRATEGY, encoding="utf-8")
    monkeypatch.setenv("NOBITEX_API_BASE", mock_server_url)
    monkeypatch.setenv("NOBITEX_ADAPTER_MARKETS_CACHE",
                       str(repo / "user_data" / "nobitex_gui" / "markets_cache.json"))

    from nobitex_adapter.downloader import DownloadRequest, Downloader
    from nobitex_adapter.nobitex_client import NobitexClient
    from nobitex_adapter.webui.app import create_app
    from datetime import datetime, timezone

    # download data for the backtest first (in-process, fast)
    client = NobitexClient(base_url=mock_server_url)
    Downloader(client, datadir=repo / "user_data" / "data",
               manifest_dir=repo / "user_data" / "manifests",
               report_dir=repo / "user_data" / "reports").download(DownloadRequest(
        pairs=["BTC/USDT"], timeframes=["5m"],
        start=datetime(2024, 6, 1, tzinfo=timezone.utc),
        end=datetime(2024, 6, 8, tzinfo=timezone.utc),
        startup_candles={"5m": 50}, chunk_candles=500,
        drop_incomplete_last=False, end_is_open=False,
    ))
    client.close()

    app = create_app(root=repo)
    with TestClient(app) as gui:
        r = gui.post("/api/jobs", json={
            "kind": "backtest",
            "params": {"strategy": "TinyStrategy", "pairs": "BTC/USDT",
                       "start": "2024-06-02", "end": "2024-06-06",
                       "capital": 10000, "stake": "unlimited"},
        })
        assert r.status_code == 200
        job = _wait_job(gui, r.json()["job_id"], timeout=600)
        assert job["status"] == "done", job.get("error")
        assert job["result"]["ok"] is True
        assert job["result"]["exit_code"] == 0
        assert Path(job["result"]["results_zip"]).is_file()

        # results are served
        zips = gui.get("/api/results/list").json()["results"]
        assert zips and zips[0]["name"] == Path(job["result"]["results_zip"]).name
        dash = gui.get("/api/results/latest").json()
        assert dash["strategy"] == "TinyStrategy"
        assert dash["source"].endswith(Path(job["result"]["results_zip"]).name)


def test_backtest_precheck_missing_data_reports_clear_error(tmp_path, mock_server_url, monkeypatch):
    """Backtest without downloaded data must fail with a helpful message."""
    repo = tmp_path / "repo"
    strategies = repo / "user_data" / "strategies"
    strategies.mkdir(parents=True)
    (strategies / "TinyStrategy.py").write_text(TINY_STRATEGY, encoding="utf-8")
    monkeypatch.setenv("NOBITEX_API_BASE", mock_server_url)
    monkeypatch.setenv("NOBITEX_ADAPTER_MARKETS_CACHE",
                       str(repo / "user_data" / "nobitex_gui" / "markets_cache.json"))
    from nobitex_adapter.webui.app import create_app

    app = create_app(root=repo)
    with TestClient(app) as gui:
        r = gui.post("/api/jobs", json={
            "kind": "backtest",
            "params": {"strategy": "TinyStrategy", "pairs": "SOL/USDT",
                       "start": "2024-06-02", "end": "2024-06-06"},
        })
        job_id = r.json()["job_id"]
        job = _wait_job(gui, job_id, timeout=120)
        assert job["status"] == "error"
        # the detailed reason is in the job log (CLI stderr)
        log = gui.get(f"/api/jobs/{job_id}/log").json()["lines"]
        text = "\n".join(log)
        assert "required data is missing" in text
        assert "SOL_USDT-5m.feather" in text


# ------------------------------------------------------------- _tfs helper
@pytest.mark.unit
def test_tfs_accepts_list():
    from nobitex_adapter.jobs import _tfs

    assert _tfs(["5m", "1h"]) == ["5m", "1h"]


@pytest.mark.unit
def test_tfs_accepts_comma_string():
    from nobitex_adapter.jobs import _tfs

    assert _tfs("5m,15m,1h") == ["5m", "15m", "1h"]
    assert _tfs("5m; 1h") == ["5m", "1h"]
    assert _tfs("5m") == ["5m"]  # the bug that would have given ['5','m']


@pytest.mark.unit
def test_tfs_rejects_bad_type():
    from nobitex_adapter.jobs import _tfs

    with pytest.raises(ValueError):
        _tfs(5)
