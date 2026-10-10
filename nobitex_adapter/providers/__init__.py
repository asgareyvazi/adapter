"""Exchange provider abstraction (multi-exchange support).

Phase-1 audit conclusion: the adapter had NO provider abstraction — the
Nobitex HTTP client, symbol/timeframe mapping and page pagination were
hardwired into the downloader/CLI/jobs. AZBit is integrated through the
minimal generalization below instead of a parallel architecture:

  ExchangeProvider (base.py — the contract)
      ├── NobitexProvider (nobitex.py — wraps the existing NobitexClient)
      ├── AzbitProvider   (azbit.py   — wraps the new AzbitClient)
      └── WallexProvider  (wallex.py  — wraps the new WallexClient)

The downloader, CLI, jobs and GUI talk to ``ExchangeProvider`` only.
Provider-specific syntax (``BTCUSDT`` vs ``BTC_USDT``, ``5`` vs
``minutes5``) never leaks to callers: pairs stay ``BASE/QUOTE`` and
timeframes stay ``5m``/``1h``/… outside the provider.
"""
from __future__ import annotations

from .azbit import AzbitProvider
from .base import (
    SUPPORTED_EXCHANGES,
    DepthInfo,
    ExchangeProvider,
    ProviderError,
    normalize_exchange,
)
from .nobitex import NobitexProvider
from .wallex import WallexProvider

__all__ = [
    "SUPPORTED_EXCHANGES",
    "AzbitProvider",
    "DepthInfo",
    "ExchangeProvider",
    "NobitexProvider",
    "ProviderError",
    "WallexProvider",
    "get_provider",
    "normalize_exchange",
]


def get_provider(name: str | None = None, **kwargs) -> ExchangeProvider:
    """Build the provider for ``name`` (``None``/``""`` -> ``"nobitex"``).

    Extra kwargs are forwarded to the provider constructor (e.g.
    ``base_url``, ``timeout``). Raises ``ProviderError`` for unknown names.
    """
    exch = normalize_exchange(name)
    if exch == "azbit":
        return AzbitProvider(**kwargs)
    if exch == "wallex":
        return WallexProvider(**kwargs)
    return NobitexProvider(**kwargs)
