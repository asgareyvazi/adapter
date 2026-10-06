"""Freqtrade-compatibility proof: the feather files the downloader writes
must load cleanly through Freqtrade's OWN data handler in the runtime that
executes the test (via the selected .venv when the CLI re-exec is in play).
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.e2e


def test_downloaded_feather_loads_with_freqtrade(mock_server_url, tmp_path):
    """Download via the production Downloader, then load the file with
    freqtrade.data.history.load_pair_history (the same path Freqtrade uses
    for backtesting). Asserts Freqtrade-compatible content: tz-aware UTC,
    canonical columns, strictly monotonic, no duplicates."""
    import os
    from datetime import datetime, timezone

    from nobitex_adapter.downloader import DownloadRequest, Downloader
    from nobitex_adapter.nobitex_client import NobitexClient

    old = os.environ.get("NOBITEX_API_BASE")
    os.environ["NOBITEX_API_BASE"] = mock_server_url
    try:
        client = NobitexClient()
        try:
            summary = Downloader(
                client,
                datadir=tmp_path / "data",
                manifest_dir=tmp_path / "manifests",
                report_dir=tmp_path / "reports",
            ).download(DownloadRequest(
                pairs=["BTC/USDT"], timeframes=["5m"],
                start=datetime(2024, 6, 1, tzinfo=timezone.utc),
                end=datetime(2024, 6, 3, tzinfo=timezone.utc),
                drop_incomplete_last=True, end_is_open=False,
            ))
        finally:
            client.close()
        assert summary.ok, {t.pair: (t.status, t.error) for t in summary.tasks}

        # --- now load it exactly like Freqtrade does ---
        # Freqtrade's per-exchange data layout is {datadir}/{exchange}/...,
        # so the standalone loader's datadir root is the exchange
        # subdirectory. (The config-driven backtest path — `user_data/data`
        # + exchange `nobitex` resolving to the same file — is proven by
        # test_e2e.py, which runs a real Freqtrade backtest on this data.)
        from freqtrade.data.history import load_pair_history

        df = load_pair_history(
            pair="BTC/USDT", timeframe="5m", datadir=tmp_path / "data" / "nobitex",
            fill_up_missing=False, drop_incomplete=True,
        )
        assert len(df) > 0, "Freqtrade could not read the downloaded data"
        # canonical freqtrade columns
        for col in ("date", "open", "high", "low", "close", "volume"):
            assert col in df.columns, df.columns
        # tz-aware UTC
        assert str(df["date"].dt.tz) == "UTC", df["date"].dt.tz
        # strictly monotonic, unique
        ts = df["date"].astype("int64")
        assert (ts.diff().dropna() > 0).all()
        assert ts.is_unique
        # sane OHLC
        assert (df["high"] >= df[["open", "close"]].max(axis=1) - 1e-9).all()
        assert (df["low"] <= df[["open", "close"]].min(axis=1) + 1e-9).all()
        assert (df["volume"] >= 0).all()
        # window respected: no future candles past the requested end
        end_ts = df["date"].astype("int64").max() // 10**9
        assert end_ts <= int(datetime(2024, 6, 3, tzinfo=timezone.utc).timestamp())
    finally:
        if old is None:
            os.environ.pop("NOBITEX_API_BASE", None)
        else:
            os.environ["NOBITEX_API_BASE"] = old
