"""FastAPI backend for the Nobitex Backtest Manager UI.

The UI is a control panel over the same services the CLI uses.
Market-data/backtest mode only: no private endpoints exist in this app.
"""
from __future__ import annotations

import json
import logging
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .. import __version__
from ..configgen import (
    detect_strategy_timeframes,
    default_repo_root,
    default_user_data_layout,
)
from ..jobs import (
    JobManager,
    make_backtest_job,
    make_discover_job,
    make_download_job,
    make_validate_job,
)

log = logging.getLogger("nobitex.webui")

STATIC_DIR = Path(__file__).parent / "static"


class JobRequest(BaseModel):
    kind: str  # discover | download | validate | backtest
    params: dict[str, Any] = {}


def _dt(s: str) -> datetime:
    return datetime.strptime(s.strip(), "%Y-%m-%d").replace(tzinfo=timezone.utc)


def _preset_range(preset: str) -> tuple[str, str]:
    now = datetime.now(timezone.utc)
    months = {"1M": 1, "3M": 3, "6M": 6, "1Y": 12, "2Y": 24, "3Y": 36}
    if preset in months:
        n = months[preset]
        # subtract months
        y = now.year
        m = now.month - n
        while m <= 0:
            m += 12
            y -= 1
        start = datetime(y, m, 1, tzinfo=timezone.utc)
        return start.strftime("%Y-%m-%d"), now.strftime("%Y-%m-%d")
    return now.strftime("%Y-%m-%d"), now.strftime("%Y-%m-%d")


def _ensure_layout_dirs(paths: dict) -> None:
    for p in (
        paths["datadir"], paths["strategies_dir"], paths["configs_dir"],
        paths["results_dir"], paths["jobs_dir"], paths["logs_dir"],
        paths["manifests_dir"], paths["reports_dir"],
    ):
        Path(p).mkdir(parents=True, exist_ok=True)


def create_app(root: Optional[Path] = None) -> FastAPI:
    app = FastAPI(title="Nobitex Backtest Manager", version=__version__)
    app.add_middleware(
        CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"]
    )

    # Mutable binding to the selected Freqtrade repository. The GUI HOST keeps
    # running in its own Python; BACKTEST JOBS always re-exec into the selected
    # repo's .venv (see runtime.py + jobs.make_backtest_job), so backtests run
    # on the user's real Freqtrade install.
    state: dict[str, Any] = {
        "root": Path(root) if root is not None else default_repo_root(),
        "paths": default_user_data_layout(Path(root) if root is not None else default_repo_root()),
    }
    _ensure_layout_dirs(state["paths"])

    jm = JobManager(state["paths"]["logs_dir"])
    markets_cache_file = state["paths"]["jobs_dir"].parent / "markets_cache.json"
    markets_cache: dict[str, Any] = {"markets": [], "fetched_at": None, "source": "cache"}
    if markets_cache_file.is_file():
        try:
            loaded = json.loads(markets_cache_file.read_text(encoding="utf-8"))
            if isinstance(loaded, dict) and "markets" in loaded:
                markets_cache = loaded
        except (OSError, json.JSONDecodeError):
            pass
    os.environ["NOBITEX_ADAPTER_MARKETS_CACHE"] = str(markets_cache_file)

    def _paths() -> dict:
        return state["paths"]

    def set_repo(new_root: Optional[str]) -> dict:
        """Rebind the GUI to a (different) Freqtrade repository at runtime."""
        from .. import runtime as rt

        if new_root is None or not str(new_root).strip():
            state["root"] = default_repo_root()
        else:
            candidate = Path(str(new_root).strip()).expanduser()
            if not candidate.is_dir():
                raise HTTPException(400, f"Freqtrade repository does not exist: {candidate}")
            state["root"] = candidate
        state["paths"] = default_user_data_layout(state["root"])
        _ensure_layout_dirs(state["paths"])
        os.environ["NOBITEX_ADAPTER_MARKETS_CACHE"] = str(
            state["paths"]["jobs_dir"].parent / "markets_cache.json"
        )
        try:
            probe = rt.resolve_runtime(state["root"], require_freqtrade=False)
            rt_error: Optional[str] = None if probe.ok else probe.error
        except rt.AdapterRuntimeError as exc:
            rt_error = str(exc)
        return {
            "root": str(state["root"]),
            "paths": {k: str(v) for k, v in state["paths"].items()},
            "runtime": _runtime_payload(),
            "runtime_error": rt_error,
        }

    def _runtime_payload() -> dict:
        from .. import runtime as rt

        return rt.diagnose(state["root"] if state["root"] != default_repo_root() else None)

    # ------------------------------------------------------------- helpers
    def _strategies() -> list[dict]:
        from ..backtest import bundled_strategies_dir

        out: list[dict] = []
        seen: set[str] = set()
        strategies_dir = Path(_paths()["strategies_dir"])
        # recursive: covers nested strategy-repo layouts (NostalgiaForInfinity/)
        files = sorted(strategies_dir.rglob("*.py")) if strategies_dir.is_dir() else []
        for f in files:
            if f.name.startswith("_"):
                continue
            try:
                rel = f.relative_to(strategies_dir)
            except ValueError:
                continue
            if any(
                part in ("__pycache__", ".git") or part.startswith(".")
                for part in rel.parts
            ):
                continue
            try:
                src = f.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue
            classes = re.findall(r"^class (\w+)\(.*IStrategy.*\)|^class (\w+)\(.*Strategy.*\)", src, re.M)
            names = [a or b for a, b in classes]
            # fall back to the module's main class heuristics
            if not names:
                names = re.findall(r"^class (\w+)", src, re.M)
            for name in names:
                if name.startswith("_") or name in ("IStrategy", "Strategy") or name in seen:
                    continue
                seen.add(name)
                det = detect_strategy_timeframes(f)
                out.append({
                    "name": name,
                    "file": str(f.relative_to(strategies_dir)),
                    "path": str(f),
                    "timeframe": det.get("timeframe"),
                    "info_timeframes": det.get("info_timeframes", []),
                    "startup_candle_count": det.get("startup_candle_count"),
                })
        # bundled fallback strategies (adapter checkout) - clearly marked
        if not out:
            bundled = bundled_strategies_dir()
            for f in sorted(bundled.rglob("*.py")) if bundled.is_dir() else []:
                if f.name.startswith("_") or "__pycache__" in f.parts:
                    continue
                try:
                    src = f.read_text(encoding="utf-8", errors="ignore")
                except OSError:
                    continue
                for name in re.findall(r"^class (\w+)\(.*IStrategy.*\)", src, re.M):
                    if name in seen:
                        continue
                    seen.add(name)
                    det = detect_strategy_timeframes(f)
                    out.append({
                        "name": name,
                        "file": f"bundled/{f.name}",
                        "path": str(f),
                        "timeframe": det.get("timeframe"),
                        "info_timeframes": det.get("info_timeframes", []),
                        "startup_candle_count": det.get("startup_candle_count"),
                        "bundled": True,
                    })
        return out

    def _markets() -> list[dict]:
        # the discover job writes the cache file; keep the in-memory copy fresh
        cache_file = state["paths"]["jobs_dir"].parent / "markets_cache.json"
        if cache_file.is_file():
            try:
                loaded = json.loads(cache_file.read_text(encoding="utf-8"))
                if isinstance(loaded, dict) and (
                    not markets_cache.get("markets") or
                    (loaded.get("fetched_at") or "") >= (markets_cache.get("fetched_at") or "")
                ):
                    markets_cache.clear()
                    markets_cache.update(loaded)
            except (OSError, json.JSONDecodeError):
                pass
        return markets_cache.get("markets", [])

    def _data_history() -> list[dict]:
        out = []
        man = _paths()["manifests_dir"] / "nobitex"
        if man.is_dir():
            for f in sorted(man.glob("*.json")):
                try:
                    d = json.loads(f.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError):
                    continue
                covered = d.get("covered", {})
                ranges = [k.split("-") for k in covered if k not in ("complete", "empty")]
                n_complete = sum(1 for v in covered.values() if v == "complete")
                n_empty = sum(1 for v in covered.values() if v == "empty")
                if ranges:
                    lo = min(int(r[0]) for r in ranges if r[0].lstrip("-").isdigit())
                    hi = max(int(r[1]) for r in ranges if r[1].lstrip("-").isdigit())
                    lo_s = datetime.fromtimestamp(lo, tz=timezone.utc).strftime("%Y-%m-%d")
                    hi_s = datetime.fromtimestamp(hi, tz=timezone.utc).strftime("%Y-%m-%d")
                else:
                    lo_s = hi_s = "?"
                stem = f.stem  # "BTC_USDT-15m" (no .json)
                pair = d.get("pair", stem.rsplit("-", 1)[0].replace("_", "/"))
                tf = d.get("timeframe", stem.rsplit("-", 1)[-1])
                out.append({
                    "pair": pair,
                    "timeframe": tf,
                    "range_start": lo_s,
                    "range_end": hi_s,
                    "chunks_complete": n_complete,
                    "chunks_empty": n_empty,
                    "updated": datetime.fromtimestamp(f.stat().st_mtime, tz=timezone.utc).strftime("%Y-%m-%d"),
                })
        out.sort(key=lambda x: (x["pair"], x["timeframe"]))
        return out

    # --------------------------------------------------------------- routes
    @app.get("/api/status")
    def status():
        from .. import runtime as rt

        host_freqtrade = None
        try:
            import freqtrade

            host_freqtrade = freqtrade.__version__
        except Exception:  # noqa: BLE001 - host may lack freqtrade (fine)
            pass
        selected = rt.diagnose(
            state["root"] if state["root"] != default_repo_root() else None
        )["selected"]
        return {
            "app": "nobitex-adapter",
            "version": __version__,
            "freqtrade": host_freqtrade,
            "mode": "BACKTEST / MARKET DATA MODE - NO REAL ORDERS",
            "exchange": "nobitex",
            "futures": False,
            "repo": str(state["root"]),
            "backtest_runtime": selected,
            "time": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }

    @app.get("/api/runtime")
    def runtime_info():
        """Full runtime diagnostic: host process + selected Freqtrade repo/venv."""
        from .. import runtime as rt

        return rt.diagnose(state["root"] if state["root"] != default_repo_root() else None)

    @app.post("/api/repo")
    def set_repo_endpoint(body: dict):
        """Rebind the GUI to a different Freqtrade repository at runtime."""
        try:
            return set_repo(body.get("repo"))
        except HTTPException:
            raise
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(400, str(exc))

    @app.get("/api/strategies")
    def strategies():
        return {"strategies": _strategies()}

    @app.post("/api/strategies/refresh")
    def strategies_refresh():
        return {"strategies": _strategies()}

    @app.get("/api/exchanges")
    def exchanges():
        return {
            "exchanges": [
                {"id": "nobitex", "name": "Nobitex", "spot": True, "futures": False,
                 "status": "ready", "note": "market data + backtest"},
                {"id": "lbank", "name": "LBank", "spot": True, "futures": False,
                 "status": "planned", "note": "coming later"},
            ]
        }

    @app.get("/api/markets")
    def markets():
        return {
            "markets": _markets(),
            "fetched_at": markets_cache.get("fetched_at"),
            "source": markets_cache.get("source"),
        }

    @app.get("/api/data-history")
    def data_history():
        return {"items": _data_history()}

    @app.post("/api/jobs")
    def create_job(req: JobRequest):
        kind = req.kind
        params = req.params
        if kind == "discover":
            job = jm.submit("discover", make_discover_job, params)
        elif kind == "download":
            job = jm.submit("download", lambda j: make_download_job(_paths(), j), params)
        elif kind == "validate":
            job = jm.submit("validate", lambda j: make_validate_job(_paths(), j), params)
        elif kind == "backtest":
            job = jm.submit("backtest", lambda j: make_backtest_job(_paths(), j), params)
        else:
            raise HTTPException(400, f"unknown job kind {kind!r}")
        return {"job_id": job.id}

    @app.get("/api/jobs")
    def list_jobs():
        return {"jobs": [j.to_dict() for j in jm.list()]}

    @app.get("/api/jobs/{job_id}")
    def get_job(job_id: str):
        job = jm.get(job_id)
        if not job:
            raise HTTPException(404, "no such job")
        return job.to_dict()

    @app.get("/api/jobs/{job_id}/log")
    def get_job_log(job_id: str, after: int = 0):
        job = jm.get(job_id)
        if not job:
            raise HTTPException(404, "no such job")
        lines = list(job.log)
        return {"job_id": job_id, "status": job.status,
                "lines": lines[after:], "total": len(lines)}

    @app.post("/api/jobs/{job_id}/cancel")
    def cancel_job(job_id: str):
        ok = jm.cancel(job_id)
        return {"cancelled": ok}

    @app.get("/api/presets")
    def presets():
        out = {}
        for p in ("1M", "3M", "6M", "1Y", "2Y", "3Y"):
            s, e = _preset_range(p)
            out[p] = {"start": s, "end": e}
        return {"presets": out}

    @app.get("/api/results/list")
    def results_list():
        from ..backtest import list_backtest_zips

        zips = list_backtest_zips(_paths()["user_data_dir"] / "backtest_results")
        normalized = []
        for z in zips:
            normalized.append({
                "name": z.get("file") or z.get("name"),
                "modified": z.get("timestamp") or z.get("modified") or "",
                "size": z.get("size", 0),
                "path": z.get("path", ""),
            })
        return {"results": normalized}

    @app.get("/api/results/latest")
    def results_latest():
        from ..backtest import latest_results_zip
        from ..results import load_latest_dashboard

        exportdir = _paths()["user_data_dir"] / "backtest_results"
        dash = load_latest_dashboard(exportdir, _paths()["results_dir"], datadir=_paths()["datadir"])
        if dash is None:
            raise HTTPException(404, "no backtest results yet")
        return dash

    @app.get("/api/results/{name}")
    def results_one(name: str):
        p = _paths()["results_dir"] / name
        if not p.is_file() or not p.name.endswith(".dashboard.json"):
            raise HTTPException(404, "no such result")
        return json.loads(p.read_text(encoding="utf-8"))

    # -------------------------------------------------------------- static
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    @app.get("/")
    def index():
        return FileResponse(STATIC_DIR / "index.html")

    return app


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(create_app(), host="127.0.0.1", port=8765)
