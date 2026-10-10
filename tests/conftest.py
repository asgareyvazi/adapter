"""Shared fixtures and fakes for the Nobitex adapter test suite.

Unit tests never touch the network: HTTP is faked with scripted responses.
Integration/e2e tests (marked) spin up the in-repo mock Nobitex server on a
local port and/or run real Freqtrade backtests on a small controlled range.
"""
from __future__ import annotations

import socket
import threading
import time
from datetime import datetime, timezone
from typing import Any, Callable, Optional
from unittest import mock

import pandas as pd
import pytest

from nobitex_adapter.nobitex_client import NobitexClient


# --------------------------------------------------------------------- fakes
class FakeResponse:
    def __init__(self, status_code: int = 200, json_data: Any = None, text: str = ""):
        self.status_code = status_code
        self._json = json_data
        self.text = text or (str(json_data) if json_data is not None else "")

    def json(self) -> Any:
        if self._json is None:
            raise ValueError("no json")
        return self._json


class FakeSession:
    """Scripted `requests.Session` stand-in.

    `script` is a list of either:
      * FakeResponse -> returned as-is
      * Exception instance -> raised on .get()
    Requests beyond the script length raise AssertionError (unexpected call).
    """

    def __init__(self, script: list):
        self.script = list(script)
        self.calls: list[dict] = []

    def get(self, url: str, params: Optional[dict] = None, timeout: float = 0, **kw) -> FakeResponse:
        self.calls.append({"url": url, "params": params})
        if not self.script:
            raise AssertionError(f"unexpected request to {url} params={params}")
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    def close(self) -> None:
        pass


def make_client(
    script: list,
    *,
    sleeps: Optional[list] = None,
    base_url: str = "http://test.nobitex.local",
) -> NobitexClient:
    """Build a NobitexClient against a FakeSession with no real waiting."""
    if sleeps is None:
        sleeps = []
    client = NobitexClient(
        base_url=base_url,
        session=FakeSession(script),  # type: ignore[arg-type]
        sleep=sleeps.append,
    )
    # neutralize rate limiting so tests run instantly
    client._limiter = mock.MagicMock()  # type: ignore[assignment]
    return client


def candles_payload(n: int, start_ts: int, interval: int = 300, price: float = 100.0) -> dict:
    """Build a documented columnar /market/udf/history 'ok' payload."""
    t = [start_ts + i * interval for i in range(n)]
    return {
        "s": "ok",
        "t": t,
        "o": [price] * n,
        "h": [price * 1.01] * n,
        "l": [price * 0.99] * n,
        "c": [price * 1.005] * n,
        "v": [10.0] * n,
    }


def make_df(
    start_ts: int,
    n: int,
    interval: int = 300,
    *,
    base: float = 100.0,
) -> pd.DataFrame:
    """Freqtrade-format dataframe (unix-second `date` column)."""
    dates = [start_ts + i * interval for i in range(n)]
    return pd.DataFrame(
        {
            "date": dates,
            "open": [base] * n,
            "high": [base * 1.01] * n,
            "low": [base * 0.99] * n,
            "close": [base * 1.005] * n,
            "volume": [5.0] * n,
        }
    )


# ------------------------------------------------------------------ fixtures
def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="module")
def mock_server_url():
    """Run the in-repo mock Nobitex API on a local port (real HTTP)."""
    import uvicorn

    from nobitex_adapter.mockserver import app as mock_app

    port = free_port()
    config = uvicorn.Config(mock_app, host="127.0.0.1", port=port, log_level="error")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.time() + 10
    while time.time() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                break
        except OSError:
            time.sleep(0.05)
    else:
        raise RuntimeError("mock server did not start")
    yield f"http://127.0.0.1:{port}"
    server.should_exit = True
    thread.join(timeout=5)


@pytest.fixture(scope="module")
def azbit_mock_server_url():
    """Run the in-repo mock AZBit API on a local port (real HTTP)."""
    import uvicorn

    from nobitex_adapter.azbit_mockserver import app as azbit_mock_app

    port = free_port()
    config = uvicorn.Config(azbit_mock_app, host="127.0.0.1", port=port, log_level="error")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.time() + 10
    while time.time() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                break
        except OSError:
            time.sleep(0.05)
    else:
        raise RuntimeError("azbit mock server did not start")
    yield f"http://127.0.0.1:{port}"
    server.should_exit = True
    thread.join(timeout=5)


def azbit_ohlc_rows(start_ts: int, n: int, interval: int = 300,
                    base: float = 100.0) -> list[dict]:
    """Build documented-shape /api/ohlc rows (naive UTC date strings)."""
    from datetime import datetime, timezone

    rows = []
    for i in range(n):
        ts = start_ts + i * interval
        rows.append({
            "date": datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S"),
            "open": base,
            "max": base * 1.01,
            "min": base * 0.99,
            "close": base * 1.005,
            "volume": 5.0,
            "volumeBase": base * 5.0,
        })
    return rows


def make_azbit_client(script: list, **kwargs):
    """Build an AzbitClient against a FakeSession with no real waiting."""
    from unittest import mock

    from nobitex_adapter.azbit_client import AzbitClient

    client = AzbitClient(
        base_url=kwargs.pop("base_url", "http://test.azbit.local"),
        session=FakeSession(script),  # type: ignore[arg-type]
        sleep=kwargs.pop("sleep", lambda s: None),
        **kwargs,
    )
    client._limiter = mock.MagicMock()  # type: ignore[assignment]
    return client


def wallex_history_payload(n: int, start_ts: int, interval: int = 300,
                           price: float = 100.0) -> dict:
    """Build a documented-shape /v1/udf/history 'ok' payload (number-strings)."""
    t = [start_ts + i * interval for i in range(n)]
    return {
        "s": "ok",
        "t": t,
        "o": [f"{price:.10f}"] * n,
        "h": [f"{price * 1.01:.10f}"] * n,
        "l": [f"{price * 0.99:.10f}"] * n,
        "c": [f"{price * 1.005:.10f}"] * n,
        "v": ["10.0000000000"] * n,
    }


def wallex_markets_payload(*symbols: str) -> dict:
    """Build a documented-shape /v1/markets payload for the given symbols."""
    out: dict[str, dict] = {}
    for sym in symbols:
        s = sym.upper()
        quote = "USDT" if s.endswith("USDT") else ("TMN" if s.endswith("TMN") else "")
        base = s[: -len(quote)] if quote else s
        out[s] = {
            "symbol": s,
            "baseAsset": base,
            "baseAssetPrecision": 8,
            "quoteAsset": quote,
            "quotePrecision": 8,
            "faName": f"{base} - {quote}",
            "stats": {
                "bidPrice": "99.9900000000",
                "askPrice": "100.0100000000",
                "24h_ch": 1.5,
                "24h_volume": "12.5000000000",
                "24h_quoteVolume": "1250.0000000000",
                "24h_highPrice": "101.0000000000",
                "24h_lowPrice": "99.0000000000",
                "lastPrice": "100.0000000000",
            },
            "createdAt": "2020-04-01T00:00:00.000000Z",
        }
    return {"success": True, "message": "ok", "result": {"symbols": out}}


def make_wallex_client(script: list, **kwargs):
    """Build a WallexClient against a FakeSession with no real waiting."""
    from unittest import mock

    from nobitex_adapter.wallex_client import WallexClient

    client = WallexClient(
        base_url=kwargs.pop("base_url", "http://test.wallex.local"),
        session=FakeSession(script),  # type: ignore[arg-type]
        sleep=kwargs.pop("sleep", lambda s: None),
        **kwargs,
    )
    client._limiter = mock.MagicMock()  # type: ignore[assignment]
    return client


@pytest.fixture(scope="module")
def wallex_mock_server_url():
    """Run the in-repo mock Wallex API on a local port (real HTTP)."""
    import uvicorn

    from nobitex_adapter.wallex_mockserver import app as wallex_mock_app

    port = free_port()
    config = uvicorn.Config(wallex_mock_app, host="127.0.0.1", port=port, log_level="error")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.time() + 10
    while time.time() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                break
        except OSError:
            time.sleep(0.05)
    else:
        raise RuntimeError("wallex mock server did not start")
    yield f"http://127.0.0.1:{port}"
    server.should_exit = True
    thread.join(timeout=5)


SMALL_STRATEGY = '''
from freqtrade.strategy import IStrategy


class TinyStrategy(IStrategy):
    """Minimal strategy for unit-level backtest plumbing tests."""

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
def tmp_repo(tmp_path):
    """A throw-away repo layout: root with user_data/ + a small strategy."""
    strategies = tmp_path / "user_data" / "strategies"
    strategies.mkdir(parents=True)
    (strategies / "TinyStrategy.py").write_text(SMALL_STRATEGY, encoding="utf-8")
    (tmp_path / "user_data" / "data").mkdir(parents=True)
    return tmp_path
