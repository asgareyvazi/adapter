"""Integration tests: `--exchange wallex` CLI end to end vs the mock Wallex API.

Covers the exact user-facing commands: markets / download / probe / depth,
plus zero-data hard-fail parity with Nobitex/AZBit.
"""
from __future__ import annotations

import json

import pytest

from nobitex_adapter.cli import main

pytestmark = pytest.mark.integration


def _run(capsys, monkeypatch, url, repo, argv):
    monkeypatch.setenv("WALLEX_API_BASE", url)
    rc = main(["--repo", str(repo)] + argv)
    return rc, capsys.readouterr()


def test_wallex_markets(capsys, monkeypatch, wallex_mock_server_url, tmp_path):
    rc, out = _run(capsys, monkeypatch, wallex_mock_server_url, tmp_path,
                   ["--exchange", "wallex", "markets", "--quote", "USDT"])
    assert rc == 0, out.err + out.out
    assert "BTC/USDT" in out.out
    assert "markets" in out.out


def test_wallex_markets_tmn_quote(capsys, monkeypatch, wallex_mock_server_url, tmp_path):
    rc, out = _run(capsys, monkeypatch, wallex_mock_server_url, tmp_path,
                   ["--exchange", "wallex", "markets", "--quote", "TMN", "--json"])
    assert rc == 0, out.err + out.out
    data = json.loads(out.out)
    assert {m["ft_symbol"] for m in data} >= {"BTC/TMN", "USDT/TMN"}
    assert all(m["quote"] == "TMN" for m in data)


def test_wallex_markets_json_lists_pairs(capsys, monkeypatch, wallex_mock_server_url, tmp_path):
    rc, out = _run(capsys, monkeypatch, wallex_mock_server_url, tmp_path,
                   ["--exchange", "wallex", "markets", "--quote", "USDT", "--json"])
    assert rc == 0
    data = json.loads(out.out)
    codes = [m["ft_symbol"] for m in data]
    assert "BTC/USDT" in codes and "ETH/USDT" in codes
    assert all(m["quote"] == "USDT" for m in data)


def test_wallex_download_all_five_x8_timeframes(capsys, monkeypatch, wallex_mock_server_url, tmp_path):
    rc, out = _run(
        capsys, monkeypatch, wallex_mock_server_url, tmp_path,
        ["--exchange", "wallex", "download", "--pairs", "BTC/USDT",
         "--timeframes", "5m,15m,1h,4h,1d",
         "--start", "2024-06-01", "--end", "2024-06-06"],
    )
    assert rc == 0, out.err + out.out
    for tf in ("5m", "15m", "1h", "4h", "1d"):
        assert f"BTC/USDT {tf}:" in out.out, out.out
    feathers = list((tmp_path / "user_data" / "data" / "wallex").glob("*.feather"))
    assert len(feathers) == 5


def test_wallex_download_per_command_exchange_override(capsys, monkeypatch, wallex_mock_server_url, tmp_path):
    """`download --exchange wallex` (no global flag) must also bind Wallex."""
    monkeypatch.setenv("WALLEX_API_BASE", wallex_mock_server_url)
    rc = main(["--repo", str(tmp_path), "download", "--pairs", "BTC/USDT",
               "--timeframes", "1h", "--start", "2024-06-01", "--end", "2024-06-02",
               "--exchange", "wallex"])
    assert rc == 0
    assert (tmp_path / "user_data" / "data" / "wallex" / "BTC_USDT-1h.feather").is_file()


def test_wallex_download_unsupported_timeframe_fails_clearly(
    capsys, monkeypatch, wallex_mock_server_url, tmp_path
):
    rc, out = _run(
        capsys, monkeypatch, wallex_mock_server_url, tmp_path,
        ["--exchange", "wallex", "download", "--pairs", "BTC/USDT",
         "--timeframes", "5m,1w", "--start", "2024-06-01", "--end", "2024-06-02"],
    )
    assert rc == 2
    assert "'1w'" in out.out and "Wallex" in out.out


def test_wallex_zero_data_is_hard_fail(capsys, monkeypatch, wallex_mock_server_url, tmp_path):
    """5m before the mock minute floor (2024-01-01) -> ERROR, never success."""
    rc, out = _run(
        capsys, monkeypatch, wallex_mock_server_url, tmp_path,
        ["--exchange", "wallex", "download", "--pairs", "BTC/USDT",
         "--timeframes", "5m", "--start", "2023-01-01", "--end", "2023-01-02"],
    )
    assert rc == 1, out.out
    assert "ZERO candles" in out.out
    assert "--exchange wallex probe" in out.out
    assert "[zero-data FAIL] BTC/USDT 5m" in out.out


def test_wallex_probe_reports_quality(capsys, monkeypatch, wallex_mock_server_url, tmp_path):
    rc, out = _run(
        capsys, monkeypatch, wallex_mock_server_url, tmp_path,
        ["--exchange", "wallex", "probe", "--pair", "BTC/USDT",
         "--timeframes", "5m,15m,1h,4h,1d",
         "--start", "2024-06-01", "--end", "2024-06-06"],
    )
    text = out.out
    assert rc == 0, text
    for field in ("provider", "pair", "timeframe", "range", "requests", "rows",
                  "first", "last", "duplicates", "gaps", "avg gap", "quality",
                  "validation"):
        assert field in text, f"missing probe field {field!r}\n{text}"
    assert "CONTIGUOUS" in text  # mock data is a perfect grid
    assert "verdict" in text


def test_wallex_probe_empty_range_reports_empty(capsys, monkeypatch, wallex_mock_server_url, tmp_path):
    rc, out = _run(
        capsys, monkeypatch, wallex_mock_server_url, tmp_path,
        ["--exchange", "wallex", "probe", "--pair", "BTC/USDT",
         "--timeframes", "5m", "--start", "2023-01-01", "--end", "2023-01-02"],
    )
    assert rc == 1
    assert "EMPTY" in out.out


def test_wallex_depth_reports_common_earliest(capsys, monkeypatch, wallex_mock_server_url, tmp_path):
    rc, out = _run(
        capsys, monkeypatch, wallex_mock_server_url, tmp_path,
        ["--exchange", "wallex", "depth", "--pair", "BTC/USDT",
         "--timeframes", "5m,15m,1h,4h,1d"],
    )
    text = out.out
    assert rc == 0, text
    assert "COMMON_EARLIEST" in text
    for tf in ("5m", "15m", "1h", "4h", "1d"):
        assert tf in text
