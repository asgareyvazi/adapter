"""Cross-run backtest comparison with run registry (schema v1).

Every executed backtest registers a run record
(``<results_dir>/runs/<run_id>.run.json``) capturing WHAT ran (strategy,
exchange, pairs, timeframes, timerange, effective config hash, strategy
source hash, input-data fingerprints) and WHAT came out (results zip,
dashboard summary, runtime versions). ``compare_runs`` then builds a
side-by-side metric table over any set of registered runs plus explicit
INCOMPATIBILITY WARNINGS wherever the numbers are not directly comparable
(different ranges/pairs/timeframes/data/configs).

Schemas (both versioned, both JSON):

  * run record:      ``{"schema": 1, "run_id": ..., "spec": ..., ...}``
  * comparison:      ``{"schema": 1, "runs": [...], "warnings": [...]}``

Run IDs are content-addressed: ``<strategy>-<exchange>-<stamp>-<hash8>``
where ``hash8`` covers the full spec + strategy source + data fingerprints.
Two executions of the identical spec in the same second share an ID (the
later registration wins — Freqtrade is deterministic, so there is nothing
to compare between them).

Only EXECUTED runs register: pre-execution failures (missing data, unknown
strategy) raise before any run record exists. Failed executions DO register
(``status: "failed"``) so broken runs show up in comparisons explicitly
instead of silently disappearing.
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

log = logging.getLogger("nobitex.compare")

RUN_SCHEMA_VERSION = 1
COMPARISON_SCHEMA_VERSION = 1

RUNS_SUBDIR = "runs"

# Config keys that depend on the machine/checkout (excluded from config_sha
# so the same logical run on two machines hashes identically).
_MACHINE_CONFIG_KEYS = frozenset({
    "strategy_path", "datadir", "user_data_dir", "exportdirectory",
})

_SUMMARY_KEYS = (
    "strategy", "backtest_start", "backtest_end", "backtest_days",
    "timeframe", "pairlist", "starting_balance", "final_balance",
    "cards", "trades_count", "buy_hold", "per_pair", "exit_reasons",
)


def _slug(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9]+", "_", value).strip("_") or "run"


def _canonical(obj) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str)


def config_sha256(config: dict) -> str:
    """Hash the EFFECTIVE config (machine-specific paths excluded)."""
    trimmed = {k: v for k, v in config.items() if k not in _MACHINE_CONFIG_KEYS}
    return "sha256:" + hashlib.sha256(_canonical(trimmed).encode("utf-8")).hexdigest()


def file_sha256(path: str | Path) -> Optional[str]:
    try:
        h = hashlib.sha256()
        with open(path, "rb") as fh:
            for chunk in iter(lambda: fh.read(1 << 20), b""):
                h.update(chunk)
        return "sha256:" + h.hexdigest()
    except OSError:
        return None


def make_run_id(spec: dict, *, stamp: Optional[str] = None) -> str:
    """Content-addressed run ID for a spec (deterministic given stamp)."""
    stamp = stamp or datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    digest = hashlib.sha1(_canonical(spec).encode("utf-8")).hexdigest()[:8]
    return f"{_slug(spec.get('strategy', ''))}-{_slug(spec.get('exchange', ''))}-{stamp}-{digest}"


def runs_dir(results_dir: str | Path) -> Path:
    return Path(results_dir) / RUNS_SUBDIR


def data_fingerprints(
    datadir: str | Path, exchange: str, pairs: list[str], timeframes: list[str]
) -> dict[str, Optional[dict]]:
    """Fingerprint every input feather file (missing/unreadable -> None)."""
    from .datacontract import fingerprint_file
    from .downloader import data_filename

    out: dict[str, Optional[dict]] = {}
    for pair in pairs:
        for tf in timeframes:
            key = f"{pair} {tf}"
            path = data_filename(Path(datadir), exchange, pair, tf)
            try:
                out[key] = fingerprint_file(path) if path.is_file() else None
            except Exception as exc:  # noqa: BLE001 - never break registration
                log.warning("could not fingerprint %s: %s", path, exc)
                out[key] = None
    return out


def summarize_zip(zip_path: str | Path, datadir: Optional[str | Path] = None) -> dict:
    """Parse a results zip into the trimmed summary stored per run."""
    from .results import parse_backtest_zip

    dash = parse_backtest_zip(Path(zip_path), datadir=Path(datadir) if datadir else None)
    return {k: dash.get(k) for k in _SUMMARY_KEYS}


def register_run(
    results_dir: str | Path,
    *,
    spec: dict,
    status: str,
    config: Optional[dict] = None,
    config_path: Optional[str | Path] = None,
    results_zip: Optional[str | Path] = None,
    strategy_file: Optional[str | Path] = None,
    datadir: Optional[str | Path] = None,
    runtime: Optional[dict] = None,
    elapsed: Optional[float] = None,
    error: Optional[str] = None,
    stamp: Optional[str] = None,
) -> dict:
    """Write (or overwrite) the run record for a spec; return the record.

    ``spec`` keys: strategy, exchange, pairs, timeframes, start, end (+ the
    numeric config surface: stake_currency, initial_capital, stake_amount,
    max_open_trades, fee). ``status`` is ``"ok"`` or ``"failed"``.
    Registration NEVER raises for I/O/parse problems: failures degrade to
    ``summary: None`` / ``data_fingerprints: {}`` with a ``notes`` entry, so
    a broken registration can never fail or hide a backtest result.
    """
    notes: list[str] = []
    record_spec = dict(spec)
    if config is not None:
        try:
            record_spec["config_sha256"] = config_sha256(config)
        except Exception as exc:  # noqa: BLE001
            notes.append(f"config hash failed: {exc}")
    strategy_sha = file_sha256(strategy_file) if strategy_file else None
    if strategy_file and strategy_sha is None:
        notes.append(f"strategy file unreadable: {strategy_file}")

    fps: dict[str, Optional[dict]] = {}
    if datadir is not None:
        try:
            fps = data_fingerprints(
                datadir, str(spec.get("exchange", "")),
                list(spec.get("pairs") or []), list(spec.get("timeframes") or []),
            )
        except Exception as exc:  # noqa: BLE001
            notes.append(f"data fingerprinting failed: {exc}")

    summary: Optional[dict] = None
    if status == "ok" and results_zip:
        try:
            summary = summarize_zip(results_zip, datadir)
        except Exception as exc:  # noqa: BLE001
            notes.append(f"results parse failed: {exc}")

    run_id = make_run_id(
        {**record_spec, "strategy_sha256": strategy_sha, "data": fps}, stamp=stamp
    )
    record = {
        "schema": RUN_SCHEMA_VERSION,
        "run_id": run_id,
        "status": status,
        "spec": record_spec,
        "strategy_sha256": strategy_sha,
        "data_fingerprints": fps,
        "summary": summary,
        "config_path": str(config_path) if config_path else None,
        "results_zip": str(results_zip) if results_zip else None,
        "runtime": runtime or {},
        "elapsed": elapsed,
        "error": error,
        "notes": notes,
        "registered_at": datetime.now(timezone.utc).isoformat(),
    }
    rdir = runs_dir(results_dir)
    rdir.mkdir(parents=True, exist_ok=True)
    path = rdir / f"{run_id}.run.json"
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(record, indent=1, default=str), encoding="utf-8")
    tmp.replace(path)
    return record


def list_runs(results_dir: str | Path) -> list[dict]:
    """Load all run records, newest first (corrupt files are skipped)."""
    out: list[dict] = []
    rdir = runs_dir(results_dir)
    if not rdir.is_dir():
        return out
    for path in sorted(rdir.glob("*.run.json")):
        try:
            rec = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            log.warning("skipping unreadable run record %s: %s", path, exc)
            continue
        if not isinstance(rec, dict) or "run_id" not in rec:
            log.warning("skipping malformed run record %s", path)
            continue
        out.append(rec)
    out.sort(key=lambda r: (r.get("registered_at", ""), r.get("run_id", "")), reverse=True)
    return out


def load_run(results_dir: str | Path, run_id: str) -> dict:
    """Load one run record by ID (ValueError when unknown)."""
    path = runs_dir(results_dir) / f"{run_id}.run.json"
    if not path.is_file():
        known = [r["run_id"] for r in list_runs(results_dir)][:10]
        hint = f" (known: {', '.join(known)})" if known else " (no runs registered)"
        raise ValueError(f"unknown run {run_id!r}{hint}")
    return json.loads(path.read_text(encoding="utf-8"))


def _warn(code: str, severity: str, message: str, runs: list[str]) -> dict:
    return {"code": code, "severity": severity, "message": message, "runs": sorted(runs)}


def compare_runs(results_dir: str | Path, run_ids: list[str]) -> dict:
    """Compare registered runs: metric table + incompatibility warnings."""
    if not run_ids:
        raise ValueError("no runs given (list them with `compare --list`)")
    # dedupe, preserve order
    ids = list(dict.fromkeys(run_ids))
    records = [load_run(results_dir, rid) for rid in ids]

    rows: list[dict] = []
    warnings: list[dict] = []
    for rec in records:
        spec = rec.get("spec", {})
        summary = rec.get("summary") or {}
        cards = summary.get("cards") or {}
        rows.append({
            "run_id": rec.get("run_id"),
            "status": rec.get("status"),
            "strategy": spec.get("strategy"),
            "exchange": spec.get("exchange"),
            "pairs": list(spec.get("pairs") or []),
            "timeframes": list(spec.get("timeframes") or []),
            "timerange": [spec.get("start"), spec.get("end")],
            "trades": summary.get("trades_count"),
            "return_pct": cards.get("return_pct"),
            "max_drawdown_pct": cards.get("max_drawdown_pct"),
            "win_rate_pct": cards.get("win_rate_pct"),
            "profit_factor": cards.get("profit_factor"),
            "sharpe": cards.get("sharpe"),
            "cagr_pct": cards.get("cagr_pct"),
            "market_change_pct": cards.get("market_change_pct"),
            "buy_hold_pct": (summary.get("buy_hold") or {}).get("equal_weight_buy_hold_pct"),
            "initial_capital": spec.get("initial_capital"),
            "fee": spec.get("fee"),
            "freqtrade": (rec.get("runtime") or {}).get("freqtrade"),
            "adapter": (rec.get("runtime") or {}).get("adapter_version"),
            "registered_at": rec.get("registered_at"),
            "error": rec.get("error"),
        })

    if len(ids) == 1:
        warnings.append(_warn("SINGLE_RUN", "info",
                              "only one run selected: nothing to compare against", ids))

    failed = [r for r in records if r.get("status") != "ok" or not r.get("summary")]
    if failed:
        warnings.append(_warn(
            "FAILED_RUN", "high",
            "failed runs have no metrics and are excluded from comparison: "
            + ", ".join(f"{r['run_id']} ({r.get('error') or 'no summary'})" for r in failed),
            [r["run_id"] for r in failed],
        ))

    ok_records = [r for r in records if r.get("status") == "ok" and r.get("summary")]
    if len(ok_records) >= 2:
        warnings.extend(_spec_warnings(ok_records))
        warnings.extend(_data_warnings(ok_records))
        warnings.extend(_runtime_warnings(ok_records))

    return {
        "schema": COMPARISON_SCHEMA_VERSION,
        "runs": rows,
        "warnings": warnings,
        "compared_at": datetime.now(timezone.utc).isoformat(),
    }


def _groups(records: list[dict], key: list[str]) -> dict[str, list[str]]:
    groups: dict[str, list[str]] = {}
    for rec in records:
        node: object = rec
        for part in key:
            node = (node or {}).get(part) if isinstance(node, dict) else None
        groups.setdefault(_canonical(node), []).append(rec["run_id"])
    return groups


def _spec_warnings(records: list[dict]) -> list[dict]:
    out: list[dict] = []
    ids = [r["run_id"] for r in records]

    def _differs(key: list[str]) -> bool:
        return len(_groups(records, key)) > 1

    if _differs(["spec", "start"]) or _differs(["spec", "end"]):
        ranges = {r["run_id"]: [r["spec"].get("start"), r["spec"].get("end")] for r in records}
        out.append(_warn("RANGE_DIFF", "high",
                         f"timeranges differ, returns are not directly comparable: {ranges}", ids))
    if _differs(["spec", "pairs"]):
        out.append(_warn("PAIRS_DIFF", "high",
                         "pair lists differ, per-pair exposure is not comparable", ids))
    if _differs(["spec", "timeframes"]):
        out.append(_warn("TIMEFRAMES_DIFF", "high",
                         "timeframe sets differ, signal data is not comparable", ids))
    if _differs(["spec", "exchange"]):
        out.append(_warn("EXCHANGE_DIFF", "medium",
                         "runs span multiple exchanges: fees, liquidity and data "
                         "provenance differ; compare shapes, not pennies", ids))
    if _differs(["spec", "initial_capital"]):
        out.append(_warn("CAPITAL_DIFF", "medium",
                         "initial capital differs: compare percentages, not absolute profit", ids))
    if _differs(["spec", "fee"]):
        out.append(_warn("FEE_DIFF", "medium", "trading fees differ", ids))
    if _differs(["spec", "stake_amount"]) or _differs(["spec", "max_open_trades"]):
        out.append(_warn("STAKE_DIFF", "medium",
                         "stake sizing or max-open-trades differ", ids))
    if _differs(["spec", "config_sha256"]):
        out.append(_warn("CONFIG_DIFF", "medium",
                         "effective configs differ beyond the compared fields "
                         "(advanced overrides?) — inspect config_sha256", ids))
    return out


def _data_warnings(records: list[dict]) -> list[dict]:
    out: list[dict] = []
    ids = [r["run_id"] for r in records]
    by_key: dict[str, dict[str, list[str]]] = {}
    for rec in records:
        for key, fp in (rec.get("data_fingerprints") or {}).items():
            sha = (fp or {}).get("sha256") if isinstance(fp, dict) else None
            by_key.setdefault(key, {}).setdefault(str(sha), []).append(rec["run_id"])
    changed = {k: v for k, v in by_key.items() if len(v) > 1}
    if changed:
        detail = "; ".join(
            f"{k}: {', '.join(f'{sorted(ids)}={sha[:19]}' for sha, ids in v.items())}"
            for k, v in sorted(changed.items()))
        out.append(_warn("DATA_FP_DIFF", "high",
                         "input data changed between runs for the same dataset "
                         f"(re-downloaded/extended?): {detail}", ids))
    return out


def _runtime_warnings(records: list[dict]) -> list[dict]:
    out: list[dict] = []
    ids = [r["run_id"] for r in records]
    if len({_canonical((r.get("runtime") or {}).get("freqtrade")) for r in records}) > 1:
        versions = {r["run_id"]: (r.get("runtime") or {}).get("freqtrade") for r in records}
        out.append(_warn("FREQTRADE_DIFF", "low",
                         f"freqtrade versions differ: {versions}", ids))
    if len({_canonical((r.get("runtime") or {}).get("adapter_version")) for r in records}) > 1:
        out.append(_warn("ADAPTER_DIFF", "low", "adapter versions differ", ids))
    if len({_canonical(r.get("strategy_sha256")) for r in records}) > 1:
        out.append(_warn("STRATEGY_SRC_DIFF", "medium",
                         "strategy SOURCES differ (same name, different file) — "
                         "this is not an apples-to-apples strategy comparison", ids))
    return out
