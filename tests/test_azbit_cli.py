"""Integration tests: `--exchange azbit` CLI end to end vs the mock AZBit API.

Covers the exact user-facing commands: markets / download / probe / depth,
plus zero-data hard-fail parity with Nobitex.
"""
from __future__ import annotations

import json

import pytest

from nobitex_adapter.cli import main

pytestmark = pytest.mark.integration


def _run(capsys, monkeypatch, url, repo, argv):
    monkeypatch.setenv("AZBIT_API_BASE", url)
    rc = main(["--repo", str(repo)] + argv)
    return rc, capsys.readouterr()


def test_azbit_markets(capsys, monkeypatch, azbit_mock_server_url, tmp_path):
    rc, out = _run(capsys, monkeypatch, azbit_mock_server_url, tmp_path,
                   ["--exchange", "azbit", "markets", "--quote", "USDT"])
    assert rc == 0, out.err + out.out
    assert "BTC/USDT" in out.out
    assert "markets" in out.out


def test_azbit_markets_json_lists_pairs(capsys, monkeypatch, azbit_mock_server_url, tmp_path):
    rc, out = _run(capsys, monkeypatch, azbit_mock_server_url, tmp_path,
                   ["--exchange", "azbit", "markets", "--quote", "USDT", "--json"])
    assert rc == 0
    data = json.loads(out.out)
    codes = [m["ft_symbol"] for m in data]
    assert "BTC/USDT" in codes and "ETH/USDT" in codes
    assert all(m["quote"] == "USDT" for m in data)


def test_azbit_download_all_five_x8_timeframes(capsys, monkeypatch, azbit_mock_server_url, tmp_path):
    rc, out = _run(
        capsys, monkeypatch, azbit_mock_server_url, tmp_path,
        ["--exchange", "azbit", "download", "--pairs", "BTC/USDT",
         "--timeframes", "5m,15m,1h,4h,1d",
         "--start", "2024-06-01", "--end", "2024-06-06"],
    )
    assert rc == 0, out.err + out.out
    for tf in ("5m", "15m", "1h", "4h", "1d"):
        assert f"BTC/USDT {tf}:" in out.out, out.out
    feathers = list((tmp_path / "user_data" / "data" / "azbit").glob("*.feather"))
    assert len(feathers) == 5


def test_azbit_download_per_command_exchange_override(capsys, monkeypatch, azbit_mock_server_url, tmp_path):
    """`download --exchange azbit` (no global flag) must also bind AZBit."""
    monkeypatch.setenv("AZBIT_API_BASE", azbit_mock_server_url)
    rc = main(["--repo", str(tmp_path), "download", "--pairs", "BTC/USDT",
               "--timeframes", "1h", "--start", "2024-06-01", "--end", "2024-06-02",
               "--exchange", "azbit"])
    assert rc == 0
    assert (tmp_path / "user_data" / "data" / "azbit" / "BTC_USDT-1h.feather").is_file()


def test_azbit_download_unsupported_timeframe_fails_clearly(
    capsys, monkeypatch, azbit_mock_server_url, tmp_path
):
    rc, out = _run(
        capsys, monkeypatch, azbit_mock_server_url, tmp_path,
        ["--exchange", "azbit", "download", "--pairs", "BTC/USDT",
         "--timeframes", "5m,3h", "--start", "2024-06-01", "--end", "2024-06-02"],
    )
    assert rc == 2
    assert "'3h'" in out.out and "AZBit" in out.out


def test_azbit_zero_data_is_hard_fail(capsys, monkeypatch, azbit_mock_server_url, tmp_path):
    """5m before the mock minute floor (2024-01-01) -> ERROR, never success."""
    rc, out = _run(
        capsys, monkeypatch, azbit_mock_server_url, tmp_path,
        ["--exchange", "azbit", "download", "--pairs", "BTC/USDT",
         "--timeframes", "5m", "--start", "2023-01-01", "--end", "2023-01-02"],
    )
    assert rc == 1, out.out
    assert "ZERO candles" in out.out
    assert "--exchange azbit probe" in out.out
    assert "[zero-data FAIL] BTC/USDT 5m" in out.out


def test_azbit_probe_reports_quality(capsys, monkeypatch, azbit_mock_server_url, tmp_path):
    rc, out = _run(
        capsys, monkeypatch, azbit_mock_server_url, tmp_path,
        ["--exchange", "azbit", "probe", "--pair", "BTC/USDT",
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


def test_azbit_probe_empty_range_reports_empty(capsys, monkeypatch, azbit_mock_server_url, tmp_path):
    rc, out = _run(
        capsys, monkeypatch, azbit_mock_server_url, tmp_path,
        ["--exchange", "azbit", "probe", "--pair", "BTC/USDT",
         "--timeframes", "5m", "--start", "2023-01-01", "--end", "2023-01-02"],
    )
    assert rc == 1
    assert "EMPTY" in out.out


def test_azbit_depth_reports_common_earliest(capsys, monkeypatch, azbit_mock_server_url, tmp_path):
    rc, out = _run(
        capsys, monkeypatch, azbit_mock_server_url, tmp_path,
        ["--exchange", "azbit", "depth", "--pair", "BTC/USDT",
         "--timeframes", "5m,15m,1h,4h,1d"],
    )
    text = out.out
    assert rc == 0, text
    assert "COMMON_EARLIEST" in text
    for tf in ("5m", "15m", "1h", "4h", "1d"):
        assert tf in text


def test_nobitex_probe_also_works(capsys, monkeypatch, mock_server_url, tmp_path):
    """The generic probe is provider-agnostic: default exchange = nobitex."""
    monkeypatch.setenv("NOBITEX_API_BASE", mock_server_url)
    rc = main(["--repo", str(tmp_path), "probe", "--pair", "BTC/USDT",
               "--timeframes", "5m", "--start", "2024-06-01", "--end", "2024-06-02"])
    out = capsys.readouterr().out
    assert rc == 0, out
    assert "provider   : nobitex" in out
    assert "CONTIGUOUS" in out


def test_nobitex_default_unchanged_without_exchange_flag(
    capsys, monkeypatch, mock_server_url, tmp_path
):
    """No --exchange anywhere -> historical Nobitex behavior, byte for byte."""
    rc, out = _run_markets_nobitex(capsys, monkeypatch, mock_server_url, tmp_path)
    assert rc == 0
    assert "BTC/USDT" in out.out


def _run_markets_nobitex(capsys, monkeypatch, url, repo):
    monkeypatch.setenv("NOBITEX_API_BASE", url)
    rc = main(["--repo", str(repo), "markets", "--quote", "USDT"])
    return rc, capsys.readouterr()
