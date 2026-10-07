"""Historical OHLCV validation.

Checks (per mission spec):
  1. timestamp monotonicity
  2. duplicate timestamps
  3. OHLC relationships (high >= max(o,c), low <= min(o,c), high >= low, > 0)
  4. volume validity (>= 0, finite)
  5. candle interval consistency (constant spacing == timeframe)
  6. missing periods (gaps inside the data range)
  7. timezone consistency (unix seconds -> UTC; rejects impossible values)
  8. first/last timestamp
  9. row count
 10. expected timeframe spacing (expected rows for the requested range)

Policy: NO silent repair. Duplicates may be removed ONLY when `repair=True`
is explicitly passed, and every repair is recorded in the report.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

import numpy as np
import pandas as pd

from .timeframes import parse_timeframe

REQUIRED_COLUMNS = ("date", "open", "high", "low", "close", "volume")


@dataclass
class ValidationReport:
    pair: str = ""
    timeframe: str = ""
    start: str = ""
    end: str = ""
    rows: int = 0
    expected_rows: int = 0
    duplicates: int = 0
    non_monotonic: int = 0
    invalid_ohlc: int = 0
    invalid_volume: int = 0
    missing_intervals: int = 0
    gap_ranges: list = field(default_factory=list)
    first_ts: str = ""
    last_ts: str = ""
    timezone: str = "UTC"
    candle_spacing_ok: bool = True
    incomplete_last_candle: bool = False
    repairs: list = field(default_factory=list)
    status: str = "PASS"
    problems: list = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.status == "PASS"

    @property
    def quality(self) -> str:
        """One-word data-quality verdict (gap-handling policy).

        CONTIGUOUS: every candle exactly one interval apart, no defects.
        GAPPED:     spacing defects (missing intervals / irregular spacing).
        DUPLICATE:  repeated timestamps (kept-first counting, pre-repair).
        OUT_OF_ORDER: non-monotonic timestamps.
        EMPTY:      no rows at all.
        INVALID:    broken OHLC/volume values (unusable regardless of shape).

        Priority is EMPTY > INVALID > OUT_OF_ORDER > DUPLICATE > GAPPED >
        CONTIGUOUS so the most severe defect wins; all counters stay
        visible alongside. Derived from counters only — `status` keeps its
        exact historical semantics (PASS / PASS_WITH_GAPS / FAIL / ...).
        """
        if self.rows == 0:
            return "EMPTY"
        if self.invalid_ohlc or self.invalid_volume:
            return "INVALID"
        if self.non_monotonic:
            return "OUT_OF_ORDER"
        if self.duplicates:
            return "DUPLICATE"
        if self.missing_intervals or not self.candle_spacing_ok:
            return "GAPPED"
        return "CONTIGUOUS"

    def render(self) -> str:
        lines = [
            f"PAIR: {self.pair}",
            f"TIMEFRAME: {self.timeframe}",
            f"START: {self.start}",
            f"END: {self.end}",
            f"ROWS: {self.rows}",
            f"EXPECTED ROWS: {self.expected_rows if self.expected_rows else 'n/a'}",
            f"DUPLICATES: {self.duplicates}",
            f"MISSING INTERVALS: {self.missing_intervals}",
            f"INVALID OHLC: {self.invalid_ohlc}",
            f"INVALID VOLUME: {self.invalid_volume}",
            f"NON-MONOTONIC: {self.non_monotonic}",
            f"FIRST: {self.first_ts}",
            f"LAST: {self.last_ts}",
            f"TIMEZONE: {self.timezone}",
        ]
        if self.gap_ranges:
            shown = self.gap_ranges[:5]
            lines.append(f"FIRST GAPS: {shown}" + (f" (+{len(self.gap_ranges) - 5} more)" if len(self.gap_ranges) > 5 else ""))
        if self.incomplete_last_candle:
            lines.append("INCOMPLETE LAST CANDLE: yes (open period at download end)")
        if self.repairs:
            lines.append(f"REPAIRED: {'; '.join(self.repairs)}")
        if self.problems:
            lines.append(f"PROBLEMS: {'; '.join(self.problems)}")
        lines.append(f"QUALITY: {self.quality}")
        lines.append(f"STATUS: {self.status}")
        return "\n".join(lines)

    def to_dict(self) -> dict:
        return {
            "pair": self.pair, "timeframe": self.timeframe, "start": self.start,
            "end": self.end, "rows": self.rows, "expected_rows": self.expected_rows,
            "duplicates": self.duplicates, "missing_intervals": self.missing_intervals,
            "invalid_ohlc": self.invalid_ohlc, "invalid_volume": self.invalid_volume,
            "non_monotonic": self.non_monotonic, "gap_ranges": self.gap_ranges[:20],
            "first_ts": self.first_ts, "last_ts": self.last_ts, "timezone": self.timezone,
            "incomplete_last_candle": self.incomplete_last_candle, "repairs": self.repairs,
            "quality": self.quality,
            "status": self.status, "problems": self.problems,
        }


def df_to_ts(df: pd.DataFrame) -> pd.Series:
    """Return the `date` column as integer unix seconds (UTC).

    Handles any datetime64 resolution (pyarrow/feather may store s/ms/us).
    """
    d = df["date"]
    if pd.api.types.is_datetime64_any_dtype(d):
        if getattr(d.dt, "tz", None) is not None:
            d = d.dt.tz_convert("UTC")
        return d.astype("datetime64[ns]").astype("int64") // 1_000_000_000
    return d.astype("int64")


def _fmt(ts: Optional[int]) -> str:
    if ts is None:
        return "?"
    return pd.Timestamp(int(ts), unit="s", tz="UTC").strftime("%Y-%m-%d %H:%M:%S UTC")


def validate(
    df: pd.DataFrame,
    pair: str,
    tf: str,
    *,
    expected_start_ts: Optional[int] = None,
    expected_end_ts: Optional[int] = None,
    end_is_open: bool = False,
    repair: bool = False,
) -> tuple[pd.DataFrame, ValidationReport]:
    """Validate a freqtrade-format dataframe.

    Returns (possibly repaired df, report). Repair is limited to:
      * removing duplicate timestamps (keeping the LAST occurrence),
      * re-sorting by date,
    and only when `repair=True`.
    """
    rep = ValidationReport(pair=pair, timeframe=tf)
    problems = rep.problems

    for col in REQUIRED_COLUMNS:
        if col not in df.columns:
            rep.status = "FAIL"
            problems.append(f"missing column {col!r}")
            return df, rep

    out = df.copy()
    ts = df_to_ts(out)
    interval = parse_timeframe(tf).seconds

    # 7. timezone sanity: unix seconds within a plausible era
    if len(ts) and (ts.min() < 946_684_800 or ts.max() > 4_102_444_800):
        rep.status = "FAIL"
        problems.append("timestamps outside plausible unix era (timezone/epoch problem?)")

    # 2. duplicates: count extra rows beyond the first occurrence
    dup_mask = ts.duplicated(keep="first")
    rep.duplicates = int(dup_mask.sum())
    if repair and dup_mask.any():
        out = out[~ts.duplicated(keep="last")]
        rep.repairs.append(f"removed {rep.duplicates} duplicate timestamps (kept last)")
        ts = df_to_ts(out)

    # 1. monotonicity (after optional dedupe; sort applied only when repairing)
    non_monotonic = int((ts.diff().dropna() < 0).sum())
    rep.non_monotonic = non_monotonic
    if non_monotonic:
        if repair:
            out = out.sort_values("date").reset_index(drop=True)
            rep.repairs.append(f"re-sorted {non_monotonic} out-of-order rows")
            ts = df_to_ts(out)
            rep.non_monotonic = 0
        else:
            rep.status = "FAIL"
            problems.append(f"{non_monotonic} non-monotonic timestamps")

    if len(out) == 0:
        rep.status = "FAIL"
        rep.rows = 0
        problems.append("no rows")
        return out, rep

    o, h, l, c, v = (out[col].astype(float) for col in ("open", "high", "low", "close", "volume"))

    # 3. OHLC relationships
    bad_ohlc = (
        (h < o) | (h < c) | (l > o) | (l > c) | (h < l)
        | (o <= 0) | (h <= 0) | (l <= 0) | (c <= 0)
        | (~np.isfinite(o)) | (~np.isfinite(h)) | (~np.isfinite(l)) | (~np.isfinite(c))
    )
    rep.invalid_ohlc = int(bad_ohlc.sum())

    # 4. volume
    bad_vol = (v < 0) | (~np.isfinite(v))
    rep.invalid_volume = int(bad_vol.sum())

    # 5/10. spacing
    diffs = ts.diff().dropna()
    if len(diffs):
        rep.candle_spacing_ok = bool((diffs == interval).all())
        if not rep.candle_spacing_ok:
            odd = int((diffs != interval).sum())
            problems.append(f"{odd} candles with spacing != {tf}")
            rep.status = "FAIL" if rep.status == "PASS" else rep.status

    # 6. missing intervals inside data (gaps > 1 interval, excluding pre-data start)
    if len(diffs):
        gaps = diffs[diffs > interval]
        rep.missing_intervals = int(len(gaps))
        if len(gaps):
            idx = gaps.index.to_list()
            for i in idx[:20]:
                prev_t = int(ts.loc[i - 1])
                cur_t = int(ts.loc[i])
                rep.gap_ranges.append(f"{_fmt(prev_t)} -> {_fmt(cur_t)} ({(cur_t - prev_t) // interval - 1} missing)")

    # 8. first/last
    rep.first_ts = _fmt(int(ts.iloc[0]))
    rep.last_ts = _fmt(int(ts.iloc[-1]))
    rep.rows = int(len(out))

    # 9/10. expected rows for the requested range (only when both bounds given)
    if expected_start_ts is not None and expected_end_ts is not None:
        span = int(expected_end_ts) - int(expected_start_ts)
        expected = span // interval if span > 0 else 0
        if end_is_open:
            # the last (still-open) candle may be incomplete
            expected = max(expected - 1, 0)
        rep.expected_rows = expected
        actual_start = int(ts.iloc[0])
        actual_end = int(ts.iloc[-1])
        if actual_start > int(expected_start_ts) + interval:
            problems.append(
                f"data starts {actual_start - int(expected_start_ts)}s after requested start "
                f"(exchange has no earlier data?)"
            )
        if not end_is_open and actual_end < int(expected_end_ts) - 2 * interval:
            problems.append(
                f"data ends {(int(expected_end_ts) - actual_end) // interval} candles before requested end"
            )

    # incomplete last candle: last candle starts within one interval of "now"/end
    if end_is_open:
        rep.incomplete_last_candle = True

    # overall status
    if rep.status == "PASS":
        if rep.invalid_ohlc or rep.invalid_volume or rep.duplicates or rep.non_monotonic:
            rep.status = "FAIL"
        elif rep.candle_spacing_ok is False:
            rep.status = "FAIL"
        elif rep.missing_intervals:
            # gaps are reported but do not fail the dataset by default;
            # market suspensions exist on real exchanges.
            rep.status = "PASS_WITH_GAPS"
        else:
            rep.status = "PASS"
    return out, rep
