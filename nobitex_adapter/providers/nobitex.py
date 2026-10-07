"""Nobitex provider: the existing NobitexClient behind the provider contract."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from ..nobitex_client import DEFAULT_BASE_URL, NobitexClient, NobitexNoData
from ..symbols import freqtrade_to_nobitex, nobitex_to_freqtrade
from ..timeframes import SUPPORTED_TIMEFRAMES, to_nobitex_resolution
from .base import DepthInfo, ExchangeProvider

CANDLES_PER_PAGE = 500  # documented max per /market/udf/history response


class NobitexProvider(ExchangeProvider):
    """ExchangeProvider implementation for Nobitex (public API only)."""

    name = "nobitex"
    display_name = "Nobitex"
    default_base_url = DEFAULT_BASE_URL

    def __init__(self, client: Optional[NobitexClient] = None, **client_kwargs) -> None:
        self.client = client or NobitexClient(**client_kwargs)

    @property
    def supported_timeframes(self) -> tuple[str, ...]:
        return SUPPORTED_TIMEFRAMES

    def to_exchange_symbol(self, pair: str) -> str:
        return freqtrade_to_nobitex(pair)

    def to_ft_symbol(self, exchange_symbol: str) -> str:
        return nobitex_to_freqtrade(exchange_symbol)

    def to_exchange_timeframe(self, tf: str) -> str:
        return to_nobitex_resolution(tf)

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
        """Fetch [start_ts, end_ts) via <=500-candle pages.

        Every empty response is recorded in ``empty_notes`` with its exact
        request context (identical wording to the pre-provider downloader
        so zero-data diagnostics are unchanged).
        """
        sym = self.to_exchange_symbol(pair)
        resolution = self.to_exchange_timeframe(tf)
        out: list = []
        page = 1
        while True:
            try:
                batch = self.client.candles_page(
                    symbol=sym, resolution=resolution,
                    start_ts=start_ts, end_ts=end_ts, page=page,
                )
            except NobitexNoData:
                if empty_notes is not None:
                    empty_notes.append(
                        f"no_data symbol={sym} res={resolution} "
                        f"from={start_ts} to={end_ts} page={page}"
                    )
                break
            out.extend(batch)
            if len(batch) < CANDLES_PER_PAGE:
                break
            page += 1
        return out

    def discover_depth(self, pair: str, tf: str) -> DepthInfo:
        """Earliest/latest candles via real probes (floors verified, not trusted).

        Strategy: one wide request from a safely-early floor; its first row
        is the earliest available candle (the endpoint returns rows from
        `from` forward). One narrow recent window gives the latest row.
        """
        from ..timeframes import parse_timeframe

        info = DepthInfo(pair=pair, timeframe=tf)
        before = self.client.request_count
        sym = self.to_exchange_symbol(pair)
        resolution = self.to_exchange_timeframe(tf)
        interval = parse_timeframe(tf).seconds
        now_ts = int(datetime.now(timezone.utc).timestamp())
        floor = 1_420_070_400  # 2015-01-01 (safely before any Nobitex data)
        try:
            batch = self.client.candles_page(
                symbol=sym, resolution=resolution,
                start_ts=floor, end_ts=now_ts, page=1,
            )
            if batch:
                info.earliest_ts = int(min(c.ts for c in batch))
                info.notes.append(f"wide probe [{floor}..now] page 1: {len(batch)} rows")
            else:
                info.notes.append("wide probe returned no rows")
        except NobitexNoData:
            info.notes.append("wide probe: no_data (no candles at all in [2015..now])")
        try:
            recent = self.client.candles_page(
                symbol=sym, resolution=resolution,
                start_ts=now_ts - 10 * interval, end_ts=now_ts, page=1,
            )
            if recent:
                info.latest_ts = int(max(c.ts for c in recent))
        except NobitexNoData:
            info.notes.append("recent 10-candle window: no_data")
        info.requests_made = self.client.request_count - before
        return info

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
            f"   1. Nobitex minute-level (5m/15m) history for this pair "
            f"may be shorter than documented (minute candles are "
            f"documented only from ~2022-03-20)\n"
            f"   2. the pair did not trade in that range (list it with "
            f"'markets')\n"
            f"   3. an API range limitation\n"
            f"  diagnose with a raw probe (public endpoint only):\n"
            f"   python -m nobitex_adapter ohlcv-probe --pair {pair} "
            f"--timeframe {tf} --start {req_start} "
            f"--end {req_end}"
        )

    @property
    def request_count(self) -> int:
        return self.client.request_count

    def close(self) -> None:
        self.client.close()
