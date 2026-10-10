"""Versioned on-disk data contract + deterministic fingerprints.

Every dataset the downloader writes consists of three artifacts per
(exchange, pair, timeframe):

  * ``<datadir>/<exchange>/<PAIR>-<tf>.feather`` — Freqtrade feather layout
    (tz-aware UTC ``date`` + float64 ``open/high/low/close/volume``);
  * ``<manifest_dir>/<exchange>/<PAIR>-<tf>.json`` — chunk coverage
    (``covered`` map) for resume;
  * ``<report_dir>/<PAIR>-<tf>.json`` — validation summary.

Before contract v1 these JSON sidecars were UNVERSIONED (``{"covered": ...}``
only), so consumers could not tell what produced a file, and two downloads
could only be compared by re-reading every row. Contract v1 adds, WITHOUT
changing any path or the feather layout:

  * ``contract: 1`` — the sidecar schema version;
  * ``provenance`` — what/when produced the data
    (``adapter_version``, ``exchange``, ``pair``, ``timeframe``,
    ``written_at`` UTC, ``source``);
  * ``fingerprint`` — deterministic content hash
    (``sha256`` + ``rows`` + ``first_ts``/``last_ts``).

Legacy sidecars (no ``contract`` key) are contract v0: readers MUST accept
them (``covered`` map keeps working for resume) and writers upgrade them to
v1 on the next save. The fingerprint is computed over LOGICAL content
(sorted UTC-second timestamps + float64 OHLCV), so it survives feather
rewrites, dtype spellings and row order — but ANY value change alters it.
"""
from __future__ import annotations

import hashlib
import struct
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd

DATA_CONTRACT_VERSION = 1

_OHLCV_COLS = ("open", "high", "low", "close", "volume")

# struct layout per candle row: int64 unix seconds + 5 float64 (OHLCV),
# little-endian. Fixed for all v1 fingerprints.
_ROW_STRUCT = struct.Struct("<q5d")


def canonical_rows(df: pd.DataFrame) -> list[tuple[int, float, float, float, float, float]]:
    """Reduce a candle frame to canonical ``(ts, o, h, l, c, v)`` rows.

    Accepts the stored layout (tz-aware UTC ``date``), the in-memory layout
    (tz-naive UTC ``date``) and raw integer unix-second ``date`` columns;
    OHLCV columns are coerced to float64. Output is sorted ascending by ts
    with duplicate timestamps removed (last wins), so row order and LOSSLESS
    dtype spellings never affect the fingerprint (a lossy spelling such as
    float32 rounding genuinely changes values and therefore the hash).
    """
    if "date" not in df.columns:
        raise ValueError("fingerprint requires a 'date' column")
    for col in _OHLCV_COLS:
        if col not in df.columns:
            raise ValueError(f"fingerprint requires an {col!r} column")
    d = df["date"]
    if pd.api.types.is_datetime64_any_dtype(d):
        if getattr(d.dt, "tz", None) is not None:
            d = d.dt.tz_convert("UTC").dt.tz_localize(None)
        # normalize ANY datetime64 resolution (s/ms/us/ns) to ns, then seconds
        ts = d.astype("datetime64[ns]").astype("int64") // 1_000_000_000
    else:
        ts = d.astype("int64")
    by_ts: dict[int, tuple[float, float, float, float, float]] = {}
    o = df["open"].astype("float64").tolist()
    h = df["high"].astype("float64").tolist()
    lo = df["low"].astype("float64").tolist()
    c = df["close"].astype("float64").tolist()
    v = df["volume"].astype("float64").tolist()
    for i, t in enumerate(ts.tolist()):
        by_ts[int(t)] = (float(o[i]), float(h[i]), float(lo[i]), float(c[i]), float(v[i]))
    return [(t, *by_ts[t]) for t in sorted(by_ts)]


def fingerprint_dataframe(df: pd.DataFrame) -> dict[str, Any]:
    """Fingerprint a candle frame: ``{sha256, rows, first_ts, last_ts}``.

    ``sha256`` is ``"sha256:<hex>"`` over the packed canonical rows (empty
    frame -> hash of the empty string, rows 0, ``first_ts``/``last_ts`` None).
    """
    rows = canonical_rows(df)
    digest = hashlib.sha256()
    for row in rows:
        digest.update(_ROW_STRUCT.pack(*row))
    return {
        "sha256": f"sha256:{digest.hexdigest()}",
        "rows": len(rows),
        "first_ts": rows[0][0] if rows else None,
        "last_ts": rows[-1][0] if rows else None,
    }


def fingerprint_file(path: str | Path) -> dict[str, Any]:
    """Fingerprint a stored feather file (same schema as dataframe)."""
    return fingerprint_dataframe(pd.read_feather(path))


def provenance(exchange: str, pair: str, timeframe: str, source: str = "download") -> dict[str, Any]:
    """Build the v1 ``provenance`` block for a sidecar."""
    from . import __version__

    return {
        "adapter_version": __version__,
        "exchange": exchange,
        "pair": pair,
        "timeframe": timeframe,
        "source": source,
        "written_at": datetime.now(timezone.utc).isoformat(),
    }


def contract_block(
    exchange: str,
    pair: str,
    timeframe: str,
    fingerprint: dict[str, Any] | None,
    source: str = "download",
) -> dict[str, Any]:
    """Build the v1 sidecar additions (contract + provenance + fingerprint)."""
    return {
        "contract": DATA_CONTRACT_VERSION,
        "provenance": provenance(exchange, pair, timeframe, source=source),
        "fingerprint": fingerprint,
    }


def sidecar_contract_version(sidecar: dict[str, Any]) -> int:
    """Return the contract version of a manifest/report dict (v0 = legacy)."""
    try:
        return int(sidecar.get("contract", 0))
    except (TypeError, ValueError):
        return 0


def is_legacy_sidecar(sidecar: dict[str, Any]) -> bool:
    """True when the sidecar predates the versioned contract (v0)."""
    return sidecar_contract_version(sidecar) < DATA_CONTRACT_VERSION
