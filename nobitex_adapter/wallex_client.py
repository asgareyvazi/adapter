"""Minimal, honest client for the Wallex public market-data API.

Implemented strictly against the official reference
(https://api-docs.wallex.ir/, sections "بازارها" / markets):

  * ``GET /v1/markets``        -- market discovery (``result.symbols`` map)
  * ``GET /v1/udf/history``    -- OHLCV candles, UDF columnar format
    (``symbol``/``resolution``/``from``/``to`` -> ``{s,t,o,h,l,c,v}``)

Base URL is ``https://api.wallex.ir`` per the docs ("تمام اندپوینت‌ها ...
تنها از طریق نشانی https://api.wallex.ir دردسترس‌اند"). Override with the
environment variable ``WALLEX_API_BASE`` (used by tests to point at the
bundled mock server; on real usage leave it unset).

Documented gaps (handled honestly, never papered over):

  * the reference documents only ONE ``resolution`` example value (``60``);
    the full ladder lives in ``timeframes.WALLEX_RESOLUTIONS`` and is marked
    PROVISIONAL until each entry passes a live probe;
  * ``/v1/udf/history`` documents NO ``page`` parameter and NO row cap, so
    pagination is cursor-based (advance ``from`` past the last received
    candle; empty response ends the walk). Termination never depends on a
    guessed cap: empty-window, stall and max-request guards bound the loop;
  * no rate limits are documented: the client defaults to gentle limits and
    honors ``429`` + ``Retry-After``;
  * market entries carry NO active/disabled flag: ``discover_markets``
    reports every listed symbol as active.

Only public endpoints are implemented. No authentication is used or needed.
No private API calls exist in this module (by design).
"""
from __future__ import annotations

import logging
import os
import threading
import time
from dataclasses import dataclass
from typing import Callable, Optional

import requests

from .ratelimit import RateLimiter as _RateLimiter  # noqa: F401 (compat alias)
from .symbols import Market, wallex_to_freqtrade
from .timeframes import TimeframeError, to_wallex_resolution  # noqa: F401  (re-exported)

DEFAULT_BASE_URL = "https://api.wallex.ir"

log = logging.getLogger("wallex.client")

# Conservative client-side rate limits (requests per second) per endpoint.
# Wallex documents NO rate limits, so these are deliberately gentle; the
# client backs off on 429 (honoring Retry-After when the server sends one).
DEFAULT_ENDPOINT_RPS = {
    "/v1/markets": 1.0,
    "/v1/udf/history": 5.0,
}

DEFAULT_MAX_REQUESTS = 2000


class WallexError(Exception):
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


class WallexAPIError(WallexError):
    """The API answered with a logical failure (success=false / s=error)."""


class WallexRateLimitedError(WallexAPIError):
    """HTTP 429; `retry_after` seconds are authoritative when sent."""

    def __init__(self, *args, retry_after: float = 5.0, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.retry_after = float(retry_after)


class WallexNoData(WallexError):
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
        raise WallexAPIError(
            f"malformed numeric value in candle field {field!r}: {value!r} ({ctx})",
            code="MalformedCandle",
        ) from exc


def parse_history_payload(payload: dict, ctx: str = "") -> list[Candle]:
    """Parse the columnar `/v1/udf/history` response into Candle objects.

    Documented response: {"s": "ok", "t": [...], "o": [...], "h": [...],
    "l": [...], "c": [...], "v": [...]} with number-strings. Raises
    WallexNoData on ``s: no_data``; ``s: ok`` with empty arrays is a
    legitimate empty window (returns []).
    """
    if not isinstance(payload, dict):
        raise WallexAPIError(
            f"unexpected non-object payload ({ctx})", body_snippet=str(payload)
        )
    status = payload.get("s")
    if status == "no_data":
        raise WallexNoData(f"no data returned ({ctx})", code="NoData")
    if status != "ok":
        raise WallexAPIError(
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
            raise WallexAPIError(
                f"candle column length mismatch: t={n} {name}={len(col)} ({ctx})",
                code="MalformedCandle",
            )
    out: list[Candle] = []
    for i in range(n):
        try:
            ts = int(t[i])
        except (TypeError, ValueError) as exc:
            raise WallexAPIError(
                f"malformed timestamp in candle column 't': {t[i]!r} ({ctx})",
                code="MalformedCandle",
            ) from exc
        out.append(
            Candle(
                ts=ts,
                open=_f(cols["open"][i], "open", ctx),
                high=_f(cols["high"][i], "high", ctx),
                low=_f(cols["low"][i], "low", ctx),
                close=_f(cols["close"][i], "close", ctx),
                volume=_f(cols["volume"][i], "volume", ctx),
            )
        )
    return out


class WallexClient:
    """Public market-data client with retry, backoff and rate limiting.

    All methods raise WallexError subclasses with full request context.
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
        max_requests: int = DEFAULT_MAX_REQUESTS,
    ) -> None:
        self.base_url = (base_url or os.environ.get("WALLEX_API_BASE") or DEFAULT_BASE_URL).rstrip(
            "/"
        )
        self.timeout = timeout
        self.max_retries = max_retries
        self._limiter = _RateLimiter(rate_limit_rps or DEFAULT_ENDPOINT_RPS)
        self.session = session or requests.Session()
        self._sleep = sleep
        self.max_requests = int(max_requests)
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
            raise WallexError(
                f"request failed after {_retry} retries: {exc}",
                endpoint=path, params=params,
            ) from exc

        self.request_count += 1

        if resp.status_code == 429:
            retry_after = _retry_after_seconds(resp, default=5.0)
            err = WallexRateLimitedError(
                f"rate limited on {path}: retry_after={retry_after}s",
                endpoint=path, params=params, http_status=resp.status_code,
                code="RateLimited", body_snippet=resp.text[:200],
                retry_after=retry_after,
            )
            if _retry < self.max_retries:
                log.warning("rate limited on %s; sleeping %.1fs then retrying", path, retry_after)
                self._sleep(retry_after)
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
            raise WallexError(
                f"server error {resp.status_code} on {path}",
                endpoint=path, params=params, http_status=resp.status_code,
                body_snippet=resp.text[:200],
            )

        if resp.status_code >= 400:
            raise WallexError(
                f"client error {resp.status_code} on {path}: {resp.text[:200]}",
                endpoint=path, params=params, http_status=resp.status_code,
                body_snippet=resp.text[:200],
            )

        payload = self._safe_json(resp)
        if not isinstance(payload, dict):
            raise WallexAPIError(
                f"non-JSON object response from {path}",
                endpoint=path, params=params, http_status=resp.status_code,
                body_snippet=resp.text[:200],
            )
        # /v1/* endpoints use the {"success": bool, "result": ...} envelope;
        # /v1/udf/history answers raw UDF {"s": "ok"|"no_data", ...}.
        if "success" in payload and not payload.get("success"):
            raise WallexAPIError(
                f"API failure on {path}: {payload.get('message', payload)}",
                endpoint=path, params=params, http_status=resp.status_code,
                body_snippet=resp.text[:200],
            )
        return payload

    @staticmethod
    def _safe_json(resp: requests.Response) -> object:
        try:
            return resp.json()
        except ValueError:
            return {}

    # ------------------------------------------------------------- endpoints
    def markets(self) -> dict:
        """``GET /v1/markets`` -> raw ``result.symbols`` map."""
        payload = self._get("/v1/markets", {})
        result = payload.get("result")
        symbols = result.get("symbols") if isinstance(result, dict) else None
        if not isinstance(symbols, dict):
            raise WallexAPIError(
                "unexpected /v1/markets payload (no 'result.symbols' object)",
                endpoint="/v1/markets", body_snippet=str(payload)[:500],
            )
        return symbols

    def discover_markets(self, quote: Optional[str] = None) -> list[Market]:
        """Discover markets. When `quote` is given, filter to that quote.

        Uses the explicit ``baseAsset``/``quoteAsset`` fields (falling back
        to symbol splitting when they are absent). Returns a deterministic,
        sorted list of Market objects.
        """
        dst = quote.upper() if quote else None
        raw = self.markets()
        out: list[Market] = []
        for key, entry in raw.items():
            if not isinstance(entry, dict):
                log.debug("skipping markets entry with non-object value %r", key)
                continue
            symbol = str(entry.get("symbol") or key)
            base = str(entry.get("baseAsset") or "").strip().upper()
            q = str(entry.get("quoteAsset") or "").strip().upper()
            if not base or not q:
                try:
                    ft = wallex_to_freqtrade(symbol)
                except Exception as exc:  # noqa: BLE001 - resilient to odd pairs
                    log.debug("skipping symbol %r: %s", symbol, exc)
                    continue
                base, q = ft.split("/")
            ft_symbol = f"{base}/{q}"
            if dst and q != dst:
                continue
            stats = entry.get("stats") if isinstance(entry.get("stats"), dict) else {}
            market = Market(
                symbol=symbol.upper(),
                base=base,
                quote=q,
                ft_symbol=ft_symbol,
                active=True,  # the reference documents no active/disabled flag
                price=_safe_float(stats.get("lastPrice")),
                volume_base=_safe_float(stats.get("24h_volume")),
                volume_quote=_safe_float(stats.get("24h_quoteVolume")),
                day_change_pct=_safe_float(stats.get("24h_ch")),
                info={
                    k: stats.get(k)
                    for k in ("bidPrice", "askPrice", "24h_highPrice", "24h_lowPrice")
                },
            )
            out.append(market)
        out.sort(key=lambda m: m.symbol)
        return out

    def history_page(
        self,
        symbol: str,
        resolution: str,
        start_ts: int,
        end_ts: int,
    ) -> list[Candle]:
        """Fetch ONE response window from /v1/udf/history.

        Returns candles in API order (possibly empty — empty means no data
        in [start, end), never an error here; the range walker treats it as
        the end of data). Raises WallexNoData on ``s: no_data``.
        """
        params = {
            "symbol": symbol,
            "resolution": resolution,
            "from": int(start_ts),
            "to": int(end_ts),
        }
        payload = self._get("/v1/udf/history", params)
        ctx = f"symbol={symbol} res={resolution} from={start_ts} to={end_ts}"
        return parse_history_payload(payload, ctx=ctx)

    def history_range(
        self,
        symbol: str,
        resolution: str,
        start_ts: int,
        end_ts: int,
        progress_cb: Optional[Callable[[int, int], None]] = None,
        stop_event: Optional[threading.Event] = None,
    ) -> list[Candle]:
        """Fetch the full [start_ts, end_ts) range with cursor pagination.

        The reference documents no ``page`` parameter and no row cap, so the
        walker advances ``from`` past the last received candle::

          1. request [cursor, end_ts)   (cursor starts at start_ts)
          2. empty response / no_data -> done (no data remains in range)
          3. last_ts >= end_ts - 1 -> done (range fully covered)
          4. otherwise advance cursor to last_ts + 1 and continue
          5. boundary rows re-fetched across pages are deduplicated by ts
          6. STALL (no forward progress) or max_requests -> hard error
             (never an infinite loop, never a silent truncation)

        Returns candles sorted ascending by ts, deduplicated (last wins).
        """
        if end_ts <= start_ts:
            raise WallexError(
                f"invalid range: end ({end_ts}) must be after start ({start_ts})",
                endpoint="/v1/udf/history",
            )
        by_ts: dict[int, Candle] = {}
        cursor = int(start_ts)
        end = int(end_ts)
        made = 0
        while True:
            if stop_event is not None and stop_event.is_set():
                raise WallexError(
                    "cancelled", endpoint="/v1/udf/history",
                    params={"symbol": symbol},
                )
            if made >= self.max_requests:
                raise WallexError(
                    f"range walk exceeded max_requests={self.max_requests} "
                    f"(symbol={symbol} res={resolution} cursor={cursor} end={end}); "
                    f"widen max_requests explicitly, data is NOT silently truncated",
                    endpoint="/v1/udf/history",
                    params={"symbol": symbol, "resolution": resolution},
                )
            try:
                batch = self.history_page(symbol, resolution, cursor, end)
            except WallexNoData:
                break
            made += 1
            if not batch:
                break
            for c in batch:
                by_ts[int(c.ts)] = c
            if progress_cb:
                progress_cb(len(by_ts), made)
            last_ts = max(by_ts)
            if last_ts >= end - 1:
                break
            if last_ts < cursor:
                raise WallexError(
                    f"range walk stalled: server returned only candles older than "
                    f"cursor={cursor} (symbol={symbol} res={resolution}); refusing "
                    f"to loop forever",
                    endpoint="/v1/udf/history",
                    params={"symbol": symbol, "resolution": resolution},
                )
            cursor = last_ts + 1
        return [by_ts[ts] for ts in sorted(by_ts)]

    def close(self) -> None:
        self.session.close()


def _retry_after_seconds(resp: requests.Response, default: float = 5.0) -> float:
    """Best-effort Retry-After: header first, then JSON body hints."""
    try:
        headers = getattr(resp, "headers", None) or {}
        header = headers.get("Retry-After")
        if header is not None:
            return max(0.0, float(header))
    except (TypeError, ValueError):
        pass
    try:
        payload = resp.json()
    except ValueError:
        return default
    if isinstance(payload, dict):
        for key in ("retry_after", "retryAfter", "backOff", "back_off"):
            try:
                if payload.get(key) is not None:
                    return max(0.0, float(payload.get(key)))
            except (TypeError, ValueError):
                continue
    return default


def _safe_float(v) -> float:
    try:
        if v is None:
            return 0.0
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def resolution_for(tf: str) -> str:
    """Convenience: freqtrade timeframe -> wallex resolution."""
    return to_wallex_resolution(tf)
