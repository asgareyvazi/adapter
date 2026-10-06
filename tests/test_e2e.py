"""Full-pipeline E2E (small controlled range):

mock Nobitex API -> robust download (all X8 timeframes) -> strict validation
-> in-process Freqtrade backtest with the REAL NostalgiaForInfinityX8
-> results zip parsed into the dashboard payload.

The numbers are synthetic (mock data) but every stage is the production code
path used by the CLI/GUI.
"""
from __future__ import annotations

import shutil
from datetime import datetime, timezone
from pathlib import Path

import pytest

from nobitex_adapter.backtest import run_backtest
from nobitex_adapter.downloader import DownloadRequest, Downloader
from nobitex_adapter.nobitex_client import NobitexClient
from nobitex_adapter.results import parse_backtest_zip

pytestmark = pytest.mark.e2e

REPO_ROOT = Path(__file__).resolve().parent.parent
X8 = REPO_ROOT / "user_data" / "strategies" / "NostalgiaForInfinityX8.py"

START = datetime(2024, 6, 1, tzinfo=timezone.utc)
END = datetime(2024, 6, 15, tzinfo=timezone.utc)  # 14-day controlled window
PAIRS = ["BTC/USDT", "ETH/USDT"]
X8_TFS = ["5m", "15m", "1h", "4h", "1d"]


@pytest.fixture(scope="module")
def e2e_repo(tmp_path_factory, mock_server_url):
    """Isolated repo with the real X8 strategy + downloaded data for 2 pairs.

    NOBITEX_API_BASE is pointed at the mock for the whole module so the
    in-process Freqtrade backtest (which instantiates the Nobitex exchange
    via ccxt) also resolves to the mock.
    """
    import os

    old_base = os.environ.get("NOBITEX_API_BASE")
    os.environ["NOBITEX_API_BASE"] = mock_server_url
    repo = tmp_path_factory.mktemp("e2e") / "repo"
    strategies = repo / "user_data" / "strategies"
    strategies.mkdir(parents=True)
    assert X8.is_file(), "NostalgiaForInfinityX8.py must be in the repo user_data"
    shutil.copy(X8, strategies / "NostalgiaForInfinityX8.py")

    client = NobitexClient(base_url=mock_server_url)
    try:
        summary = Downloader(
            client,
            datadir=repo / "user_data" / "data",
            manifest_dir=repo / "user_data" / "manifests",
            report_dir=repo / "user_data" / "reports",
        ).download(DownloadRequest(
            pairs=PAIRS, timeframes=X8_TFS, start=START, end=END,
            drop_incomplete_last=False, end_is_open=False,
        ))
    finally:
        client.close()

    assert summary.ok, {t.pair: (t.status, t.error) for t in summary.tasks}
    for t in summary.tasks:
        assert t.validation is not None and t.validation.status == "PASS", \
            (t.pair, t.timeframe, t.validation.problems if t.validation else None)
    yield repo
    if old_base is None:
        os.environ.pop("NOBITEX_API_BASE", None)
    else:
        os.environ["NOBITEX_API_BASE"] = old_base


def test_full_pipeline_with_real_x8(e2e_repo):
    repo = e2e_repo
    res = run_backtest(
        strategy="NostalgiaForInfinityX8",
        pairs=PAIRS,
        start=START,
        end=END,
        user_data_dir=repo / "user_data",
        datadir=repo / "user_data" / "data",
        strategies_dir=repo / "user_data" / "strategies",
        configs_dir=repo / "user_data" / "nobitex_gui" / "configs",
        results_dir=repo / "user_data" / "nobitex_gui" / "results",
        stake_currency="USDT",
        initial_capital=10_000,
    )
    assert res["ok"], res.get("error")
    assert res["exit_code"] == 0
    zip_path = Path(res["results_zip"])
    assert zip_path.is_file()

    dash = parse_backtest_zip(zip_path, datadir=repo / "user_data" / "data")
    assert dash["strategy"] == "NostalgiaForInfinityX8"
    assert dash["pairlist"] == PAIRS
    assert dash["timeframe"] == "5m"
    assert dash["backtest_start"].startswith("2024-06-01")

    # dashboard sections all populated from the zip
    assert len(dash["per_pair"]) == 2
    assert len(dash["equity_curve"]) > 100
    assert len(dash["daily"]) >= 1
    # buy & hold per pair computed from the downloaded base-tf data
    assert set(dash["buy_hold"]["per_pair"]) == {"BTC/USDT", "ETH/USDT"}
    # X8 is aggressive on trending mock data; it should trade within 14 days
    assert dash["trades_count"] >= 1

    # generated config must be a valid backtest config (spot, no api server)
    import json

    cfg = json.loads(Path(res["config_path"]).read_text(encoding="utf-8"))
    assert cfg["trading_mode"] == "spot"
    assert "api_server" not in cfg
    assert cfg["timerange"] == "20240601-20240615"
