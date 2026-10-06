"""A ccxt-compatible exchange class for Nobitex (public market data only).

This is the extension point that lets Freqtrade treat ``nobitex`` as a
first-class exchange for *data and backtesting*, without forking Freqtrade
or shipping a fake exchange.

Scope (milestone 1, by design):
  * public market data only: markets, ticker, OHLCV, order book, trades
  * all private methods (orders, balances, withdrawals, ...) raise
    NotSupported with an explicit message. Live/dry-run trading is a
    later milestone and will add a private API behind the same class.

Everything public here is implemented against the official Nobitex API
docs (github.com/nobitex/docs-api, master) and shares the parsing code
with :mod:`nobitex_adapter.nobitex_client`.

Fees/precision notes (documented assumptions, NOT fetched values):
  * Nobitex's public stats endpoint does not expose maker/taker fees or
    market precision. A default spot fee of 0.2% and 8-decimal precision
    are used as safe defaults; generated backtest configs set an explicit
    ``tradingFee`` so results never depend on this placeholder.
"""
from __future__ import annotations

import time

import ccxt
from ccxt.base.decimal_to_precision import DECIMAL_PLACES
from ccxt.base.errors import NotSupported

from .nobitex_client import DEFAULT_BASE_URL, NobitexClient, NobitexError, parse_candles_payload
from .timeframes import to_nobitex_resolution

# Documented default Nobitex spot fee (maker = taker), overridable via the
# generated backtest config's ``tradingFee``. See docs/nobitex-api.md.
DEFAULT_SPOT_FEE = 0.002


class Nobitex(ccxt.Exchange):  # type: ignore[no-redef]
    """ccxt subclass implementing Nobitex public market data."""

    def describe(self) -> dict:
        return self.deep_extend(super().describe(), {
            "id": "nobitex",
            "name": "Nobitex",
            "countries": ["IR"],
            "certified": False,
            "rateLimit": 200,
            "has": {
                "publicAPI": True,
                "privateAPI": False,
                "CORS": None,
                "spot": True,
                "margin": False,
                "swap": False,
                "future": False,
                "option": False,
                "createOrder": False,
                "createOrders": False,
                "cancelOrder": False,
                "cancelOrders": False,
                "fetchBalance": False,
                "fetchCurrencies": False,
                "fetchDeposit": False,
                "fetchDeposits": False,
                "fetchMarkets": True,
                "fetchMyTrades": False,
                "fetchOHLCV": True,
                "fetchOrderBook": True,
                "fetchOrders": False,
                "fetchOrder": False,
                "fetchTicker": True,
                "fetchTickers": False,
                "fetchTrades": True,
                "fetchTradesFromTimestamp": False,
                "fetchWithdrawals": False,
                "withdraw": False,
            },
            "precisionMode": DECIMAL_PLACES,
            "urls": {
                "logo": "https://nobitex.ir/favicon.ico",
                "home": ["https://nobitex.ir"],
                "api": [DEFAULT_BASE_URL],
                "doc": ["https://apidocs.nobitex.ir", "https://github.com/nobitex/docs-api"],
                "fees": ["https://nobitex.ir/pricing/"],
            },
            "api": {
                "public": {
                    "get": [
                        "market/stats",
                        "market/udf/history",
                        "v3/orderbook/{symbol}",
                        "v2/trades/{symbol}",
                    ]
                },
                "private": {},
            },
            "timeframes": {
                "1m": "1",
                "5m": "5",
                "15m": "15",
                "30m": "30",
                "1h": "60",
                "3h": "180",
                "4h": "240",
                "6h": "360",
                "12h": "720",
                "1d": "D",
                "2d": "2D",
                "3d": "3D",
            },
            "exceptions": {
                "exact": {},
            },
        })

    # ------------------------------------------------------------- infra
    @property
    def _client(self) -> NobitexClient:
        if getattr(self, "_nobitex_client", None) is None:
            self._nobitex_client = NobitexClient()
        return self._nobitex_client

    # ------------------------------------------------------------- markets
    def _parse_market(self, key: str, raw: dict) -> dict:
        base, _, quote = str(key).partition("-")
        base, quote = base.upper(), quote.upper()
        fee = DEFAULT_SPOT_FEE
        return {
            "id": f"{base}{quote}",
            "symbol": f"{base}/{quote}",
            "base": base,
            "quote": quote,
            "baseId": base,
            "quoteId": quote,
            "active": not bool(raw.get("isClosed", False)),
            "type": "spot",
            "spot": True,
            "margin": False,
            "swap": False,
            "future": False,
            "option": False,
            "contract": False,
            "contractSize": None,
            "taker": fee,
            "maker": fee,
            # Nobitex's public API does not expose per-market precision;
            # 8 decimals is a conservative, documented default.
            "precision": {"amount": 8, "price": 8},
            "limits": {
                "amount": {"min": None, "max": None},
                "price": {"min": None, "max": None},
                "cost": {"min": None, "max": None},
            },
            "info": raw,
        }

    def fetch_markets(self, params: dict = {}) -> list:
        raw = self._client.market_stats()
        return [self._parse_market(k, v) for k, v in raw.items() if "-" in str(k)]

    # ---------------------------------------------------------------- ohlcv
    def _fetch_ohlcv(
        self, symbol: str, timeframe: str, since: int | None, limit: int | None, params: dict
    ) -> list:
        market = self.market(symbol)
        try:
            resolution = self.timeframes[timeframe]
        except KeyError as exc:
            raise NotSupported(f"Nobitex does not support timeframe {timeframe!r}") from exc

        now_ms = self.milliseconds()
        start_ms = since if since is not None else now_ms - 500 * self.safe_integer(
            {"1m": 60_000, "5m": 300_000, "15m": 900_000, "30m": 1_800_000,
             "1h": 3_600_000, "3h": 10_800_000, "4h": 14_400_000, "6h": 21_600_000,
             "12h": 43_200_000, "1d": 86_400_000, "2d": 172_800_000, "3d": 259_200_000,
             }.get(timeframe, 300_000), 1
        )
        end_ms = self.safe_integer(params, "until", self.milliseconds())
        # Nobitex caps each response at 500 candles; fetch_ohlcv is a
        # convenience here -- the dedicated downloader handles long ranges.
        candles = self._client.candles_range(
            symbol=market["id"],
            resolution=resolution,
            start_ts=int(start_ms // 1000),
            end_ts=int(end_ms // 1000),
        )
        out = [[c.ts * 1000, c.open, c.high, c.low, c.close, c.volume] for c in candles]
        if limit is not None:
            out = out[-int(limit):]
        return out

    # -------------------------------------------------------------- ticker
    def _fetch_ticker(self, symbol: str, params: dict = {}) -> dict:
        market = self.market(symbol)
        raw = self._client.market_stats(
            src=market["base"].lower(), dst=market["quote"].lower()
        )
        key = f"{market['base'].lower()}-{market['quote'].lower()}"
        data = raw.get(key)
        if not isinstance(data, dict):
            raise NotSupported(f"Nobitex has no stats for {symbol}")
        latest = self.safe_float(data, "latest")
        day_open = self.safe_float(data, "dayOpen")
        change = self.safe_float(data, "dayChange")
        return {
            "symbol": symbol,
            "timestamp": self.milliseconds(),
            "datetime": self.iso8601(self.milliseconds()),
            "high": self.safe_float(data, "dayHigh"),
            "low": self.safe_float(data, "dayLow"),
            "bid": self.safe_float(data, "bestBuy"),
            "bidVolume": None,
            "ask": self.safe_float(data, "bestSell"),
            "askVolume": None,
            "vwap": self.safe_float(data, "mark"),
            "open": day_open,
            "close": self.safe_float(data, "dayClose"),
            "last": latest,
            "previousClose": None,
            "change": (change or 0.0) / 100.0 if latest and day_open else None,
            "percentage": change,
            "average": (latest + day_open) / 2.0 if latest and day_open else None,
            "baseVolume": self.safe_float(data, "volumeSrc"),
            "quoteVolume": self.safe_float(data, "volumeDst"),
            "info": data,
        }

    # ------------------------------------------------------------ orderbook
    def _fetch_order_book(self, symbol: str, limit: int | None = None, params: dict = {}) -> dict:
        market = self.market(symbol)
        raw = self._client.orderbook(market["id"])
        ts = self.safe_integer(raw, "lastUpdate")
        bids = [[self.safe_float(e[0]), self.safe_float(e[1])] for e in (raw.get("bids") or [])]
        asks = [[self.safe_float(e[0]), self.safe_float(e[1])] for e in (raw.get("asks") or [])]
        if limit is not None:
            bids = bids[:limit]
            asks = asks[:limit]
        return {
            "symbol": symbol,
            "bids": sorted(bids, key=lambda x: -x[0]),
            "asks": sorted(asks, key=lambda x: x[0]),
            "timestamp": ts,
            "datetime": self.iso8601(ts) if ts else None,
            "base": market["base"],
            "quote": market["quote"],
            "index": None,
            "indexQuote": None,
            "nonlinear": False,
            "parsed": True,
            "info": raw,
        }

    # --------------------------------------------------------------- trades
    def _fetch_trades(
        self, symbol: str, since: int | None = None, limit: int | None = None, params: dict = {}
    ) -> list:
        market = self.market(symbol)
        raw = self._client.trades(market["id"])
        out = []
        for t in raw:
            ts = self.safe_integer(t, "time")
            price = self.safe_float(t, "price")
            amount = self.safe_float(t, "volume")
            side = self.safe_lower(t, "type")
            out.append({
                "id": None,
                "timestamp": ts,
                "datetime": self.iso8601(ts) if ts else None,
                "symbol": symbol,
                "type": None,
                "side": side if side in ("buy", "sell") else None,
                "price": price,
                "amount": amount,
                "cost": price * amount if price is not None and amount is not None else None,
                "fee": None,
                "info": t,
            })
        if since is not None:
            out = [t for t in out if t["timestamp"] is None or t["timestamp"] >= since]
        if limit is not None:
            out = out[-int(limit):]
        return out

    # ------------------------------------------------- private: BLOCKED on purpose
    def _private_not_available(self, op: str) -> None:
        raise NotSupported(
            f"Nobitex {op}() is not implemented: this milestone is "
            f"market-data/backtest only (no real orders, by design)."
        )

    def create_order(self, *args, **kwargs):
        self._private_not_available("createOrder")

    def cancel_order(self, *args, **kwargs):
        self._private_not_available("cancelOrder")

    def fetch_balance(self, params: dict = {}):
        self._private_not_available("fetchBalance")

    def fetch_order(self, *args, **kwargs):
        self._private_not_available("fetchOrder")

    def fetch_orders(self, *args, **kwargs):
        self._private_not_available("fetchOrders")

    def fetch_my_trades(self, *args, **kwargs):
        self._private_not_available("fetchMyTrades")

    def withdraw(self, *args, **kwargs):
        self._private_not_available("withdraw")


class NobitexAsync(Nobitex):
    """Variant registered in the ``ccxt.async_support`` / ``ccxt.pro`` namespaces.

    Freqtrade instantiates the exchange twice: once sync (``_api``) and once
    through the async namespaces (``_api_async``). For backtesting only the
    market metadata load touches the async handle. The wrappers below keep a
    single parsing implementation (the sync one); the HTTP calls are blocking
    but short, and run inside Freqtrade's dedicated asyncio loop thread.

    The dry-run/live milestone will replace these wrappers with true async
    implementations (aiohttp) once the private API is added.
    """

    async def fetch_markets(self, params: dict = {}):
        return super().fetch_markets(params)

    async def load_markets(self, reload=False, params=None):
        """Coroutine mirror of the sync base load_markets (Freqtrade awaits it)."""
        params = params or {}
        if not reload:
            if self.markets:
                if not self.markets_by_id:
                    return self.set_markets(self.markets)
                return self.markets
        markets = await self.fetch_markets(params)
        return self.set_markets(markets)

    async def fetch_ohlcv(self, symbol, timeframe="1m", since=None, limit=None, params=None):
        return super(Nobitex, self).fetch_ohlcv(symbol, timeframe, since, limit, params or {})

    async def fetch_ticker(self, symbol, params=None):
        return super(Nobitex, self).fetch_ticker(symbol, params or {})

    async def fetch_order_book(self, symbol, limit=None, params=None):
        return super(Nobitex, self).fetch_order_book(symbol, limit, params or {})

    async def fetch_trades(self, symbol, since=None, limit=None, params=None):
        return super(Nobitex, self).fetch_trades(symbol, since, limit, params or {})


def register_ccxt() -> None:
    """Register the Nobitex classes with the ccxt namespaces Freqtrade uses.

    Safe to call multiple times. This does NOT modify ccxt or freqtrade
    source code -- it only adds runtime attributes, the standard way ccxt's
    registry accepts community exchanges.
    """
    import ccxt.async_support as ccxt_async

    targets = [(ccxt, Nobitex), (ccxt_async, NobitexAsync)]
    try:
        import ccxt.pro as ccxt_pro  # Freqtrade checks ccxt.pro first

        targets.append((ccxt_pro, NobitexAsync))
    except Exception:  # noqa: BLE001 - pro is optional
        pass

    for module, cls in targets:
        if getattr(module, "nobitex", None) is not cls:
            setattr(module, "nobitex", cls)
        exchanges = getattr(module, "exchanges", None)
        if exchanges is not None and "nobitex" not in exchanges:
            exchanges.append("nobitex")
