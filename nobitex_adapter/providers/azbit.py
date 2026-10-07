"""AZBit provider: the AZBitClient behind the provider contract."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from ..azbit_client import DEFAULT_BASE_URL, AzbitClient, AzbitError
from ..symbols import azbit_to_freqtrade, freqtrade_to_azbit
from ..timeframes import AZBIT_SUPPORTED_TIMEFRAMES, to_azbit_interval
from .base import DepthInfo, ExchangeProvider


class AzbitProvider(ExchangeProvider):
    """ExchangeProvider implementation for AZBit (public API only)."""

    name = "azbit"
    display_name = "AZBit"
    default_base_url = DEFAULT_BASE_URL

    def __init__(self, client: Optional[AzbitClient] = None, **client_kwargs) -> None:
        self.client = client or AzbitClient(**client_kwargs)

    @property
    def supported_timeframes(self) -> tuple[str, ...]:
        return AZBIT_SUPPORTED_TIMEFRAMES

    def to_exchange_symbol(self, pair: str) -> str:
        return freqtrade_to_azbit(pair)

    def to_ft_symbol(self, exchange_symbol: str) -> str:
        return azbit_to_freqtrade(exchange_symbol)

    def to_exchange_timeframe(self, tf: str) -> str:
        return to_azbit_interval(tf)

    def discover_markets(self, quote: Optional[str] = None) -> list:
        return self.client.discover_markets(quote=quote)

    def fetch_window(
        self,
        pair: str,
        tf: str,
        start_ts: int,
        end_ts: int,
        *,
        empty_notes: Optional[list[str]] = None,
    ) -> list:
        """Fetch ALL of [start_ts, end_ts) via cursor pagination.

        The client's ``ohlc_range`` walks the ~1000-row cap internally
        (cursor = last_ts + 1s, boundary dedupe, stall/max-request guards).
        An empty window is recorded in ``empty_notes`` with its exact
        request context for zero-data diagnostics.
        """
        code = self.to_exchange_symbol(pair)
        interval = self.to_exchange_timeframe(tf)
        out = self.client.ohlc_range(code, interval, int(start_ts), int(end_ts))
        if not out and empty_notes is not None:
            empty_notes.append(
                f"empty pair={code} interval={interval} "
                f"start={start_ts} end={end_ts}"
            )
        return out

    def discover_depth(self, pair: str, tf: str) -> DepthInfo:
        """Earliest/latest candles via real probes (no trusted floors).

        Strategy: ONE wide request [2015-01-01 .. now]. Because the API
        skips empty prefixes (observed), its first row is the earliest
        available candle — unless the API answers empty for a too-early
        start, in which case we bisect forward to the first non-empty
        window (bounded, ~8 probes). One narrow recent window gives the
        latest row.
        """
        from ..timeframes import parse_timeframe

        info = DepthInfo(pair=pair, timeframe=tf)
        before = self.client.request_count
        code = self.to_exchange_symbol(pair)
        interval = self.to_exchange_timeframe(tf)
        tf_secs = parse_timeframe(tf).seconds
        now_ts = int(datetime.now(timezone.utc).timestamp())
        floor = 1_420_070_400  # 2015-01-01 (safely before any AZBit data)

        first = self._safe_page(code, interval, floor, now_ts, info, "wide")
        if first:
            info.earliest_ts = int(min(c.ts for c in first))
            info.notes.append(
                f"wide probe [2015-01-01..now]: {len(first)} rows, "
                f"earliest={info.earliest_ts}"
            )
        else:
            info.notes.append("wide probe empty; bisecting forward for first data")
            earliest = self._bisect_earliest(code, interval, floor, now_ts, info)
            if earliest is not None:
                info.earliest_ts = earliest

        latest_rows = self._safe_page(
            code, interval, now_ts - 10 * tf_secs, now_ts, info, "recent"
        )
        if latest_rows:
            info.latest_ts = int(max(c.ts for c in latest_rows))
        else:
            info.notes.append("recent 10-candle window is empty")
        info.requests_made = self.client.request_count - before
        return info

    def _safe_page(
        self, code: str, interval: str, start_ts: int, end_ts: int,
        info: DepthInfo, label: str,
    ) -> list:
        try:
            return self.client.ohlc_page(code, interval, start_ts, end_ts)
        except AzbitError as exc:
            info.notes.append(f"{label} probe failed: {exc}")
            return []

    def _bisect_earliest(
        self, code: str, interval: str, lo: int, hi: int, info: DepthInfo,
    ) -> Optional[int]:
        """Find the first non-empty window by bisecting [lo, hi] forward.

        Invariant: [lo, hi) contains data (checked by the caller falling
        back only when recent data exists... here we simply probe: if even
        [hi-30d, hi) is empty there is no data at all). Bounded to 12
        probes, then a final exact fetch of the first non-empty day.
        """
        try:
            tail = self.client.ohlc_page(code, interval, hi - 30 * 86400, hi)
        except AzbitError as exc:
            info.notes.append(f"tail probe failed: {exc}")
            return None
        if not tail:
            info.notes.append("no data in the trailing 30 days either: pair/timeframe empty")
            return None
        # bisect: find the earliest day-boundary whose [mid, hi) is non-empty
        left, right = lo, hi
        for _ in range(12):
            if right - left <= 86400:
                break
            mid = left + (right - left) // 2
            try:
                probe = self.client.ohlc_page(code, interval, mid, hi)
            except AzbitError:
                break
            if probe:
                right = mid
            else:
                left = mid
        try:
            first_day = self.client.ohlc_page(code, interval, right, right + 86400)
        except AzbitError as exc:
            info.notes.append(f"first-day probe failed: {exc}")
            return None
        if not first_day:
            # [right, right+1d) empty but [right, hi) was not: fetch forward
            try:
                first_day = self.client.ohlc_page(code, interval, right, hi)
            except AzbitError as exc:
                info.notes.append(f"forward probe failed: {exc}")
                return None
        if not first_day:
            return None
        earliest = int(min(c.ts for c in first_day))
        info.notes.append(f"bisected earliest data to ts={earliest} (~day precision)")
        return earliest

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
        from ..downloader import data_ts_iso, end_ts_iso

        seen = empty_notes[-3:] if empty_notes else ["all chunk windows returned no data"]
        return (
            f"exchange returned ZERO candles for {pair} {tf} over "
            f"{data_ts_iso(data_start_ts)} .. {end_ts_iso(end_ts)}\n"
            f"  last empty responses: {' | '.join(seen)}\n"
            f"  possible causes:\n"
            f"   1. AZBit has no candles for this pair/timeframe in that range "
            f"(pair listed later, delisted, or sparse history)\n"
            f"   2. the pair code is wrong (list pairs with "
            f"'--exchange azbit markets')\n"
            f"   3. an API range limitation\n"
            f"  diagnose with the quality probe (public endpoint only):\n"
            f"   python -m nobitex_adapter --exchange azbit probe --pair {pair} "
            f"--timeframes {tf} --start {req_start} "
            f"--end {req_end}"
        )

    @property
    def request_count(self) -> int:
        return self.client.request_count

    def close(self) -> None:
        self.client.close()
