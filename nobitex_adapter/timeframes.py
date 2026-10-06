"""Timeframe handling: Freqtrade timeframes <-> Nobitex resolutions.

Nobitex `resolution` values (official API docs, nobitex/docs-api master,
source/includes/_market_data.md, endpoint GET /market/udf/history):

    minutes:  1, 5, 15, 30
    hours:    60, 180, 240, 360, 720
    days:     D, 2D, 3D

Freqtrade timeframes use e.g. ``5m``, ``1h``, ``4h``, ``1d``.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

# Nobitex resolution values, exactly as documented.
NOBITEX_RESOLUTIONS: dict[str, str] = {
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
}

# Reverse map: resolution -> timeframe
_RESOLUTION_TO_TF = {v: k for k, v in NOBITEX_RESOLUTIONS.items()}

_TF_RE = re.compile(r"^(\d+)([mhdw])$")

# Timeframes the adapter supports end-to-end (download + validation + backtest).
SUPPORTED_TIMEFRAMES = tuple(NOBITEX_RESOLUTIONS.keys())

# Timeframes required by NostalgiaForInfinityX8 (base + informative per pair)
X8_BASE_TIMEFRAME = "5m"
X8_INFORMATIVE_TIMEFRAMES = ("15m", "1h", "4h", "1d")
X8_BTC_INFO_TIMEFRAMES = ("4h",)

# Conservative warmup ("startup") candles prepended before the requested
# backtest start, per timeframe. X8 uses startup_candle_count=800 on the base
# 5m timeframe and up to EMA(200) on the 1d informative, so 1d needs a large
# lead-in. These defaults are intentionally generous; they only add data,
# they never remove data.
DEFAULT_STARTUP_CANDLES: dict[str, int] = {
    "5m": 850,
    "15m": 400,
    "1h": 400,
    "4h": 250,
    "1d": 260,
    "2d": 150,
    "3d": 120,
    "1m": 850,
    "30m": 400,
    "3h": 250,
    "6h": 200,
    "12h": 150,
}


class TimeframeError(ValueError):
    """Raised on unknown/unsupported timeframes or resolutions."""


@dataclass(frozen=True)
class Timeframe:
    """A parsed timeframe (freqtrade style)."""

    tf: str
    seconds: int

    @property
    def nobitex_resolution(self) -> str:
        try:
            return NOBITEX_RESOLUTIONS[self.tf]
        except KeyError as exc:
            raise TimeframeError(
                f"timeframe {self.tf!r} has no Nobitex resolution "
                f"(supported: {', '.join(SUPPORTED_TIMEFRAMES)})"
            ) from exc


def parse_timeframe(tf: str) -> Timeframe:
    """Parse a freqtrade-style timeframe string like ``5m`` into seconds."""
    if not isinstance(tf, str):
        raise TimeframeError(f"invalid timeframe: {tf!r}")
    tf = tf.strip().lower()
    m = _TF_RE.match(tf)
    if not m:
        raise TimeframeError(
            f"invalid timeframe {tf!r} (expected forms like 5m, 15m, 1h, 4h, 1d)"
        )
    amount, unit = int(m.group(1)), m.group(2)
    if amount < 1:
        raise TimeframeError(f"timeframe amount must be >= 1: {tf!r}")
    mult = {"m": 60, "h": 3600, "d": 86400, "w": 604800}[unit]
    return Timeframe(tf=tf, seconds=amount * mult)


def to_nobitex_resolution(tf: str) -> str:
    """Convert a freqtrade timeframe to the Nobitex resolution string."""
    t = parse_timeframe(tf)
    return t.nobitex_resolution


def from_nobitex_resolution(resolution: str) -> str:
    """Convert a Nobitex resolution to a freqtrade timeframe."""
    try:
        return _RESOLUTION_TO_TF[resolution]
    except KeyError as exc:
        raise TimeframeError(
            f"unknown Nobitex resolution {resolution!r} "
            f"(known: {', '.join(sorted(_RESOLUTION_TO_TF))})"
        ) from exc


def startup_candles(tf: str) -> int:
    """Default warmup candle count to prepend before a backtest start."""
    t = parse_timeframe(tf)
    return DEFAULT_STARTUP_CANDLES.get(t.tf, 200)
