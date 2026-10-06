"""Background job manager for the web UI.

Jobs never block the UI thread. Download/discover/validate run in threads
sharing the same service functions the CLI uses; backtests run in a
subprocess (isolated Freqtrade globals + clean cancellation via process kill).
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass
class Job:
    id: str
    kind: str
    params: dict
    status: str = "running"  # running / done / error / cancelled
    created_at: str = field(default_factory=_now_iso)
    finished_at: Optional[str] = None
    progress: dict = field(default_factory=dict)
    log: deque = field(default_factory=lambda: deque(maxlen=4000))
    result: Optional[dict] = None
    error: Optional[str] = None
    stop_event: threading.Event = field(default_factory=threading.Event)
    thread: Optional[threading.Thread] = None
    proc: Optional[subprocess.Popen] = None

    def add_log(self, line: str) -> None:
        self.log.append(line.rstrip("\n"))

    def to_dict(self, include_log: bool = False) -> dict:
        d = {
            "id": self.id,
            "kind": self.kind,
            "status": self.status,
            "created_at": self.created_at,
            "finished_at": self.finished_at,
            "elapsed_s": round(
                (datetime.now(timezone.utc) - datetime.fromisoformat(self.created_at)).total_seconds(), 1
            ),
            "progress": self.progress,
            "result": self.result,
            "error": self.error,
        }
        if include_log:
            d["log"] = list(self.log)[-400:]
        return d


class JobManager:
    def __init__(self, logs_dir: Path) -> None:
        self.jobs: dict[str, Job] = {}
        self._lock = threading.Lock()
        self.logs_dir = Path(logs_dir)
        self.logs_dir.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------ api
    def submit(self, kind: str, fn: Callable[[Job], None], params: dict) -> Job:
        job = Job(id=uuid.uuid4().hex[:10], kind=kind, params=params)
        with self._lock:
            self.jobs[job.id] = job
        t = threading.Thread(target=self._runner, args=(job, fn), daemon=True,
                             name=f"job-{job.id}")
        job.thread = t
        t.start()
        return job

    def get(self, job_id: str) -> Optional[Job]:
        return self.jobs.get(job_id)

    def list(self) -> list[Job]:
        return sorted(self.jobs.values(), key=lambda j: j.created_at, reverse=True)[:100]

    def cancel(self, job_id: str) -> bool:
        job = self.jobs.get(job_id)
        if not job or job.status != "running":
            return False
        job.add_log("[cancel] cancellation requested")
        job.stop_event.set()
        if job.proc is not None and job.proc.poll() is None:
            try:
                job.proc.terminate()
            except OSError:
                pass
        return True

    # --------------------------------------------------------------- runner
    def _runner(self, job: Job, fn: Callable[[Job], None]) -> None:
        try:
            fn(job)
            if job.status == "running":
                job.status = "done"
        except Exception as exc:  # noqa: BLE001
            cancelled = job.stop_event.is_set()
            job.status = "cancelled" if cancelled else "error"
            job.error = f"{exc.__class__.__name__}: {exc}"
            job.add_log(f"[{'cancel' if cancelled else 'error'}] {job.error}")
        finally:
            if job.status == "running":
                job.status = "cancelled"
            job.finished_at = _now_iso()
            _persist_job_state(self.logs_dir, job)


def _persist_job_state(logs_dir: Path, job: Job) -> None:
    """Small JSON state file per job (survives UI refreshes)."""
    try:
        (logs_dir / "jobs").mkdir(parents=True, exist_ok=True)
        (logs_dir / "jobs" / f"{job.id}.json").write_text(
            json.dumps(job.to_dict(), default=str, indent=1), encoding="utf-8"
        )
    except OSError:
        pass


# --------------------------------------------------------------------------
# Job factories: these wrap the SAME services the CLI uses.

def _progress_to_job(job: Job, ev: dict) -> None:
    e = ev.get("event")
    if e == "page":
        job.progress = {
            "stage": "download",
            "pair": ev.get("pair"),
            "timeframe": ev.get("timeframe"),
            "chunk": ev.get("chunk"),
            "chunks_total": ev.get("chunks_total"),
            "candles": ev.get("candles"),
        }
        job.add_log(f"[download] {ev.get('pair')} {ev.get('timeframe')} "
                    f"chunk {ev.get('chunk')}/{ev.get('chunks_total')} candles {ev.get('candles'):,}")
    elif e == "chunk_skipped":
        job.add_log(f"[download] {ev.get('pair')} {ev.get('timeframe')} "
                    f"chunk {ev.get('chunk')}/{ev.get('chunks_total')} skipped (already have data)")
    elif e == "task_done":
        job.add_log(f"[ok] {ev.get('pair')} {ev.get('timeframe')}: "
                    f"{ev.get('rows')} rows (+{ev.get('new_rows')}), validation={ev.get('validation')}")
    elif e == "task_empty":
        job.add_log(f"[empty] {ev.get('pair')} {ev.get('timeframe')}: {ev.get('reason')}")
    elif e == "task_start":
        job.add_log(f"[start] {ev.get('pair')} {ev.get('timeframe')} "
                    f"({ev.get('data_start')} .. {ev.get('end')})")
    elif e == "job_done":
        job.add_log(f"[done] total rows {ev.get('total_rows'):,} ok={ev.get('ok')}")


def make_download_job(paths: dict, job: Job) -> None:
    from .downloader import DownloadRequest, Downloader
    from .nobitex_client import NobitexClient
    from .cli import _parse_dt, _parse_pairs

    p = job.params
    client = NobitexClient()
    job.add_log(f"[info] Downloading {len(p['pairs'])} pairs x {len(p['timeframes'])} timeframes")
    try:
        summary = Downloader(
            client, paths["datadir"], paths["manifests_dir"], paths["reports_dir"],
            progress_cb=lambda ev: _progress_to_job(job, ev),
            stop_event=job.stop_event,
        ).download(DownloadRequest(
            pairs=_parse_pairs(p["pairs"]),
            timeframes=p["timeframes"],
            start=_parse_dt(p["start"]),
            end=_parse_dt(p.get("end", "now"), default_now=True),
            exchange="nobitex",
            drop_incomplete_last=bool(p.get("drop_incomplete", True)),
            force=bool(p.get("force", False)),
        ))
    finally:
        client.close()
    job.result = summary.to_dict()
    job.progress = {"stage": "complete", "total_rows": summary.total_rows}
    if not summary.ok:
        raise RuntimeError("download finished with validation problems (see log)")


def make_validate_job(paths: dict, job: Job) -> None:
    from .downloader import Downloader
    from .cli import _parse_dt, _parse_pairs

    p = job.params
    job.add_log(f"[info] Validating {len(p['pairs'])} pairs x {len(p['timeframes'])} timeframes")
    dl = Downloader(None, paths["datadir"], paths["manifests_dir"], paths["reports_dir"])  # type: ignore[arg-type]
    reports = dl.validate_dataset(
        exchange="nobitex",
        pairs=_parse_pairs(p["pairs"]),
        timeframes=p["timeframes"],
        start=_parse_dt(p["start"]),
        end=_parse_dt(p.get("end", "now"), default_now=True),
        end_is_open=bool(p.get("end_is_open", True)),
    )
    out = []
    for rep in reports:
        job.add_log("\n" + rep.render())
        out.append(rep.to_dict())
    job.result = {"reports": out,
                  "all_ok": all(r["status"] in ("PASS", "PASS_WITH_GAPS") for r in out)}


MARKETS_CACHE_ENV = "NOBITEX_ADAPTER_MARKETS_CACHE"


def make_discover_job(job: Job) -> None:
    import os

    from .nobitex_client import NobitexClient

    job.add_log(f"[info] Discovering Nobitex markets (quote={job.params.get('quote', 'USDT')})")
    client = NobitexClient()
    try:
        markets = client.discover_markets(quote=job.params.get("quote", "USDT"))
    finally:
        client.close()
    job.add_log(f"[info] {len(markets)} markets found "
                f"({sum(1 for m in markets if m.active)} active)")
    result = {"markets": [m.to_dict() for m in markets], "fetched_at": _now_iso()}
    job.result = result
    # persist for the UI (survives restarts)
    cache_file = os.environ.get(MARKETS_CACHE_ENV)
    if cache_file:
        try:
            Path(cache_file).write_text(json.dumps(result), encoding="utf-8")
        except OSError as exc:
            job.add_log(f"[warn] could not persist market cache: {exc}")


def make_backtest_job(paths: dict, job: Job) -> None:
    """Run the backtest in a subprocess (robust cancellation, clean globals)."""
    from .cli import _parse_pairs

    p = job.params
    pairs = _parse_pairs(p["pairs"])
    out_json = paths["jobs_dir"] / f"bt-{job.id}.json"
    log_file = paths["logs_dir"] / f"backtest-{job.id}.log"
    paths["jobs_dir"].mkdir(parents=True, exist_ok=True)
    paths["logs_dir"].mkdir(parents=True, exist_ok=True)

    root = Path(paths["user_data_dir"]).parent
    env = dict(os.environ)
    # the subprocess must import the adapter package no matter which repo root
    # the GUI was created with: cover both the given root and the package's
    # own parent directory
    import nobitex_adapter as _pkg
    pkg_root = str(Path(_pkg.__file__).resolve().parent.parent)
    parts = {str(root), pkg_root}
    env["PYTHONPATH"] = os.pathsep.join(list(parts) + ([env["PYTHONPATH"]] if env.get("PYTHONPATH") else []))

    argv = [
        sys.executable, "-m", "nobitex_adapter",
        "--repo", str(root),
        "backtest",
        "--strategy", p["strategy"],
        "--pairs", ",".join(pairs),
        "--start", p["start"],
        "--end", p.get("end", "now"),
        "--capital", str(p.get("capital", 10000)),
        "--stake", str(p.get("stake", "unlimited")),
        "--stake-currency", str(p.get("stake_currency", "USDT")),
        "--max-open", str(p.get("max_open_trades", 8)),
        "--fee", str(p.get("fee", "0.002") or ""),
        "--out", str(out_json),
    ]
    job.add_log(f"[info] freqtrade backtest: {p['strategy']} "
                f"{len(pairs)} pairs {p['start']}..{p.get('end', 'now')}")
    job.progress = {"stage": "backtest"}

    proc = subprocess.Popen(
        argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        env=env, cwd=str(root), text=True,
        bufsize=1,
    )
    job.proc = proc
    try:
        assert proc.stdout is not None
        for line in proc.stdout:
            job.add_log(line)
        proc.wait()
    finally:
        job.proc = None

    if job.stop_event.is_set():
        raise RuntimeError("cancelled")
    if not out_json.is_file():
        raise RuntimeError(f"backtest did not produce output (exit={proc.returncode}); see log")
    res = json.loads(out_json.read_text(encoding="utf-8"))
    job.result = res
    job.progress = {"stage": "complete", "elapsed_s": res.get("elapsed")}
    job.add_log(f"[done] exit={res.get('exit_code')} zip={res.get('results_zip')}")
    if not res.get("ok"):
        raise RuntimeError(res.get("error") or "backtest failed")
