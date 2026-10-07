"""AZBit materialized data must load through Freqtrade's own OHLCV loader."""
from __future__ import annotations

import os
from datetime import datetime, timezone

import pandas as pd
import pytest

pytestmark = pytest.mark.integration


def test_azbit_feather_loads_via_freqtrade_loader(tmp_path, azbit_mock_server_url, monkeypatch):
    from nobitex_adapter.downloader import DownloadRequest, Downloader
    from nobitex_adapter.providers import AzbitProvider

    monkeypatch.setenv("AZBIT_API_BASE", azbit_mock_server_url)
    provider = AzbitProvider()
    try:
        summary = Downloader(
            provider,
            datadir=tmp_path / "data",
            manifest_dir=tmp_path / "manifests",
            report_dir=tmp_path / "reports",
        ).download(DownloadRequest(
            pairs=["BTC/USDT"], timeframes=["5m"],
            start=datetime(2024, 6, 1, tzinfo=timezone.utc),
            end=datetime(2024, 6, 3, tzinfo=timezone.utc),
            exchange="azbit",
            drop_incomplete_last=True, end_is_open=False,
        ))
    finally:
        provider.close()
    assert summary.ok, {t.pair: (t.status, t.error) for t in summary.tasks}

    from freqtrade.data.history import load_pair_history

    df = load_pair_history(
        pair="BTC/USDT", timeframe="5m", datadir=tmp_path / "data" / "azbit",
        fill_up_missing=False, drop_incomplete=True,
    )
    assert len(df) > 100
    assert list(df.columns) == ["date", "open", "high", "low", "close", "volume"]
    assert str(df["date"].dt.tz) == "UTC"
    ts = (df["date"] - pd.Timestamp("1970-01-01", tz="UTC")) // pd.Timedelta("1s")
    assert (ts.diff().dropna() == 300).all()  # mock grid is contiguous 5m
    assert (df["high"] >= df[["open", "close"]].max(axis=1)).all()
    assert (df["low"] <= df[["open", "close"]].min(axis=1)).all()
    assert (df["volume"] >= 0).all()
