"""Unit tests: provider registry, contract conformance, legacy-client wrap."""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from conftest import (
    FakeResponse,
    azbit_ohlc_rows,
    candles_payload,
    make_azbit_client,
    make_client,
    make_wallex_client,
    wallex_history_payload,
)
from nobitex_adapter.azbit_client import AzbitClient
from nobitex_adapter.downloader import DownloadRequest, Downloader
from nobitex_adapter.nobitex_client import NobitexClient, NobitexNoData
from nobitex_adapter.providers import (
    SUPPORTED_EXCHANGES,
    AzbitProvider,
    NobitexProvider,
    WallexProvider,
    get_provider,
    normalize_exchange,
)
from nobitex_adapter.providers.base import ExchangeProvider

START = int(datetime(2024, 6, 1, tzinfo=timezone.utc).timestamp())
END = int(datetime(2024, 6, 2, tzinfo=timezone.utc).timestamp())


def test_supported_exchanges():
    assert set(SUPPORTED_EXCHANGES) == {"nobitex", "azbit", "wallex"}


def test_cli_exchange_choices_match_registry():
    """Every --exchange choices= list equals the provider registry."""
    import argparse

    from nobitex_adapter.cli import build_parser

    parser = build_parser()
    parsers = [parser]
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            parsers.extend(action.choices.values())
    seen = []
    for p in parsers:
        for action in p._actions:
            if "--exchange" in getattr(action, "option_strings", []):
                seen.append(sorted(action.choices))
    assert len(seen) == 8  # top-level + 7 exchange-aware subcommands
    assert all(c == sorted(SUPPORTED_EXCHANGES) for c in seen)


@pytest.mark.parametrize("name,expected", [(None, "nobitex"), ("", "nobitex"),
                                           ("NOBITEX", "nobitex"), ("AzBit", "azbit"),
                                           ("Wallex", "wallex"), (" WALLEX ", "wallex")])
def test_normalize_exchange(name, expected):
    assert normalize_exchange(name) == expected


def test_normalize_exchange_unknown():
    from nobitex_adapter.providers import ProviderError

    with pytest.raises(ProviderError):
        normalize_exchange("lbank")


def test_get_provider_types():
    assert isinstance(get_provider(None), NobitexProvider)
    assert isinstance(get_provider("nobitex"), NobitexProvider)
    assert isinstance(get_provider("azbit"), AzbitProvider)
    assert isinstance(get_provider("wallex"), WallexProvider)
    for p in (get_provider("nobitex"), get_provider("azbit"), get_provider("wallex")):
        assert isinstance(p, ExchangeProvider)
        assert p.name in SUPPORTED_EXCHANGES
        assert p.supported_timeframes
        assert p.describe()["name"] == p.name


def test_provider_symbol_mapping_stays_ft_faced():
    assert NobitexProvider().to_exchange_symbol("BTC/USDT") == "BTCUSDT"
    assert NobitexProvider().to_ft_symbol("BTCUSDT") == "BTC/USDT"
    assert NobitexProvider().to_exchange_timeframe("5m") == "5"
    assert AzbitProvider().to_exchange_symbol("BTC/USDT") == "BTC_USDT"
    assert AzbitProvider().to_ft_symbol("BTC_USDT") == "BTC/USDT"
    assert AzbitProvider().to_exchange_timeframe("5m") == "minutes5"
    assert WallexProvider().to_exchange_symbol("BTC/USDT") == "BTCUSDT"
    assert WallexProvider().to_exchange_symbol("BTC/TMN") == "BTCTMN"
    assert WallexProvider().to_ft_symbol("BTCTMN") == "BTC/TMN"
    assert WallexProvider().to_exchange_timeframe("5m") == "5"


def test_nobitex_provider_fetch_window_pages_and_notes():
    c1 = make_client([FakeResponse(200, candles_payload(500, START, 300)),
                      FakeResponse(200, candles_payload(100, START + 500 * 300, 300))])
    provider = NobitexProvider(client=c1)
    notes: list[str] = []
    out = provider.fetch_window("BTC/USDT", "5m", START, START + 600 * 300,
                                empty_notes=notes)
    assert len(out) == 600
    assert notes == []


def test_nobitex_provider_fetch_window_empty_note_wording():
    c1 = make_client([FakeResponse(200, {"s": "no_data"})])
    provider = NobitexProvider(client=c1)
    notes: list[str] = []
    assert provider.fetch_window("BTC/USDT", "5m", START, END, empty_notes=notes) == []
    assert notes == [f"no_data symbol=BTCUSDT res=5 from={START} to={END} page=1"]


def test_azbit_provider_fetch_window_and_note():
    rows = azbit_ohlc_rows(START, 10, 300)
    client = make_azbit_client([FakeResponse(200, rows)])
    provider = AzbitProvider(client=client)
    notes: list[str] = []
    out = provider.fetch_window("BTC/USDT", "5m", START, END, empty_notes=notes)
    assert len(out) == 10
    assert notes == []
    assert provider.request_count == 1


def test_azbit_provider_fetch_window_empty_note():
    client = make_azbit_client([FakeResponse(200, [])])
    provider = AzbitProvider(client=client)
    notes: list[str] = []
    assert provider.fetch_window("BTC/USDT", "5m", START, END, empty_notes=notes) == []
    assert len(notes) == 1
    assert "BTC_USDT" in notes[0] and "minutes5" in notes[0]


def test_wallex_provider_fetch_window_and_note():
    rows = wallex_history_payload(10, START, 300)
    empty = {"s": "ok", "t": [], "o": [], "h": [], "l": [], "c": [], "v": []}
    client = make_wallex_client([FakeResponse(200, rows), FakeResponse(200, empty)])
    provider = WallexProvider(client=client)
    notes: list[str] = []
    out = provider.fetch_window("BTC/USDT", "5m", START, END, empty_notes=notes)
    assert len(out) == 10
    assert notes == []
    assert provider.request_count == 2  # page + cursor follow-up (empty)


def test_wallex_provider_fetch_window_empty_note():
    client = make_wallex_client([FakeResponse(200, {"s": "no_data"})])
    provider = WallexProvider(client=client)
    notes: list[str] = []
    assert provider.fetch_window("BTC/USDT", "5m", START, END, empty_notes=notes) == []
    assert notes == [f"empty symbol=BTCUSDT res=5 from={START} to={END}"]


def test_zero_data_messages_are_provider_specific():
    n = NobitexProvider().zero_data_error("BTC/USDT", "5m", START, END,
                                          ["no_data symbol=BTCUSDT res=5 from=1 to=2 page=1"],
                                          "2024-06-01", "2024-06-06")
    assert "ZERO candles" in n and "ohlcv-probe" in n and "Nobitex" in n
    a = AzbitProvider().zero_data_error("BTC/USDT", "5m", START, END,
                                        ["empty pair=BTC_USDT interval=minutes5"],
                                        "2024-06-01", "2024-06-06")
    assert "ZERO candles" in a and "--exchange azbit probe" in a and "AZBit" in a
    w = WallexProvider().zero_data_error("BTC/USDT", "5m", START, END,
                                         ["empty symbol=BTCUSDT res=5 from=1 to=2"],
                                         "2024-06-01", "2024-06-06")
    assert "ZERO candles" in w and "--exchange wallex probe" in w and "Wallex" in w


def test_downloader_accepts_legacy_client_unchanged(tmp_path):
    """A bare NobitexClient keeps working: wrapped, identical results."""
    from nobitex_adapter.nobitex_client import Candle

    class FakeClient:
        def candles_page(self, symbol, resolution, start_ts, end_ts, page=1, **kw):
            if page > 1:
                raise NobitexNoData("x", code="NoData")
            return [Candle(ts=start_ts + i * 300, open=1.0, high=1.1, low=0.9,
                           close=1.05, volume=2.0) for i in range(10)]

    dl = Downloader(FakeClient(), datadir=tmp_path / "data",
                    manifest_dir=tmp_path / "m", report_dir=tmp_path / "r")
    assert isinstance(dl.provider, NobitexProvider)
    summary = dl.download(DownloadRequest(
        pairs=["BTC/USDT"], timeframes=["5m"],
        start=datetime(2024, 6, 1, tzinfo=timezone.utc),
        end=datetime(2024, 6, 1, 0, 50, tzinfo=timezone.utc),
        startup_candles={"5m": 0}, drop_incomplete_last=False, end_is_open=False,
    ))
    assert summary.ok


def test_downloader_rejects_garbage_client(tmp_path):
    with pytest.raises(TypeError):
        Downloader(object(), datadir=tmp_path, manifest_dir=tmp_path)


def test_azbit_request_count_tracks_http():
    rows = azbit_ohlc_rows(START, 5, 300)
    client = make_azbit_client([FakeResponse(200, rows), FakeResponse(200, [])])
    provider = AzbitProvider(client=client)
    assert provider.request_count == 0
    provider.fetch_window("BTC/USDT", "5m", START, END)
    assert provider.request_count == 1


def test_wallex_request_count_tracks_http():
    rows = wallex_history_payload(5, START, 300)
    empty = {"s": "ok", "t": [], "o": [], "h": [], "l": [], "c": [], "v": []}
    client = make_wallex_client([FakeResponse(200, rows), FakeResponse(200, empty)])
    provider = WallexProvider(client=client)
    assert provider.request_count == 0
    provider.fetch_window("BTC/USDT", "5m", START, END)
    assert provider.request_count == 2
