"""Minimal, honest client for the AZBit public market-data API.

Implemented against the official public documentation:

  * ``GET /api/ohlc?interval=&currencyPairCode=&start=&end=`` -- OHLCV
    candles (https://data.azbit.com/docs/ and
    https://docs.azbit.com/docs/spot/tickers/)
  * ``GET /api/currencies/pairs`` -- reference pair listing (Reference data)
  * ``GET /api/tickers?currencyPairCode=`` -- 24h tickers (market discovery
    fallback; documented shape)

Base URL is ``https://data.azbit.com`` per the docs. Override with the
environment variable ``AZBIT_API_BASE`` (used by tests to point at the
bundled mock server; on real usage leave it unset).

Only public endpoints are implemented. No authentication is used or needed.
No private API calls exist in this module (by design).

Documented observations / assumptions (see docs/AZBIT.md for the full
matrix — anything marked ASSUMPTION there is re-stated at the use site):

  * ``/api/ohlc`` answers a bare JSON **array** of
    ``{date, open, max, min, close, volume, volumeBase}`` (docs example).
  * ``date`` is ISO-8601. The docs show both offset-less
    (``"2021-02-05T14:00:00"``) and ``Z``-suffixed with millis
    (``"2024-08-14T08:45:22.621Z"``) forms. Offset-less values are assumed
    UTC (ASSUMPTION — exchange convention; flagged in docs/AZBIT.md).
  * Sub-second fractions are **truncated** (never rounded) when stored as
    unix seconds; the raw string is kept on the candle for diagnostics.
    This is a resolution reduction, NOT a bucket alignment: floating
    timestamps stay floating and the validator still flags irregular
    spacing. No ``floor(ts/300)*300``-style alignment is ever applied.
  * A real query (2024-06-01..2024-06-06, minutes5, BTC_USDT) returned
    exactly 1000 rows for the first window and 430 for the remainder, so
    the endpoint caps responses at ~1000 rows (OBSERVED, undocumented —
    ``page_cap`` is configurable and the fetcher never trusts a single
    response to cover the range).
  * ``start`` is prefix-skipping: a query starting mid-gap returns the
    first available candle *after* ``start`` (OBSERVED: start 12:10:00 ->
    first row 12:11:43). An empty array therefore means "no data in
    [start, end) at all" and terminates pagination.
  * Timestamps are NOT guaranteed on the standard grid (OBSERVED:
    00:00:59, 00:06:37, ... with gaps up to 7731s). The client stores
    them verbatim; grid conformance is the validator's job to REPORT,
    never the client's job to FABRICATE.
"""
from __future__ import annotations

import logging
import os
import re
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable, Optional

import requests

from .ratelimit import RateLimiter as _RateLimiter  # noqa: F401 (compat alias)
from .symbols import Market, azbit_to_freqtrade

DEFAULT_BASE_URL = "https://data.azbit.com"

log = logging.getLogger("azbit.client")

# Conservative client-side rate limits (requests per second) per endpoint.
# AZBit documents 30 rps only for *signed trading* endpoints; anonymous
# endpoints "have their own stated limits" (not published for /api/ohlc),
# so we stay gentle and back off on 429.
DEFAULT_ENDPOINT_RPS = {
    "/api/ohlc": 5.0,
    "/api/tickers": 2.0,
    "/api/currencies/pairs": 2.0,
}

# Observed (undocumented) per-response row cap on /api/ohlc.
DEFAULT_PAGE_CAP = 1000

# Hard bound on paginated range requests: a fetcher must terminate.
DEFAULT_MAX_REQUESTS = 2000


class AzbitError(Exception):
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


class AzbitAPIError(AzbitError):
    """The API answered with a logical failure or malformed payload."""


class AzbitRateLimitedError(AzbitAPIError):
    """HTTP 429; honour `retry_after` seconds before retrying."""

    def __init__(self, *args, retry_after: float = 5.0, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.retry_after = float(retry_after)


@dataclass
class Candle:
    """One OHLCV candle.

    `ts` is the candle timestamp as unix seconds (UTC), truncated from the
    raw ISO ``date`` (sub-second fractions are dropped, never rounded).
    `raw_date` keeps the verbatim API string for diagnostics; `volume_base`
    carries the auxiliary ``volumeBase`` field (quote-currency volume —
    NEVER substituted for ``volume``).
    """

    ts: int
    open: float
    high: float
    low: float
    close: float
    volume: float
    volume_base: float = 0.0
    raw_date: str = ""


def _f(value: object, field_name: str, ctx: str) -> float:
    """Strict numeric conversion: null/missing/non-numeric is an error."""
    if value is None or isinstance(value, bool):
        raise AzbitAPIError(
            f"malformed candle: field {field_name!r} is null/missing ({ctx})",
            code="MalformedCandle",
        )
    try:
        out = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError) as exc:
        raise AzbitAPIError(
            f"malformed numeric value in candle field {field_name!r}: {value!r} ({ctx})",
            code="MalformedCandle",
        ) from exc
    if out != out or out in (float("inf"), float("-inf")):  # NaN / Inf
        raise AzbitAPIError(
            f"non-finite value in candle field {field_name!r}: {value!r} ({ctx})",
            code="MalformedCandle",
        )
    return out


_ISO_Z_RE = re.compile(r"Z$", re.IGNORECASE)


def parse_azbit_date(value: object, ctx: str = "") -> int:
    """Parse an AZBit ``date`` value to unix seconds (UTC).

    Accepts ISO-8601 strings (with/without offset, with/without
    fractional seconds) and unix timestamps (seconds, or millis when the
    magnitude requires it). Offset-less strings are assumed UTC
    (documented assumption). Values outside the plausible era
    (2009-01-01 .. 2040-01-01) are rejected.
    """
    if isinstance(value, bool):
        raise AzbitAPIError(f"malformed candle date {value!r} ({ctx})", code="MalformedCandle")
    if isinstance(value, (int, float)):
        ts = float(value)
        if ts > 1e12:  # millis
            ts /= 1000.0
        elif ts > 1e15:  # micros (defensive)
            ts /= 1_000_000.0
        ts_int = int(ts)  # truncate, never round
        _check_era(ts_int, value, ctx)
        return ts_int
    if not isinstance(value, str) or not value.strip():
        raise AzbitAPIError(f"malformed candle date {value!r} ({ctx})", code="MalformedCandle")
    text = value.strip()
    # Python's fromisoformat (3.11+) handles offsets but not a trailing 'Z'.
    text = _ISO_Z_RE.sub("+00:00", text)
    try:
        dt = datetime.fromisoformat(text)
    except ValueError as exc:
        raise AzbitAPIError(
            f"malformed candle date {value!r} ({ctx})", code="MalformedCandle"
        ) from exc
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)  # documented assumption: UTC
    ts_int = int(dt.timestamp())  # truncate sub-second, never round
    _check_era(ts_int, value, ctx)
    return ts_int


def _check_era(ts: int, value: object, ctx: str) -> None:
    if ts < 1_231_006_800 or ts > 2_208_988_800:  # 2009-01-01 .. 2040-01-01
        raise AzbitAPIError(
            f"candle date outside plausible era: {value!r} ({ctx})", code="MalformedCandle"
        )


def parse_ohlc_payload(payload: object, ctx: str = "") -> list[Candle]:
    """Parse an ``/api/ohlc`` response into Candle objects.

    Documented shape: a bare JSON array of
    ``{date, open, max, min, close, volume, volumeBase}``. An empty array
    is valid and means "no data in range". Dict wrappers (``data`` /
    ``candles`` / ``result`` keys) are accepted defensively; anything else
    raises. Row order is preserved verbatim (callers sort).
    """
    rows: object = payload
    if isinstance(payload, dict):
        if "Code" in payload and "Message" in payload:
            raise AzbitAPIError(
                f"API error {payload.get('Code')}: {payload.get('Message')} ({ctx})",
                code=str(payload.get("Code", "")),
                body_snippet=str(payload),
            )
        for key in ("data", "candles", "result", "items"):
            if isinstance(payload.get(key), list):
                rows = payload[key]
                break
        else:
            raise AzbitAPIError(
                f"unexpected /api/ohlc object payload without a row list ({ctx})",
                body_snippet=str(payload),
            )
    if not isinstance(rows, list):
        raise AzbitAPIError(
            f"unexpected non-array /api/ohlc payload ({ctx})", body_snippet=str(payload)
        )
    out: list[Candle] = []
    for i, row in enumerate(rows):
        if not isinstance(row, dict):
            raise AzbitAPIError(
                f"malformed candle row #{i}: not an object ({ctx})", code="MalformedCandle"
            )
        rctx = f"{ctx} row#{i}"
        if "date" not in row:
            raise AzbitAPIError(
                f"malformed candle row #{i}: missing 'date' ({ctx})", code="MalformedCandle"
            )
        out.append(
            Candle(
                ts=parse_azbit_date(row.get("date"), rctx),
                open=_f(row.get("open"), "open", rctx),
                high=_f(row.get("max"), "max", rctx),
                low=_f(row.get("min"), "min", rctx),
                close=_f(row.get("close"), "close", rctx),
                volume=_f(row.get("volume"), "volume", rctx),
                volume_base=_f(row.get("volumeBase", 0.0), "volumeBase", rctx)
                if row.get("volumeBase") is not None
                else 0.0,
                raw_date=str(row.get("date")),
            )
        )
    return out


def _dt_to_azbit(ts: int) -> str:
    """Unix seconds -> AZBit ``start``/``end`` format (UTC, no offset)."""
    return datetime.fromtimestamp(int(ts), tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")


class AzbitClient:
    """Public market-data client with retry, backoff and rate limiting.

    All methods raise AzbitError subclasses with full request context.
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
        page_cap: int = DEFAULT_PAGE_CAP,
        max_requests: int = DEFAULT_MAX_REQUESTS,
    ) -> None:
        self.base_url = (base_url or os.environ.get("AZBIT_API_BASE") or DEFAULT_BASE_URL).rstrip(
            "/"
        )
        self.timeout = timeout
        self.max_retries = max_retries
        self._limiter = _RateLimiter(rate_limit_rps or DEFAULT_ENDPOINT_RPS)
        self.session = session or requests.Session()
        self._sleep = sleep
        self.page_cap = int(page_cap)
        self.max_requests = int(max_requests)
        self.request_count = 0

    # ------------------------------------------------------------------ http
    def _get(self, path: str, params: Optional[dict] = None, _retry: int = 0) -> object:
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
            raise AzbitError(
                f"request failed after {_retry} retries: {exc}",
                endpoint=path, params=params,
            ) from exc

        self.request_count += 1

        if resp.status_code == 429:
            retry_after = _retry_after_seconds(resp, default=5.0)
            err = AzbitRateLimitedError(
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
            raise AzbitError(
                f"server error {resp.status_code} on {path}",
                endpoint=path, params=params, http_status=resp.status_code,
                body_snippet=resp.text[:200],
            )

        if resp.status_code >= 400:
            raise AzbitError(
                f"client error {resp.status_code} on {path}: {resp.text[:200]}",
                endpoint=path, params=params, http_status=resp.status_code,
                body_snippet=resp.text[:200],
            )

        payload = self._safe_json(resp)
        if isinstance(payload, dict) and "Code" in payload and "Message" in payload:
            # AZBit error envelope on HTTP 200 — never mistake it for data.
            raise AzbitAPIError(
                f"API error {payload.get('Code')}: {payload.get('Message')}",
                endpoint=path, params=params, http_status=resp.status_code,
                code=str(payload.get("Code", "")), body_snippet=resp.text[:200],
            )
        return payload

    @staticmethod
    def _safe_json(resp: requests.Response) -> object:
        try:
            return resp.json()
        except ValueError:
            return {}

    # ------------------------------------------------------------- endpoints
    def ohlc_page(
        self,
        currency_pair_code: str,
        interval: str,
        start_ts: int,
        end_ts: int,
    ) -> list[Candle]:
        """Fetch ONE response (<=page_cap rows) from ``GET /api/ohlc``.

        Returns candles in API order (possibly empty — empty means no data
        in [start, end), never an error here; the range walker treats it as
        the end of data).
        """
        params = {
            "interval": interval,
            "currencyPairCode": currency_pair_code,
            "start": _dt_to_azbit(start_ts),
            "end": _dt_to_azbit(end_ts),
        }
        payload = self._get("/api/ohlc", params)
        ctx = (
            f"pair={currency_pair_code} interval={interval} "
            f"start={params['start']} end={params['end']}"
        )
        return parse_ohlc_payload(payload, ctx=ctx)

    def ohlc_range(
        self,
        currency_pair_code: str,
        interval: str,
        start_ts: int,
        end_ts: int,
        progress_cb: Optional[Callable[[int, int], None]] = None,
        stop_event: Optional[threading.Event] = None,
    ) -> list[Candle]:
        """Fetch the full [start_ts, end_ts) range with cursor pagination.

        Algorithm (deterministic, bounded):
          1. request [cursor, end_ts)   (cursor starts at start_ts)
          2. empty response -> done (the API skips empty prefixes, so empty
             means no data remains in the range)
          3. short response (< page_cap rows) -> done (range fully covered)
          4. full response -> advance cursor to last_ts + 1s and continue
          5. boundary rows re-fetched across pages are deduplicated by ts
          6. STALL (no forward progress) or max_requests -> hard error
             (never an infinite loop, never a silent truncation)

        Returns candles sorted ascending by ts, deduplicated (last wins).
        """
        if end_ts <= start_ts:
            raise AzbitError(
                f"invalid range: end ({end_ts}) must be after start ({start_ts})",
                endpoint="/api/ohlc",
            )
        out: list[Candle] = []
        seen: set[int] = set()
        cursor = int(start_ts)
        requests = 0
        while cursor < int(end_ts):
            if stop_event is not None and stop_event.is_set():
                raise AzbitError("cancelled", endpoint="/api/ohlc",
                                 params={"currencyPairCode": currency_pair_code})
            if requests >= self.max_requests:
                raise AzbitAPIError(
                    f"pagination exceeded max_requests={self.max_requests} for "
                    f"{currency_pair_code} {interval} "
                    f"({_dt_to_azbit(start_ts)}..{_dt_to_azbit(end_ts)}); "
                    f"narrow the range instead of silently truncating",
                    endpoint="/api/ohlc", code="MaxRequests",
                )
            batch = self.ohlc_page(currency_pair_code, interval, cursor, int(end_ts))
            requests += 1
            if not batch:
                break  # no data in [cursor, end): done
            fresh = 0
            max_ts = cursor - 1
            for c in batch:
                if c.ts > max_ts:
                    max_ts = c.ts
                if c.ts not in seen:
                    seen.add(c.ts)
                    out.append(c)
                    fresh += 1
            if progress_cb:
                progress_cb(len(out), requests)
            if len(batch) < self.page_cap:
                break  # short page: the range is fully covered
            if max_ts < cursor:
                # Full-size page with no forward progress: the endpoint is
                # stuck returning the same rows -> fail loudly, never loop.
                raise AzbitAPIError(
                    f"pagination stalled on {currency_pair_code} {interval}: "
                    f"request #{requests} returned {len(batch)} rows but none "
                    f"at/after cursor {_dt_to_azbit(cursor)}",
                    endpoint="/api/ohlc", code="PaginationStalled",
                )
            cursor = max_ts + 1
        out.sort(key=lambda c: c.ts)
        # boundary duplicates across pages: keep the LAST occurrence
        deduped: list[Candle] = []
        seen2: set[int] = set()
        for c in reversed(out):
            if c.ts not in seen2:
                seen2.add(c.ts)
                deduped.append(c)
        deduped.reverse()
        return deduped

    def discover_markets(self, quote: Optional[str] = None) -> list[Market]:
        """Discover markets, optionally filtered to a quote asset.

        Primary: ``GET /api/currencies/pairs`` (reference data); fallback:
        ``GET /api/tickers`` (documented shape). Returns a deterministic,
        sorted list of Market objects. HTTP failures raise; an empty result
        means "no pairs matched" (e.g. an unknown quote filter).
        """
        pairs = self._pairs_via_reference()
        tickers_by_code: dict[str, dict] = {}
        if pairs is None:
            log.info("/api/currencies/pairs unavailable; falling back to /api/tickers")
            pairs = []
            for t in self._tickers():
                code = str(t.get("currencyPairCode", "")).strip()
                if code:
                    pairs.append({"currencyPairCode": code, "isActive": True})
                    tickers_by_code[code.upper()] = t
        else:
            try:
                for t in self._tickers():
                    code = str(t.get("currencyPairCode", "")).strip()
                    if code:
                        tickers_by_code[code.upper()] = t
            except AzbitError as exc:
                log.debug("ticker enrichment skipped: %s", exc)
        dst = quote.upper() if quote else None
        out: list[Market] = []
        for entry in pairs:
            code = _pair_code(entry)
            if not code:
                log.debug("skipping pair entry without a code: %r", entry)
                continue
            try:
                ft = azbit_to_freqtrade(code)
            except Exception as exc:  # noqa: BLE001 - resilient to odd pairs
                log.debug("skipping pair %r: %s", code, exc)
                continue
            base, _, q = ft.partition("/")
            if dst and q.upper() != dst:
                continue
            active = _pair_active(entry)
            tick = tickers_by_code.get(code.upper(), {})
            out.append(
                Market(
                    symbol=code.upper(),
                    base=base,
                    quote=q,
                    ft_symbol=ft,
                    active=active,
                    price=_safe_float(tick.get("price")),
                    volume_base=0.0,
                    # BEST-EFFORT (documented): ticker `volume24h` has no
                    # documented unit; treated as quote-volume for ranking
                    # only. Raw values stay in `info`.
                    volume_quote=_safe_float(tick.get("volume24h")),
                    day_change_pct=_safe_float(tick.get("priceChangePercentage24h")),
                    info={
                        "price24hAgo": tick.get("price24hAgo"),
                        "bidPrice": tick.get("bidPrice"),
                        "askPrice": tick.get("askPrice"),
                        "low24h": tick.get("low24h"),
                        "high24h": tick.get("high24h"),
                        "volume24h_raw": tick.get("volume24h"),
                    },
                )
            )
        out.sort(key=lambda m: m.symbol)
        return out

    def _pairs_via_reference(self) -> Optional[list[dict]]:
        """GET /api/currencies/pairs -> list of pair dicts, or None when the
        endpoint itself is unavailable (fallback to tickers)."""
        try:
            payload = self._get("/api/currencies/pairs")
        except AzbitError as exc:
            log.debug("/api/currencies/pairs failed (%s); will use tickers", exc)
            return None
        if isinstance(payload, dict):
            for key in ("data", "pairs", "result", "items"):
                if isinstance(payload.get(key), list):
                    payload = payload[key]
                    break
        if not isinstance(payload, list):
            log.debug("/api/currencies/pairs: unexpected shape; will use tickers")
            return None
        return [e for e in payload if isinstance(e, dict)]

    def _tickers(self) -> list[dict]:
        payload = self._get("/api/tickers")
        if isinstance(payload, dict):
            for key in ("data", "tickers", "result", "items"):
                if isinstance(payload.get(key), list):
                    payload = payload[key]
                    break
        if not isinstance(payload, list):
            raise AzbitAPIError(
                "unexpected /api/tickers payload (no row list)",
                endpoint="/api/tickers",
            )
        return [t for t in payload if isinstance(t, dict)]

    def close(self) -> None:
        self.session.close()


def _retry_after_seconds(resp: requests.Response, default: float = 5.0) -> float:
    try:
        raw = (resp.headers or {}).get("Retry-After")
        if raw is not None:
            return max(0.0, float(str(raw).strip()))
    except (TypeError, ValueError):
        pass
    try:
        payload = resp.json()
        if isinstance(payload, dict):
            for key in ("retryAfter", "retry_after", "backOff", "backoff"):
                if payload.get(key) is not None:
                    return max(0.0, float(payload[key]))
    except (ValueError, TypeError):
        pass
    return default


def _pair_code(entry: dict) -> str:
    for key in ("currencyPairCode", "code", "pair", "pairCode", "symbol"):
        val = entry.get(key)
        if isinstance(val, str) and val.strip():
            return val.strip()
    return ""


def _pair_active(entry: dict) -> bool:
    for key in ("isActive", "active", "isEnabled", "enabled", "tradable"):
        if key in entry:
            return bool(entry[key])
    status = entry.get("status")
    if isinstance(status, str):
        return status.strip().lower() in ("active", "trading", "enabled", "online", "listed")
    return True  # listed with no status flag: assume tradeable (documented)


def _safe_float(v: object) -> float:
    try:
        if v is None:
            return 0.0
        return float(v)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0.0
