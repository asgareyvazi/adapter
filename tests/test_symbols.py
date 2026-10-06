"""Unit tests: deterministic symbol normalization (Nobitex <-> Freqtrade)."""
from __future__ import annotations

import pytest

from nobitex_adapter.symbols import (
    Market,
    SymbolError,
    base_of,
    freqtrade_to_nobitex,
    is_quote,
    nobitex_to_freqtrade,
    normalize_pair_list,
    quote_of,
)


def test_nobitex_to_freqtrade_basic():
    assert nobitex_to_freqtrade("BTCUSDT") == "BTC/USDT"
    assert nobitex_to_freqtrade("btcusdt") == "BTC/USDT"
    assert nobitex_to_freqtrade("SOLUSDT") == "SOL/USDT"
    assert nobitex_to_freqtrade("BTCIRT") == "BTC/IRT"


def test_freqtrade_to_nobitex_basic():
    assert freqtrade_to_nobitex("BTC/USDT") == "BTCUSDT"
    assert freqtrade_to_nobitex("btc/usdt") == "BTCUSDT"
    with pytest.raises(SymbolError):
        freqtrade_to_nobitex("BTC")
    with pytest.raises(SymbolError):
        freqtrade_to_nobitex("")


def test_roundtrip():
    for sym in ("BTCUSDT", "ETHUSDT", "WBTCUSDT", "SOLUSDT", "BTCUSD", "BNBUSDT"):
        ft = nobitex_to_freqtrade(sym)
        assert freqtrade_to_nobitex(ft) == sym.upper()


def test_quote_and_base():
    assert quote_of("BTCUSDT") == "USDT"
    assert base_of("BTCUSDT") == "BTC"
    assert quote_of("ETH/USDT") == "USDT"
    assert base_of("ETH/USDT") == "ETH"
    assert quote_of("WBTCUSDT") == "USDT"
    assert base_of("WBTCUSDT") == "WBTC"


def test_is_quote():
    assert is_quote("BTCUSDT", "usdt")
    assert not is_quote("BTCUSDT", "EUR")
    assert is_quote("SOL/USDT", "USDT")


def test_unknown_quote_raises():
    with pytest.raises(SymbolError):
        nobitex_to_freqtrade("ABCXYZZZ")
    with pytest.raises(SymbolError):
        nobitex_to_freqtrade("")


def test_longest_quote_match_wins():
    # "1INCHUSDT" must not be split on a shorter quote suffix
    assert nobitex_to_freqtrade("1INCHUSDT") == "1INCH/USDT"
    # USDC vs USD: "ETHUSDC" should resolve to USDC, not ETHU + SDC
    assert nobitex_to_freqtrade("ETHUSDC") == "ETH/USDC"


def test_normalize_pair_list_dedup_and_order():
    out = normalize_pair_list(["BTCUSDT", "btc/usdt", "ETH/USDT", "ethusdt"])
    assert out == ["BTC/USDT", "ETH/USDT"]


def test_normalize_pair_list_quote_filter():
    out = normalize_pair_list(["BTCUSDT", "ETHUSD", "SOLUSDT"], quote="USDT")
    assert out == ["BTC/USDT", "SOL/USDT"]


def test_market_usable_for_backtest():
    good = Market(symbol="BTCUSDT", base="BTC", quote="USDT", ft_symbol="BTC/USDT", active=True)
    assert good.usable_for_backtest

    closed = Market(symbol="SOLUSDT", base="SOL", quote="USDT", ft_symbol="SOL/USDT", active=False)
    assert not closed.usable_for_backtest

    euro = Market(symbol="BTCEUR", base="BTC", quote="EUR", ft_symbol="BTC/EUR", active=True)
    assert euro.usable_for_backtest  # EUR is a supported stable-ish quote

    unstable = Market(symbol="BTCTRY", base="BTC", quote="TRY", ft_symbol="BTC/TRY", active=True)
    assert not unstable.usable_for_backtest  # TRY is not a backtest-stable quote

    same = Market(symbol="USDTUSDT", base="USDT", quote="USDT", ft_symbol="USDT/USDT", active=True)
    assert not same.usable_for_backtest  # base == quote


def test_market_to_dict_keys():
    m = Market(symbol="BTCUSDT", base="BTC", quote="USDT", ft_symbol="BTC/USDT")
    d = m.to_dict()
    for k in ("symbol", "base", "quote", "ft_symbol", "active", "price",
              "volume_base", "volume_quote", "day_change_pct"):
        assert k in d
