"""The provider contract every exchange implementation follows."""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Optional


class ProviderError(ValueError):
    """Unknown exchange / provider misuse (actionable, no traceback needed)."""


SUPPORTED_EXCHANGES: tuple[str, ...] = ("nobitex", "azbit")


def normalize_exchange(name: str | None) -> str:
    """Normalize an exchange selector (``None``/``""`` -> ``"nobitex"``)."""
    exch = (name or "nobitex").strip().lower()
    if exch not in SUPPORTED_EXCHANGES:
        raise ProviderError(
            f"unknown exchange {name!r} (supported: {', '.join(SUPPORTED_EXCHANGES)})"
        )
    return exch


@dataclass
class DepthInfo:
    """Historical-depth discovery for one (pair, timeframe)."""

    pair: str = ""
    timeframe: str = ""
    earliest_ts: Optional[int] = None
    latest_ts: Optional[int] = None
    requests_made: int = 0
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "pair": self.pair,
            "timeframe": self.timeframe,
            "earliest_ts": self.earliest_ts,
            "latest_ts": self.latest_ts,
            "requests_made": self.requests_made,
            "notes": self.notes,
        }


class ExchangeProvider(ABC):
    """Minimal contract for a public-OHLCV exchange.

    Conventions (binding for every implementation):

    * pairs are ``BASE/QUOTE`` (Freqtrade style) on the way in AND out;
      exchange-native spellings (``BTCUSDT``, ``BTC_USDT``) live INSIDE
      the provider (``to_exchange_symbol`` / ``to_ft_symbol``).
    * timeframes are ``5m``/``15m``/``1h``/``4h``/``1d``-style on the way
      in AND out; ``to_exchange_timeframe`` maps them (``5``/``minutes5``).
    * ``fetch_window`` fetches the ENTIRE ``[start_ts, end_ts)`` window
      (internal pagination included) and returns candles sorted ascending
      by ``ts``. An empty list means "no data in window" (never an error
      here — the downloader turns a fully-empty task into a hard ERROR).
    * every empty response is described in ``empty_notes`` (exact request
      context) so zero-data failures are diagnosable.
    * candles are objects with ``ts``/``open``/``high``/``low``/``close``/
      ``volume`` attributes (duck-typed; each client defines its own).
    * NO synthetic candles, NO silent timestamp alignment, NO silent
      truncation: partial/empty answers are reported, not papered over.
    """

    name: str = ""
    display_name: str = ""
    default_base_url: str = ""

    @property
    @abstractmethod
    def supported_timeframes(self) -> tuple[str, ...]:
        """Freqtrade timeframes this exchange can serve."""

    @abstractmethod
    def to_exchange_symbol(self, pair: str) -> str:
        """``BTC/USDT`` -> exchange-native symbol (``BTCUSDT``/``BTC_USDT``)."""

    @abstractmethod
    def to_ft_symbol(self, exchange_symbol: str) -> str:
        """Exchange-native symbol -> ``BTC/USDT``."""

    @abstractmethod
    def to_exchange_timeframe(self, tf: str) -> str:
        """``5m`` -> exchange-native interval (``5``/``minutes5``)."""

    @abstractmethod
    def discover_markets(self, quote: Optional[str] = None) -> list:
        """Market discovery (returns ``symbols.Market`` objects)."""

    @abstractmethod
    def fetch_window(
        self,
        pair: str,
        tf: str,
        start_ts: int,
        end_ts: int,
        *,
        empty_notes: Optional[list[str]] = None,
    ) -> list:
        """Fetch ALL candles in [start_ts, end_ts), sorted ascending."""

    @abstractmethod
    def discover_depth(self, pair: str, tf: str) -> DepthInfo:
        """Find earliest/latest available candles (verified by real probes,
        never from hardcoded floors alone)."""

    @abstractmethod
    def zero_data_error(
        self,
        pair: str,
        tf: str,
        data_start_ts: int,
        end_ts: int,
        empty_notes: list[str],
        req_start: str,
        req_end: str,
    ) -> str:
        """Actionable zero-data message (exact requests + probe command)."""

    @property
    @abstractmethod
    def request_count(self) -> int:
        """HTTP requests made so far (for probe/diagnostics)."""

    @abstractmethod
    def close(self) -> None:
        """Release HTTP resources."""

    # -- shared helpers -------------------------------------------------
    def describe(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "display_name": self.display_name,
            "base_url": self.default_base_url,
            "supported_timeframes": list(self.supported_timeframes),
        }
