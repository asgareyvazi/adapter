"""Historical OHLCV download engine.

Turns Nobitex `/market/udf/history` responses (<=500 candles/page) into
Freqtrade-compatible feather files, with:

  * time chunking (default 2000 candles/chunk => a few pages per chunk)
  * page-level pagination inside each chunk
  * retry / timeout / rate limiting (in NobitexClient)
  * duplicate removal + chronological ordering
  * malformed-candle detection (client + validator)
  * missing-candle / gap detection (validator report)
  * incomplete-last-candle handling (`drop_incomplete_last`)
  * deterministic output (feather, UTC, freqtrade column layout)
  * resume: chunk coverage is recorded in a manifest so unchanged ranges
    are skipped (no blind re-download)
"""
from __future__ import annotations

import json
import logging
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

import pandas as pd

from .nobitex_client import NobitexClient, NobitexError, NobitexNoData
from .timeframes import DEFAULT_STARTUP_CANDLES, parse_timeframe, to_nobitex_resolution
from .validator import ValidationReport, validate

log = logging.getLogger("nobitex.download")

CANDLES_PER_PAGE = 500  # documented max per /market/udf/history response
DEFAULT_CHUNK_CANDLES = 2000  # 4 pages per chunk -> resume granularity


def pair_to_filename(pair: str) -> str:
    return pair.replace("/", "_")


def _date_to_int_seconds(series: pd.Series) -> pd.Series:
    """Any datetime64 resolution (or int) -> int64 unix seconds (UTC)."""
    if pd.api.types.is_datetime64_any_dtype(series):
        if getattr(series.dt, "tz", None) is not None:
            series = series.dt.tz_convert("UTC")
        return series.astype("datetime64[ns]").astype("int64") // 1_000_000_000
    return series.astype("int64")


def data_filename(datadir: Path, exchange: str, pair: str, tf: str) -> Path:
    datadir = Path(datadir)
    return datadir / exchange / f"{pair_to_filename(pair)}-{tf}.feather"


def _dt_to_ts(dt: datetime) -> int:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp())


@dataclass
class DownloadRequest:
    pairs: list[str]
    timeframes: list[str]
    start: datetime
    end: datetime
    exchange: str = "nobitex"
    startup_candles: Optional[dict[str, int]] = None  # tf -> extra lead-in candles
    chunk_candles: int = DEFAULT_CHUNK_CANDLES
    drop_incomplete_last: bool = False
    force: bool = False  # ignore manifest, re-download
    end_is_open: bool = True  # `end` is "now-ish": last candle may be incomplete

    def validate(self) -> None:
        if not self.pairs:
            raise ValueError("no pairs given")
        # canonical boundary: normalize timeframes even for programmatic use
        from .timeframes import normalize_timeframes

        self.timeframes = normalize_timeframes(self.timeframes, param_name="timeframes")
        s, e = _dt_to_ts(self.start), _dt_to_ts(self.end)
        if e <= s:
            raise ValueError("end must be after start")


@dataclass
class TaskResult:
    pair: str
    timeframe: str
    status: str = "PENDING"  # PENDING/DOWNLOADING/DONE/ERROR
    rows: int = 0
    new_rows: int = 0
    duplicates_removed: int = 0
    first_ts: str = ""
    last_ts: str = ""
    chunks_total: int = 0
    chunks_done: int = 0
    chunks_skipped: int = 0
    error: str = ""
    validation: Optional[ValidationReport] = None


@dataclass
class DownloadSummary:
    exchange: str = "nobitex"
    start: str = ""
    end: str = ""
    tasks: list[TaskResult] = field(default_factory=list)
    total_rows: int = 0
    all_valid: bool = True

    @property
    def ok(self) -> bool:
        # zero-data tasks are ERROR (never silent success)
        return all(t.status == "DONE" for t in self.tasks) and self.all_valid

    def to_dict(self) -> dict:
        return {
            "exchange": self.exchange,
            "start": self.start,
            "end": self.end,
            "total_rows": self.total_rows,
            "all_valid": self.all_valid,
            "tasks": [
                {
                    "pair": t.pair,
                    "timeframe": t.timeframe,
                    "status": t.status,
                    "rows": t.rows,
                    "new_rows": t.new_rows,
                    "duplicates_removed": t.duplicates_removed,
                    "first_ts": t.first_ts,
                    "last_ts": t.last_ts,
                    "chunks_total": t.chunks_total,
                    "chunks_done": t.chunks_done,
                    "chunks_skipped": t.chunks_skipped,
                    "error": t.error,
                    "validation": t.validation.to_dict() if t.validation else None,
                }
                for t in self.tasks
            ],
        }


ProgressCb = Callable[[dict], None]


class Downloader:
    def __init__(
        self,
        client: NobitexClient,
        datadir: Path,
        manifest_dir: Path,
        report_dir: Optional[Path] = None,
        progress_cb: Optional[ProgressCb] = None,
        stop_event: Optional[threading.Event] = None,
    ) -> None:
        self.client = client
        self.datadir = Path(datadir)
        self.manifest_dir = Path(manifest_dir)
        self.report_dir = Path(report_dir) if report_dir else self.manifest_dir.parent / "reports"
        self.progress_cb = progress_cb or (lambda ev: None)
        self.stop_event = stop_event

    # ------------------------------------------------------------ helpers
    def _emit(self, **ev) -> None:
        ev.setdefault("ts", time.time())
        try:
            self.progress_cb(ev)
        except Exception:  # noqa: BLE001 - progress must never break downloads
            log.exception("progress callback failed")

    def _manifest_path(self, req: DownloadRequest, pair: str, tf: str) -> Path:
        return self.manifest_dir / req.exchange / f"{pair_to_filename(pair)}-{tf}.json"

    def _load_manifest(self, path: Path) -> dict:
        if path.is_file():
            try:
                return json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                return {}
        return {}

    def _save_manifest(self, path: Path, manifest: dict) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(manifest, indent=1), encoding="utf-8")
        tmp.replace(path)

    @staticmethod
    def _chunks(start_ts: int, end_ts: int, chunk_candles: int, interval: int):
        """Chunk [start_ts, end_ts) into deterministic windows.

        Windows are aligned to `start_ts` (not the epoch) so large timeframes
        (e.g. 1d with 2000-candle chunks) don't stretch far backwards.
        Window length is also capped at ~365 days for sane resume granularity.
        """
        chunk_secs = min(chunk_candles * interval, 365 * 86400)
        cur = start_ts
        while cur < end_ts:
            yield cur, min(cur + chunk_secs, end_ts)
            cur += chunk_secs

    # ------------------------------------------------------------ feather
    @staticmethod
    def _df_from_candles(candles) -> pd.DataFrame:
        if not candles:
            return pd.DataFrame(columns=["date", "open", "high", "low", "close", "volume"])
        df = pd.DataFrame(
            [(c.ts, c.open, c.high, c.low, c.close, c.volume) for c in candles],
            columns=["date", "open", "high", "low", "close", "volume"],
        )
        return df

    @staticmethod
    def _normalize_date(df: pd.DataFrame) -> pd.DataFrame:
        """Make `date` tz-naive UTC datetime64[ns] (freqtrade's feather layout)."""
        if "date" not in df.columns or len(df) == 0:
            return df
        d = df["date"]
        if pd.api.types.is_datetime64_any_dtype(d):
            if getattr(d.dt, "tz", None) is not None:
                d = d.dt.tz_convert("UTC").dt.tz_localize(None)
            return df.assign(date=d.astype("datetime64[ns]"))
        # integer unix seconds
        return df.assign(date=pd.to_datetime(d.astype("int64"), unit="s"))

    def _load_existing(self, path: Path) -> Optional[pd.DataFrame]:
        if not path.is_file():
            return None
        try:
            df = pd.read_feather(path)
            if "date" not in df.columns:
                return None
            df = self._normalize_date(df)
            df["date"] = df["date"].astype("datetime64[ns]")
            return df
        except Exception as exc:  # noqa: BLE001
            log.warning("could not read existing %s (%s); will rewrite", path, exc)
            return None

    def _write_feather(self, path: Path, df: pd.DataFrame) -> None:
        """Write in Freqtrade's feather layout: tz-aware UTC `date` + OHLCV.

        Freqtrade 2026.x compares candle dates against tz-aware timeranges,
        so the stored column must carry the UTC timezone.
        """
        path.parent.mkdir(parents=True, exist_ok=True)
        out = df.copy()
        d = out["date"].astype("datetime64[ns]")
        if getattr(d.dt, "tz", None) is None:
            d = d.dt.tz_localize("UTC")
        else:
            d = d.dt.tz_convert("UTC")
        out["date"] = d
        tmp = path.with_suffix(".tmp")
        out.to_feather(tmp)
        tmp.replace(path)

    # ------------------------------------------------------------ download
    def download(self, req: DownloadRequest) -> DownloadSummary:
        req.validate()
        summary = DownloadSummary(
            exchange=req.exchange,
            start=req.start.isoformat(),
            end=req.end.isoformat(),
        )
        self._emit(event="job_start", pairs=req.pairs, timeframes=req.timeframes,
                   start=req.start.isoformat(), end=req.end.isoformat())

        for pair in req.pairs:
            for tf in req.timeframes:
                if self._stopped():
                    raise NobitexError("cancelled by user", code="Cancelled")
                task = self._download_one(req, pair, tf)
                summary.tasks.append(task)
                if task.status != "DONE":
                    summary.all_valid = False

        summary.total_rows = sum(t.rows for t in summary.tasks)
        self._emit(event="job_done", ok=summary.ok, total_rows=summary.total_rows,
                   tasks=len(summary.tasks))
        return summary

    def _stopped(self) -> bool:
        return self.stop_event is not None and self.stop_event.is_set()

    def _fetch_window(
        self,
        sym: str,
        resolution: str,
        start_ts: int,
        end_ts: int,
        empty_reqs: list[str],
    ) -> list:
        """Fetch [start_ts, end_ts) via <=500-candle pages.

        Returns candles (possibly empty). Every empty response is recorded
        in ``empty_reqs`` with its exact request context so a zero-data
        task can be diagnosed instead of failing silently.
        """
        out = []
        page = 1
        while True:
            try:
                batch = self.client.candles_page(
                    symbol=sym, resolution=resolution,
                    start_ts=start_ts, end_ts=end_ts, page=page,
                )
            except NobitexNoData:
                empty_reqs.append(
                    f"no_data symbol={sym} res={resolution} "
                    f"from={start_ts} to={end_ts} page={page}"
                )
                break
            out.extend(batch)
            if len(batch) < CANDLES_PER_PAGE:
                break
            page += 1
        return out

    def _download_one(self, req: DownloadRequest, pair: str, tf: str) -> TaskResult:
        t = parse_timeframe(tf)
        interval = t.seconds
        resolution = to_nobitex_resolution(tf)
        sym = pair.replace("/", "")
        path = data_filename(self.datadir, req.exchange, pair, tf)
        manifest_path = self._manifest_path(req, pair, tf)
        empty_reqs: list[str] = []

        start_ts = _dt_to_ts(req.start)
        end_ts = _dt_to_ts(req.end)

        startup = (req.startup_candles or {}).get(tf, DEFAULT_STARTUP_CANDLES.get(tf, 200))
        data_start = start_ts - startup * interval

        task = TaskResult(pair=pair, timeframe=tf, status="DOWNLOADING")
        self._emit(event="task_start", pair=pair, timeframe=tf,
                   data_start=data_ts_iso(data_start), end=end_ts_iso(end_ts),
                   startup_candles=startup)

        manifest = {} if req.force else self._load_manifest(manifest_path)
        covered = manifest.get("covered", {})

        existing = None if req.force else self._load_existing(path)
        existing_ts: set[int] = set()
        if existing is not None and len(existing):
            existing_ts = set(_date_to_int_seconds(existing["date"]).tolist())

        chunk_secs = req.chunk_candles * interval
        all_chunks = list(self._chunks(data_start, end_ts, req.chunk_candles, interval))
        task.chunks_total = len(all_chunks)
        new_candles = []

        for i, (cs, ce) in enumerate(all_chunks):
            if self._stopped():
                raise NobitexError("cancelled by user", code="Cancelled")
            key = f"{cs}-{ce}"
            status = covered.get(key)
            if status in ("complete", "empty") and not req.force:
                task.chunks_skipped += 1
                self._emit(event="chunk_skipped", pair=pair, timeframe=tf,
                           chunk=i + 1, chunks_total=len(all_chunks), chunk_range=key)
                continue
            if status == "empty":
                continue
            # Fetch the chunk in windows of <= CANDLES_PER_PAGE candles.
            # A short window means the data edge was reached (market opened
            # mid-window or history ends) -> the chunk is complete.
            #
            # Adaptive narrowing: if a WIDE window comes back empty, retry
            # it narrower before declaring it empty. This makes the
            # downloader immune to undocumented per-request range limits
            # (an API that refuses wide ranges) while adding ZERO requests
            # when data is present. An empty narrow window advances the
            # cursor by the window width (the API asserted no data there).
            chunk_candles = []
            page_start = cs
            seen_ts: set[int] = set(c.ts for c in new_candles)
            while page_start < ce:
                full_end = min(page_start + CANDLES_PER_PAGE * interval, ce)
                window_end = full_end
                batch: list = []
                narrowed = False
                while True:
                    batch = self._fetch_window(
                        sym, resolution, page_start, window_end + interval,
                        empty_reqs,
                    )
                    if any(cs <= c.ts < ce for c in batch) \
                            or (window_end - page_start) <= 10 * interval:
                        break
                    # empty wide window -> halve it and retry (bounded):
                    # immune to undocumented per-request range limits, and
                    # costs ZERO extra requests when data is present.
                    narrowed = True
                    window_end = page_start + max(
                        (window_end - page_start) // 2, 10 * interval
                    )
                fresh = []
                for c in batch:
                    if cs <= c.ts < ce and c.ts not in seen_ts:
                        seen_ts.add(c.ts)
                        fresh.append(c)
                chunk_candles.extend(fresh)
                self._emit(event="page", pair=pair, timeframe=tf,
                           chunk=i + 1, chunks_total=len(all_chunks),
                           page_start=data_ts_iso(page_start),
                           candles=len(new_candles) + len(chunk_candles))
                if not narrowed and len(batch) < CANDLES_PER_PAGE:
                    break  # full-width window short/empty: data edge reached
                # narrowed: advance by the narrow window that was answered;
                # if even it came back EMPTY, jump to the full window end so
                # a truly-empty region is not crawled 10 candles at a time.
                page_start = full_end if (narrowed and not batch) else window_end

            if not chunk_candles:
                covered[key] = "empty"
            else:
                last_ts_chunk = chunk_candles[-1].ts
                if last_ts_chunk + interval < ce:
                    # fewer candles than the window width promised: this is the
                    # data edge (no more candles exist), NOT a partial failure.
                    log.info(
                        "%s %s: data edge at %s inside chunk %s-%s (treated as complete)",
                        pair, tf, data_ts_iso(last_ts_chunk), cs, ce,
                    )
                covered[key] = "complete"
                task.chunks_done += 1
            new_candles.extend(chunk_candles)
            self._emit(event="chunk_done", pair=pair, timeframe=tf,
                       chunk=i + 1, chunks_total=len(all_chunks),
                       chunk_candles=len(chunk_candles),
                       total_candles=len(new_candles) + len(existing_ts))

        manifest["covered"] = covered
        self._save_manifest(manifest_path, manifest)

        # ---- merge with existing, dedupe, order
        df_new = self._normalize_date(self._df_from_candles(new_candles))
        if existing is not None and len(existing):
            combined = (
                pd.concat([existing, df_new], ignore_index=True) if len(df_new) else existing.copy()
            )
        else:
            combined = df_new

        if len(combined) == 0:
            # Zero data is NEVER a success: it is a hard failure with a
            # diagnostic (exact requests seen + suggested probe command).
            task.status = "ERROR"
            task.rows = 0
            seen = empty_reqs[-3:] if empty_reqs else [
                "all chunk windows returned no data"
            ]
            task.error = (
                f"exchange returned ZERO candles for {pair} {tf} over "
                f"{data_ts_iso(data_start)} .. {end_ts_iso(end_ts)}\n"
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
                f"--timeframe {tf} --start {req.start:%Y-%m-%d} "
                f"--end {req.end:%Y-%m-%d}"
            )
            self._emit(event="task_empty", pair=pair, timeframe=tf,
                       reason=task.error)
            self._save_report(req, pair, tf, None, task)
            return task

        # dedupe + sort (deterministic)
        before = len(combined)
        combined = (
            combined.drop_duplicates(subset="date", keep="last")
            .sort_values("date")
            .reset_index(drop=True)
        )
        task.duplicates_removed = before - len(combined)

        # incomplete-last-candle handling: drop candles that have not closed yet
        if req.drop_incomplete_last and req.end_is_open:
            ts_arr = _date_to_int_seconds(combined["date"])
            keep_mask = (ts_arr.to_numpy() + interval) <= end_ts
            dropped = int((~keep_mask).sum())
            if dropped:
                combined = combined.loc[keep_mask].reset_index(drop=True)
                log.info("%s %s: dropped %d incomplete trailing candles", pair, tf, dropped)

        # canonical dtypes for freqtrade (tz-naive UTC datetime64[ns] + float64)
        for col in ("open", "high", "low", "close", "volume"):
            combined[col] = combined[col].astype("float64")

        self._write_feather(path, combined)
        task.rows = int(len(combined))
        task.new_rows = max(0, len(combined) - len(existing_ts))
        task.status = "DONE"
        ts_int = _date_to_int_seconds(combined["date"])
        task.first_ts = end_ts_iso(int(ts_int.iloc[0]))
        task.last_ts = end_ts_iso(int(ts_int.iloc[-1]))

        # ---- validation
        rep = self._validate(req, pair, tf, combined, data_start, end_ts)
        task.validation = rep
        if not rep.ok:
            task.status = "DONE"  # data stored; validation flags problems
        self._save_report(req, pair, tf, rep, task)
        self._emit(event="task_done", pair=pair, timeframe=tf, rows=task.rows,
                   new_rows=task.new_rows, status=task.status,
                   validation=rep.status)
        return task

    def _validate(
        self,
        req: DownloadRequest,
        pair: str,
        tf: str,
        df: pd.DataFrame,
        data_start: int,
        end_ts: int,
    ) -> ValidationReport:
        # validate the requested backtest window (start..end) on the stored data
        ts_win = _date_to_int_seconds(df["date"])
        interval = parse_timeframe(tf).seconds
        window = df[
            (ts_win >= _dt_to_ts(req.start)) & (ts_win < end_ts)
        ]
        if len(window) == 0:
            rep = ValidationReport(pair=pair, timeframe=tf, status="FAIL")
            rep.problems.append("no candles in the requested backtest window")
            return rep
        _, rep = validate(
            window,
            pair,
            tf,
            expected_start_ts=_dt_to_ts(req.start),
            expected_end_ts=end_ts,
            end_is_open=req.end_is_open,
            repair=False,
        )
        # incomplete final candle: kept a candle that was still open at `end`
        if req.end_is_open and not req.drop_incomplete_last and len(window):
            last_ts = int(_date_to_int_seconds(window["date"]).iloc[-1])
            if last_ts + interval > end_ts:
                rep.incomplete_last_candle = True
                rep.problems.append(
                    "final candle is incomplete (still open at the requested "
                    "end; re-download with default drop_incomplete or extend "
                    "the end date)"
                )
        return rep

    def _save_report(self, req, pair, tf, rep, task) -> None:
        self.report_dir.mkdir(parents=True, exist_ok=True)
        name = f"{pair_to_filename(pair)}-{tf}"
        payload = {
            "pair": pair,
            "timeframe": tf,
            "validation": rep.to_dict() if rep else None,
            "rows": task.rows,
            "new_rows": task.new_rows,
            "duplicates_removed": task.duplicates_removed,
            "first_ts": task.first_ts,
            "last_ts": task.last_ts,
            "error": task.error,
        }
        (self.report_dir / f"{name}.json").write_text(json.dumps(payload, indent=1), encoding="utf-8")

    # ------------------------------------------------------------- validate cmd
    def validate_dataset(
        self,
        exchange: str,
        pairs: list[str],
        timeframes: list[str],
        start: datetime,
        end: datetime,
        end_is_open: bool = True,
    ) -> list[ValidationReport]:
        """Validate stored data for the given range (read-only)."""
        out: list[ValidationReport] = []
        start_ts, end_ts = _dt_to_ts(start), _dt_to_ts(end)
        for pair in pairs:
            for tf in timeframes:
                path = data_filename(self.datadir, exchange, pair, tf)
                if not path.is_file():
                    rep = ValidationReport(pair=pair, timeframe=tf, status="MISSING")
                    rep.problems.append(f"no data file at {path}")
                    out.append(rep)
                    continue
                df = self._normalize_date(pd.read_feather(path))
                ts_win = _date_to_int_seconds(df["date"])
                window = df[
                    (ts_win >= start_ts) & (ts_win < end_ts)
                ]
                if len(window) == 0:
                    rep = ValidationReport(pair=pair, timeframe=tf, status="EMPTY")
                    rep.problems.append("no candles inside the requested window")
                    out.append(rep)
                    continue
                _, rep = validate(
                    window, pair, tf,
                    expected_start_ts=start_ts, expected_end_ts=end_ts,
                    end_is_open=end_is_open,
                )
                out.append(rep)
        return out


def data_ts_iso(ts: int) -> str:
    return pd.Timestamp(int(ts), unit="s", tz="UTC").isoformat()


def end_ts_iso(ts: int) -> str:
    return pd.Timestamp(int(ts), unit="s", tz="UTC").strftime("%Y-%m-%d %H:%M:%S UTC")
