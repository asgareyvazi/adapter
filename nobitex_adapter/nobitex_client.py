"""Minimal, honest client for the Nobitex public market-data API.

Implemented strictly against the official documentation
(github.com/nobitex/docs-api, branch master, source/includes/):

  * ``GET /market/stats``            -- market discovery / 24h stats   (20 req/min)
  * ``GET /market/udf/history``      -- OHLCV candles, <=500 per page  (paginated)
  * ``GET /v3/orderbook/SYMBOL``     -- order book                     (300 req/min)
  * ``GET /v2/trades/SYMBOL``        -- recent trades                  (60 req/min)

Base URL is ``https://apiv2.nobitex.ir`` per the current docs (the base address
changed to apiv2 in Khordad 1404 / June 2025 per the official changelog).
Override with the environment variable ``NOBITEX_API_BASE`` (used by tests to
point at the bundled mock server; on real usage leave it unset).

Only public endpoints are implemented. No authentication is used or needed.
No private API calls exist in this module (by design, milestone 1).
"""
from __future__ import annotations

import logging
import os
import threading
import time
from dataclasses import dataclass
from typing import Callable, Optional

import requests

from .symbols import Market, nobitex_to_freqtrade
from .timeframes import TimeframeError, to_nobitex_resolution

DEFAULT_BASE_URL = "https://apiv2.nobitex.ir"

log = logging.getLogger("nobitex.client")

# Conservative client-side rate limits (requests per second) per endpoint.
# The official docs state per-endpoint limits (e.g. /market/stats: 20/min,
# /v2/trades: 60/min, /v3/orderbook: 300/min). /market/udf/history has no
# documented number, so we default to a gentle 10 req/s and back off on 429.
DEFAULT_ENDPOINT_RPS = {
    "/market/stats": 0.3,  # 20/min
    "/market/udf/history": 10.0,
    "/v3/orderbook": 5.0,  # well under 300/min
    "/v2/trades": 0.9,  # 60/min
}


class NobitexError(Exception):
    """Base error. Carries request context for diagnostics."""

    def __init__(
        self,
        message: str,
        *,
        endpoint: str = "",
        params: Optional[dict] = None,
        http_status: Optional[int] = None,
        code: Optional[str] = None,
        body_snippet: str = "",
    ) -> None:
        super().__init__(message)
        self.endpoint = endpoint
        self.params = params or {}
        self.http_status = http_status
        self.code = code
        self.body_snippet = body_snippet[:500]

    def context(self) -> str:
        bits = [f"endpoint={self.endpoint}"]
        bits.append(f"params={self.params}")
        if self.http_status is not None:
            bits.append(f"http={self.http_status}")
        if self.code:
            bits.append(f"code={self.code}")
        if self.body_snippet:
            bits.append(f"body={self.body_snippet!r}")
        return " ".join(bits)


class NobitexAPIError(NobitexError):
    """The API answered with a logical failure (status=failed / s=error)."""


class NobitexRateLimitedError(NobitexAPIError):
    """HTTP 429 / TooManyRequests; `back_off` seconds are authoritative."""

    def __init__(self, *args, back_off: float = 5.0, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.back_off = float(back_off)


class NobitexNoData(NobitexError):
    """The API answered `s: no_data` -- no candles exist for the range."""


@dataclass
class Candle:
    """One OHLCV candle. `ts` is the candle OPEN time, unix seconds (UTC)."""

    ts: int
    open: float
    high: float
    low: float
    close: float
    volume: float


def _f(value, field: str, ctx: str) -> float:
    try:
        return float(value)
    except (TypeError, ValueError) as exc:
        raise NobitexAPIError(
            f"malformed numeric value in candle field {field!r}: {value!r} ({ctx})",
            code="MalformedCandle",
        ) from exc


def parse_candles_payload(payload: dict, ctx: str = "") -> list[Candle]:
    """Parse the columnar `/market/udf/history` response into Candle objects.

    Documented response: {"s": "ok", "t": [...], "o": [...], "h": [...],
    "l": [...], "c": [...], "v": [...]}
    """
    if not isinstance(payload, dict):
        raise NobitexAPIError(f"unexpected non-object payload ({ctx})", body_snippet=str(payload))
    status = payload.get("s")
    if status == "no_data":
        raise NobitexNoData(f"no data returned ({ctx})", code="NoData")
    if status != "ok":
        raise NobitexAPIError(
            f"API error: {payload.get('errmsg', status)} ({ctx})",
            code=str(payload.get("errmsg", status)),
            body_snippet=str(payload),
        )
    t = payload.get("t") or []
    cols = {
        "open": payload.get("o") or [],
        "high": payload.get("h") or [],
        "low": payload.get("l") or [],
        "close": payload.get("c") or [],
        "volume": payload.get("v") or [],
    }
    n = len(t)
    for name, col in cols.items():
        if len(col) != n:
            raise NobitexAPIError(
                f"candle column length mismatch: t={n} {name}={len(col)} ({ctx})",
                code="MalformedCandle",
            )
    out: list[Candle] = []
    for i in range(n):
        out.append(
            Candle(
                ts=int(t[i]),
                open=_f(cols["open"][i], "open", ctx),
                high=_f(cols["high"][i], "high", ctx),
                low=_f(cols["low"][i], "low", ctx),
                close=_f(cols["close"][i], "close", ctx),
                volume=_f(cols["volume"][i], "volume", ctx),
            )
        )
    return out


class _RateLimiter:
    """Tiny per-endpoint token-bucket limiter (thread-safe)."""

    def __init__(self, rps: dict[str, float]) -> None:
        self._rps = dict(rps)
        self._lock = threading.Lock()
        self._last: dict[str, float] = {}

    def wait(self, endpoint: str) -> None:
        rps = self._rps.get(endpoint, 5.0)
        if rps <= 0:
            return
        interval = 1.0 / rps
        with self._lock:
            now = time.monotonic()
            last = self._last.get(endpoint, 0.0)
            sleep_for = last + interval - now
            self._last[endpoint] = max(now, last) + interval
        if sleep_for > 0:
            time.sleep(sleep_for)


class NobitexClient:
    """Public market-data client with retry, backoff and rate limiting.

    All methods raise NobitexError subclasses with full request context.
    No credentials are ever sent.
    """

    def __init__(
        self,
        base_url: Optional[str] = None,
        timeout: float = 25.0,
        max_retries: int = 5,
        rate_limit_rps: Optional[dict[str, float]] = None,
        session: Optional[requests.Session] = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.base_url = (base_url or os.environ.get("NOBITEX_API_BASE") or DEFAULT_BASE_URL).rstrip(
            "/"
        )
        self.timeout = timeout
        self.max_retries = max_retries
        self._limiter = _RateLimiter(
            rate_limit_rps or DEFAULT_ENDPOINT_RPS
        )
        self.session = session or requests.Session()
        self._sleep = sleep
        self.request_count = 0

    # ------------------------------------------------------------------ http
    def _get(self, path: str, params: Optional[dict] = None, _retry: int = 0) -> dict:
        """GET a JSON endpoint with retries for network errors / 5xx / 429."""
        self._limiter.wait(path)
        url = f"{self.base_url}{path}"
        log.debug("GET %s params=%s", url, params)
        try:
            resp = self.session.get(url, params=params, timeout=self.timeout)
        except requests.RequestException as exc:
            self.request_count += 1
            if _retry < self.max_retries:
                delay = min(30.0, 1.0 * (2**_retry))
                log.warning(
                    "network error on %s (%s); retry %d/%d in %.1fs",
                    path, exc.__class__.__name__, _retry + 1, self.max_retries, delay,
                )
                self._sleep(delay)
                return self._get(path, params, _retry + 1)
            raise NobitexError(
                f"request failed after {_retry} retries: {exc}",
                endpoint=path, params=params,
            ) from exc

        self.request_count += 1
        ctx = {"endpoint": path, "params": params or {}}

        # Rate limit: honor the server's backOff.
        if resp.status_code == 429 or (
            resp.status_code == 200 and self._json_status(resp) == "failed"
            and self._json_code(resp) == "TooManyRequests"
        ):
            payload = self._safe_json(resp)
            back_off = float(payload.get("backOff", 5) if isinstance(payload, dict) else 5)
            err = NobitexRateLimitedError(
                f"rate limited on {path}: backOff={back_off}s "
                f"limit={payload.get('limit') if isinstance(payload, dict) else '?'}",
                endpoint=path, params=params, http_status=resp.status_code,
                code="TooManyRequests", body_snippet=resp.text[:200], back_off=back_off,
            )
            if _retry < self.max_retries:
                log.warning("rate limited on %s; sleeping %.1fs then retrying", path, back_off)
                self._sleep(back_off)
                return self._get(path, params, _retry + 1)
            raise err

        if resp.status_code >= 500:
            if _retry < self.max_retries:
                delay = min(30.0, 1.0 * (2**_retry))
                log.warning(
                    "server error %d on %s; retry %d/%d in %.1fs",
                    resp.status_code, path, _retry + 1, self.max_retries, delay,
                )
                self._sleep(delay)
                return self._get(path, params, _retry + 1)
            raise NobitexError(
                f"server error {resp.status_code} on {path}",
                endpoint=path, params=params, http_status=resp.status_code,
                body_snippet=resp.text[:200],
            )

        if resp.status_code >= 400:
            raise NobitexError(
                f"client error {resp.status_code} on {path}: {resp.text[:200]}",
                endpoint=path, params=params, http_status=resp.status_code,
                body_snippet=resp.text[:200],
            )

        payload = self._safe_json(resp)
        if not isinstance(payload, dict):
            raise NobitexAPIError(
                f"non-JSON object response from {path}",
                endpoint=path, params=params, http_status=resp.status_code,
                body_snippet=resp.text[:200],
            )
        # Some endpoints use {"status": ...}, the OHLC endpoint uses {"s": ...}
        status = payload.get("status", payload.get("s"))
        if status == "failed":
            raise NobitexAPIError(
                f"API failure on {path}: {payload.get('message', payload.get('code'))}",
                endpoint=path, params=params, http_status=resp.status_code,
                code=str(payload.get("code", "")), body_snippet=resp.text[:200],
            )
        return payload

    @staticmethod
    def _safe_json(resp: requests.Response) -> object:
        try:
            return resp.json()
        except ValueError:
            return {}

    def _json_status(self, resp: requests.Response) -> str:
        p = self._safe_json(resp)
        return str(p.get("status", "")) if isinstance(p, dict) else ""

    def _json_code(self, resp: requests.Response) -> str:
        p = self._safe_json(resp)
        return str(p.get("code", "")) if isinstance(p, dict) else ""

    # ------------------------------------------------------------- endpoints
    def market_stats(self, src: Optional[str] = None, dst: Optional[str] = None) -> dict:
        """``GET /market/stats`` -> raw stats dict keyed by 'base-quote'."""
        params: dict = {}
        if src:
            params["srcCurrency"] = src
        if dst:
            params["dstCurrency"] = dst
        payload = self._get("/market/stats", params)
        stats = payload.get("stats")
        if not isinstance(stats, dict):
            raise NobitexAPIError(
                f"unexpected /market/stats payload (no 'stats' object)",
                endpoint="/market/stats", params=params,
            )
        return stats

    def discover_markets(self, quote: Optional[str] = None) -> list[Market]:
        """Discover markets. When `quote` is given, filter to that quote.

        Returns a deterministic, sorted list of Market objects.
        """
        dst = quote.upper() if quote else None
        raw = self.market_stats(dst=dst)
        out: list[Market] = []
        for key, st in raw.items():
            # key looks like "btc-usdt"
            base, _, q = str(key).partition("-")
            if not q:
                log.debug("skipping stats entry with malformed key %r", key)
                continue
            if dst and q.upper() != dst:
                continue
            try:
                ft = nobitex_to_freqtrade(f"{base.upper()}{q.upper()}")
            except Exception as exc:  # noqa: BLE001 - be resilient to odd pairs
                log.debug("skipping symbol %r: %s", key, exc)
                continue
            market = Market(
                symbol=f"{base.upper()}{q.upper()}",
                base=ft.split("/")[0],
                quote=ft.split("/")[1],
                ft_symbol=ft,
                active=not bool(st.get("isClosed", False)),
                price=_safe_float(st.get("latest")),
                volume_base=_safe_float(st.get("volumeSrc")),
                volume_quote=_safe_float(st.get("volumeDst")),
                day_change_pct=_safe_float(st.get("dayChange")),
                info={k: st.get(k) for k in ("dayHigh", "dayLow", "dayOpen", "bestBuy", "bestSell")},
            )
            out.append(market)
        out.sort(key=lambda m: m.symbol)
        return out

    def candles_page(
        self,
        symbol: str,
        resolution: str,
        start_ts: int,
        end_ts: int,
        page: int = 1,
    ) -> list[Candle]:
        """Fetch ONE page (<=500 candles) from /market/udf/history.

        Raises NobitexNoData when the range has no data.
        """
        params = {
            "symbol": symbol,
            "resolution": resolution,
            "from": int(start_ts),
            "to": int(end_ts),
            "page": int(page),
        }
        payload = self._get("/market/udf/history", params)
        ctx = f"symbol={symbol} res={resolution} from={start_ts} to={end_ts} page={page}"
        return parse_candles_payload(payload, ctx=ctx)

    def candles_range(
        self,
        symbol: str,
        resolution: str,
        start_ts: int,
        end_ts: int,
        page_size: int = 500,
        progress_cb: Optional[Callable[[int, int], None]] = None,
        stop_event: Optional[threading.Event] = None,
    ) -> list[Candle]:
        """Fetch the full [start_ts, end_ts) range by walking pages.

        Returns candles sorted by ts (may include duplicates on overlap --
        callers should dedupe; the downloader does).
        """
        out: list[Candle] = []
        page = 1
        while True:
            if stop_event is not None and stop_event.is_set():
                raise NobitexError("cancelled", endpoint="/market/udf/history",
                                   params={"symbol": symbol})
            try:
                batch = self.candles_page(symbol, resolution, start_ts, end_ts, page)
            except NobitexNoData:
                break
            if not batch:
                break
            out.extend(batch)
            if progress_cb:
                progress_cb(len(out), page)
            if len(batch) < page_size:
                break
            page += 1
        return sorted(out, key=lambda c: c.ts)

    def orderbook(self, symbol: str) -> dict:
        """``GET /v3/orderbook/SYMBOL`` (documented; not required for backtest)."""
        return self._get(f"/v3/orderbook/{symbol}")

    def trades(self, symbol: str) -> list[dict]:
        """``GET /v2/trades/SYMBOL`` (documented; not required for backtest)."""
        payload = self._get(f"/v2/trades/{symbol}")
        return list(payload.get("trades") or [])

    def close(self) -> None:
        self.session.close()


def _safe_float(v) -> float:
    try:
        if v is None:
            return 0.0
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def resolution_for(tf: str) -> str:
    """Convenience: freqtrade timeframe -> nobitex resolution."""
    return to_nobitex_resolution(tf)
