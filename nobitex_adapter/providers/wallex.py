"""Wallex provider: the WallexClient behind the provider contract."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from ..symbols import freqtrade_to_wallex, wallex_to_freqtrade
from ..timeframes import WALLEX_SUPPORTED_TIMEFRAMES, to_wallex_resolution
from ..wallex_client import DEFAULT_BASE_URL, WallexClient
from .base import DepthInfo, ExchangeProvider


class WallexProvider(ExchangeProvider):
    """ExchangeProvider implementation for Wallex (public API only)."""

    name = "wallex"
    display_name = "Wallex"
    default_base_url = DEFAULT_BASE_URL

    def __init__(self, client: Optional[WallexClient] = None, **client_kwargs) -> None:
        self.client = client or WallexClient(**client_kwargs)

    @property
    def supported_timeframes(self) -> tuple[str, ...]:
        return WALLEX_SUPPORTED_TIMEFRAMES

    def to_exchange_symbol(self, pair: str) -> str:
        return freqtrade_to_wallex(pair)

    def to_ft_symbol(self, exchange_symbol: str) -> str:
        return wallex_to_freqtrade(exchange_symbol)

    def to_exchange_timeframe(self, tf: str) -> str:
        return to_wallex_resolution(tf)

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

        The client's ``history_range`` walks the window internally (cursor =
        last_ts + 1, boundary dedupe, stall/max-request guards — Wallex
        documents no page parameter and no row cap). An empty window is
        recorded in ``empty_notes`` with its exact request context for
        zero-data diagnostics.
        """
        sym = self.to_exchange_symbol(pair)
        resolution = self.to_exchange_timeframe(tf)
        out = self.client.history_range(sym, resolution, int(start_ts), int(end_ts))
        if not out and empty_notes is not None:
            empty_notes.append(
                f"empty symbol={sym} res={resolution} "
                f"from={start_ts} to={end_ts}"
            )
        return out

    def discover_depth(self, pair: str, tf: str) -> DepthInfo:
        """Earliest/latest candles via real probes (floors verified, not trusted).

        Strategy (same as the Nobitex sibling): ONE wide single-window
        request [2015-01-01 .. now]; its earliest row is the first available
        candle (UDF windows answer oldest-first; the cap truncates the NEW
        end, never the old end — verified by live depth runs). One narrow
        recent window gives the latest row. Single windows only: a
        cursor-walked full-history scan would cost thousands of requests.
        """
        from ..timeframes import parse_timeframe
        from ..wallex_client import WallexNoData

        info = DepthInfo(pair=pair, timeframe=tf)
        before = self.client.request_count
        sym = self.to_exchange_symbol(pair)
        resolution = self.to_exchange_timeframe(tf)
        tf_secs = parse_timeframe(tf).seconds
        now_ts = int(datetime.now(timezone.utc).timestamp())
        floor = 1_420_070_400  # 2015-01-01 (safely before any Wallex data)
        try:
            wide = self.client.history_page(sym, resolution, floor, now_ts)
            if wide:
                info.earliest_ts = int(min(c.ts for c in wide))
                info.notes.append(
                    f"wide probe [{floor}..now]: {len(wide)} rows, "
                    f"earliest={info.earliest_ts}"
                )
            else:
                info.notes.append("wide probe returned no rows")
        except WallexNoData:
            info.notes.append("wide probe: no_data (no candles at all in [2015..now])")
        except Exception as exc:  # noqa: BLE001 - depth probes never raise
            info.notes.append(f"wide probe failed: {exc}")
        try:
            recent = self.client.history_page(
                sym, resolution, now_ts - 10 * tf_secs, now_ts
            )
            if recent:
                info.latest_ts = int(max(c.ts for c in recent))
            else:
                info.notes.append("recent 10-candle window is empty")
        except WallexNoData:
            info.notes.append("recent 10-candle window: no_data")
        except Exception as exc:  # noqa: BLE001 - depth probes never raise
            info.notes.append(f"recent probe failed: {exc}")
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
            f"   1. Wallex has no candles for this pair/timeframe in that range "
            f"(pair listed later, delisted, or sparse history)\n"
            f"   2. the pair symbol is wrong (list pairs with "
            f"'--exchange wallex markets')\n"
            f"   3. an API range limitation\n"
            f"  diagnose with the quality probe (public endpoint only):\n"
            f"   python -m nobitex_adapter --exchange wallex probe --pair {pair} "
            f"--timeframes {tf} --start {req_start} "
            f"--end {req_end}"
        )

    @property
    def request_count(self) -> int:
        return self.client.request_count

    def close(self) -> None:
        self.client.close()
