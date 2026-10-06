"""Deterministic symbol normalization between Nobitex and Freqtrade.

Nobitex native symbols are concatenated strings, e.g. ``BTCUSDT``, ``ETHIRT``,
``1M_BTTUSDT``, ``WBTCUSDT``. Freqtrade uses ``BASE/QUOTE`` (``BTC/USDT``).

Rules (documented, stable, no hard-coded pair lists):
  1. Quote currency = the longest known quote-asset suffix of the symbol
     (case-insensitive match; USDT/USDC/IRT/RLS/BTC/ETH/... are quotes).
  2. Base = whatever remains before the quote.
  3. Freqtrade symbol = ``{BASE}/{QUOTE}`` (upper-case).
  4. Nobitex symbol  = ``{BASE}{QUOTE}`` (upper-case, no separator).

Token-prefix conventions like ``1M_`` / ``100K_`` / ``1B_`` are part of the
BASE and survive the round-trip unchanged.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable, Optional

# Quote assets on Nobitex (from the official docs currency list + stablecoins).
# Only these can be identified as the quote side of a concatenated symbol.
QUOTE_ASSETS: tuple[str, ...] = (
    "USDT",
    "USDC",
    "BUSD",
    "TUSD",
    "FDUSD",
    "PAX",
    "USDP",
    "USD",
    "EUR",
    "TRY",
    "DAI",
    "IRT",
    "RLS",
    "BTC",
    "ETH",
    "BNB",
    "SOL",
    "XRP",
)

# Longest-first so e.g. "USDT" beats shorter ambiguous suffixes deterministically.
_QUOTE_ORDER = sorted(QUOTE_ASSETS, key=len, reverse=True)


class SymbolError(ValueError):
    """Raised when a symbol cannot be normalized."""


def _split(symbol: str) -> tuple[str, str]:
    if not symbol or not isinstance(symbol, str):
        raise SymbolError(f"invalid symbol: {symbol!r}")
    s = symbol.strip().upper()
    if "/" in s:
        base, _, quote = s.partition("/")
        return base.strip(), quote.strip()
    for quote in _QUOTE_ORDER:
        if s.endswith(quote) and len(s) > len(quote):
            base = s[: -len(quote)]
            return base, quote
    raise SymbolError(
        f"cannot detect quote asset in Nobitex symbol {symbol!r} "
        f"(known quotes: {', '.join(QUOTE_ASSETS)})"
    )


def nobitex_to_freqtrade(symbol: str) -> str:
    """``BTCUSDT`` -> ``BTC/USDT``."""
    base, quote = _split(symbol)
    return f"{base}/{quote}"


def freqtrade_to_nobitex(symbol: str) -> str:
    """``BTC/USDT`` -> ``BTCUSDT``."""
    base, _, quote = symbol.partition("/")
    if not base or not quote:
        raise SymbolError(f"expected BASE/QUOTE freqtrade symbol, got {symbol!r}")
    return f"{base.strip().upper()}{quote.strip().upper()}"


def quote_of(symbol: str) -> str:
    """Return the quote asset of a symbol in either format."""
    return _split(symbol)[1]


def base_of(symbol: str) -> str:
    return _split(symbol)[0]


def is_quote(symbol: str, quote: str) -> bool:
    return quote_of(symbol) == quote.upper()


def normalize_pair_list(pairs: Iterable[str], quote: Optional[str] = None) -> list[str]:
    """Normalize an iterable of symbols (either format) to unique Freqtrade
    ``BASE/QUOTE`` symbols, optionally filtered to a quote asset.

    Order is preserved; duplicates are removed deterministically.
    """
    out: list[str] = []
    seen: set[str] = set()
    for p in pairs:
        ft = nobitex_to_freqtrade(p) if "/" not in p else p.strip().upper()
        if quote and ft.split("/")[1] != quote.upper():
            continue
        if ft not in seen:
            seen.add(ft)
            out.append(ft)
    return out


@dataclass
class Market:
    """A discovered Nobitex market (spot)."""

    symbol: str  # Nobitex native, e.g. BTCUSDT
    base: str
    quote: str
    ft_symbol: str  # Freqtrade style, e.g. BTC/USDT
    active: bool = True  # not closed on the exchange
    price: float = 0.0  # last price
    volume_base: float = 0.0  # 24h volume in base asset
    volume_quote: float = 0.0  # 24h volume in quote asset
    day_change_pct: float = 0.0
    info: dict = field(default_factory=dict)  # raw exchange payload

    @property
    def is_stable_quote(self) -> bool:
        return self.quote in ("USDT", "USDC", "BUSD", "DAI", "TUSD", "FDUSD", "USD", "EUR")

    @property
    def usable_for_backtest(self) -> bool:
        """Active spot market with a stable quote, tradeable today."""
        return self.active and self.is_stable_quote and self.base != self.quote

    def to_dict(self) -> dict:
        return {
            "symbol": self.symbol,
            "base": self.base,
            "quote": self.quote,
            "ft_symbol": self.ft_symbol,
            "active": self.active,
            "price": self.price,
            "volume_base": self.volume_base,
            "volume_quote": self.volume_quote,
            "day_change_pct": self.day_change_pct,
        }
