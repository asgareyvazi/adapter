"""CLI-level regression tests for the timeframe parsing incident.

Reproduces the real Windows run:

    download --pairs BTC/USDT --timeframes 5m,15m,1h,4h,1d \
        --start 2024-06-01 --end 2024-06-06

which previously died with `TimeframeError: invalid timeframe '1'` (the
`1d` token reaching a character-iterating code path) and silently reported
`candles 0` for 5m/15m. These tests drive the real `main()` entry point
against the in-repo mock Nobitex API.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone

import pytest

from nobitex_adapter.cli import main

pytestmark = pytest.mark.integration


def _run_cli(capsys, monkeypatch, mock_server_url, repo, argv):
    monkeypatch.setenv("NOBITEX_API_BASE", mock_server_url)
    rc = main(["--repo", str(repo)] + argv)
    return rc, capsys.readouterr()


# ------------------------------------------------------------- the incident
def test_user_command_succeeds_with_all_five_timeframes(
    capsys, monkeypatch, mock_server_url, tmp_path
):
    """The exact failing command now downloads all five X8 timeframes."""
    rc, out = _run_cli(
        capsys, monkeypatch, mock_server_url, tmp_path,
        ["download", "--pairs", "BTC/USDT",
         "--timeframes", "5m,15m,1h,4h,1d",
         "--start", "2024-06-01", "--end", "2024-06-06"],
    )
    assert rc == 0, out.err + out.out
    text = out.out
    for tf in ("5m", "15m", "1h", "4h", "1d"):
        assert f"BTC/USDT {tf}:" in text, text
    assert "TimeframeError" not in text
    assert "candles 0" not in text


def test_space_joined_timeframes_also_work(
    capsys, monkeypatch, mock_server_url, tmp_path
):
    """Shells (e.g. PowerShell comma-array expansion) may hand the list
    space-joined: `5m 15m 1h 4h 1d`. The canonical boundary accepts it."""
    rc, out = _run_cli(
        capsys, monkeypatch, mock_server_url, tmp_path,
        ["download", "--pairs", "BTC/USDT",
         "--timeframes", "5m 15m 1h 4h 1d",
         "--start", "2024-06-01", "--end", "2024-06-06"],
    )
    assert rc == 0, out.err + out.out
    for tf in ("5m", "15m", "1h", "4h", "1d"):
        assert f"BTC/USDT {tf}:" in out.out


def test_char_split_input_fails_clearly_without_traceback(
    capsys, monkeypatch, mock_server_url, tmp_path
):
    """The exact broken token list the user's run produced ('1' and 'd' as
    separate items) must be rejected with a clear, actionable error and
    exit code 2 — no traceback, no partial download."""
    rc, out = _run_cli(
        capsys, monkeypatch, mock_server_url, tmp_path,
        ["download", "--pairs", "BTC/USDT",
         "--timeframes", "5m,15m,1h,4h,1,d",
         "--start", "2024-06-01", "--end", "2024-06-06"],
    )
    assert rc == 2
    text = out.out + out.err
    assert "Traceback" not in text
    assert "invalid timeframe '1'" in text
    # shows the raw received value so the user can see what the shell passed
    assert "['5m', '15m', '1h', '4h', '1', 'd']" in text
    # actionable hint
    assert "--timeframes" in text
    # nothing was downloaded
    assert not list((tmp_path / "user_data" / "data").rglob("*.feather"))


# ------------------------------------------------- zero data is a hard fail
def test_zero_data_is_not_silent_success(
    capsys, monkeypatch, mock_server_url, tmp_path
):
    """5m before the documented minute-data floor (~2022-03-20) comes back
    no_data from the API. The CLI must exit non-zero with the diagnostic —
    never report the download as a success."""
    rc, out = _run_cli(
        capsys, monkeypatch, mock_server_url, tmp_path,
        ["download", "--pairs", "BTC/USDT", "--timeframes", "5m",
         "--start", "2021-01-01", "--end", "2021-01-02"],
    )
    assert rc == 1, out.out
    text = out.out
    assert "ZERO candles" in text
    assert "no_data" in text
    assert "ohlcv-probe" in text
    assert "[zero-data FAIL] BTC/USDT 5m" in text


# ------------------------------------------------------------------- probe
def test_ohlcv_probe_shows_raw_responses(
    capsys, monkeypatch, mock_server_url, tmp_path
):
    """The raw diagnostic prints the exact request URLs and raw JSON for
    the requested range, a narrow window, the recent range and countback."""
    rc, out = _run_cli(
        capsys, monkeypatch, mock_server_url, tmp_path,
        ["ohlcv-probe", "--pair", "BTC/USDT", "--timeframe", "5m",
         "--start", "2024-06-01", "--end", "2024-06-06"],
    )
    text = out.out
    assert rc in (0, 1), text
    assert "GET " in text and "/market/udf/history?" in text
    assert "symbol=BTCUSDT" in text
    assert "resolution=5" in text
    assert "P1:" in text and "P2:" in text and "P3:" in text and "P4:" in text
    assert "RESPONSE:" in text
    assert "diagnosis" in text.lower()
    # June 2024 5m exists in the mock -> P1 must report candles
    assert "candles" in text
