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


# --------------------------------------------------------------------------
# Canonical timeframe-list normalization (THE boundary for every input form)
# --------------------------------------------------------------------------

_TF_ITEM_RE = re.compile(r"^\d+[mhdw]$")


def normalize_timeframes(value: object, *, param_name: str = "timeframes") -> list[str]:
    """Normalize any timeframe input to a canonical ``list[str]``.

    This is the SINGLE boundary every caller (CLI, GUI jobs, download
    request, tests) must use. Rules:

    * ``"5m"``                 -> ``["5m"]``
    * ``"5m,15m,1h"``          -> ``["5m", "15m", "1h"]``
    * ``"5m 15m 1h"``          -> ``["5m", "15m", "1h"]``  (shells may
      space-join what the user typed as a comma list, e.g. PowerShell's
      comma-array expansion)
    * ``["5m", "15m"]`` / ``("5m", "15m")`` / any iterable of strings
      -> normalized, deduplicated, order preserved
    * whitespace is trimmed; comma/semicolon/space all separate items
    * a string is NEVER iterated character-by-character — ``"1d"`` can
      only ever become ``["1d"]``, never ``["1", "d"]``
    * every item must be a valid, Nobitex-supported timeframe, otherwise
      ``TimeframeError`` is raised with the raw received value so the
      problem is visible at a glance
    """
    # -- collect candidate items without ever iterating a str for characters
    items: list[str] = []

    def _add_token(token: object) -> None:
        if isinstance(token, (list, tuple, set, frozenset)):
            for sub in token:
                _add_token(sub)
            return
        if not isinstance(token, str):
            raise TimeframeError(
                f"invalid timeframe item {token!r} in {param_name} "
                f"(expected strings like '5m', '15m', '1h', '4h', '1d')"
            )
        # explicit delimiters only — never a bare character iteration
        for part in re.split(r"[,;\s]+", token.strip()):
            if part:
                items.append(part.lower())

    if isinstance(value, str):
        _add_token(value)
    elif isinstance(value, (list, tuple, set, frozenset)):
        for v in value:
            _add_token(v)
    else:
        raise TimeframeError(
            f"invalid {param_name} value {value!r} "
            f"(expected a string like '5m,15m,1h,4h,1d' or a list of timeframes)"
        )

    # -- dedupe, preserve order
    seen: set[str] = set()
    out: list[str] = []
    for t in items:
        if t not in seen:
            seen.add(t)
            out.append(t)

    # -- validate every item (clear, actionable errors)
    bad = [t for t in out if not _TF_ITEM_RE.match(t)]
    if bad:
        raise TimeframeError(
            f"invalid timeframe {bad[0]!r} in {param_name}={out!r}\n"
            f"  each item must look like 5m / 15m / 1h / 4h / 1d "
            f"(number + m/h/d)\n"
            f"  NOTE: if you typed --timeframes 5m,15m,1h,4h,1d in PowerShell "
            f"without quotes,\n"
            f"        quote the whole list: --timeframes \"5m,15m,1h,4h,1d\""
        )
    unsupported = [t for t in out if t not in NOBITEX_RESOLUTIONS]
    if unsupported:
        raise TimeframeError(
            f"timeframe {unsupported[0]!r} is not supported by Nobitex "
            f"(got {param_name}={out!r})\n"
            f"  supported: {', '.join(SUPPORTED_TIMEFRAMES)}"
        )
    if not out:
        raise TimeframeError(f"no timeframes given in {param_name}={value!r}")
    return out


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
