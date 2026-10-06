"""Backtest orchestration: data pre-checks -> config -> Freqtrade -> results.

The GUI and CLI both call :func:`run_backtest` (or the CLI command), so there
is exactly one implementation of the backtest pipeline.
"""
from __future__ import annotations

import json
import logging
import re
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from .configgen import (
    btc_informative_pair,
    build_backtest_config,
    detect_strategy_timeframes,
    required_timeframes,
    write_backtest_config,
)
from .downloader import data_filename, pair_to_filename
from .freqtrade_bootstrap import run_freqtrade

log = logging.getLogger("nobitex.backtest")


class BacktestError(RuntimeError):
    pass


@dataclass
class DataPrecheck:
    ok: bool = True
    missing: list = field(default_factory=list)  # (pair, tf, path)
    required_pairs: list = field(default_factory=list)
    required_timeframes: list = field(default_factory=list)
    btc_pair: str = ""
    base_timeframe: str = ""
    notes: list = field(default_factory=list)


def _search_strategies_dir(strategies_dir: Path, name: str) -> Optional[Path]:
    """Find ``<name>.py`` under a strategies dir (recursive, deterministic).

    Handles the nested NostalgiaForInfinity layout
    (``user_data/strategies/NostalgiaForInfinity/NostalgiaForInfinityX8.py``)
    as well as flat layouts. Shallowest match wins; ties break
    lexicographically. ``__pycache__`` and hidden dirs are skipped.
    """
    if not strategies_dir.is_dir():
        return None
    matches = []
    for f in strategies_dir.rglob(f"{name}.py"):
        try:
            rel = f.relative_to(strategies_dir)
        except ValueError:
            continue
        if any(part in ("__pycache__", ".git") or part.startswith(".") for part in rel.parts):
            continue
        if f.is_file():
            matches.append(f)
    if not matches:
        return None
    matches.sort(key=lambda p: (len(p.relative_to(strategies_dir).parts), str(p)))
    return matches[0]


def bundled_strategies_dir() -> Path:
    """The adapter checkout's own strategies dir (bundled fallback)."""
    return Path(__file__).resolve().parent.parent / "user_data" / "strategies"


def find_strategy(strategies_dir: Path, name: str) -> tuple[Path, str]:
    """Resolve a strategy by name.

    Resolution order (documented, deterministic):
      1. the SELECTED Freqtrade repo's ``user_data/strategies`` (recursive,
         so nested strategy-repo layouts like NostalgiaForInfinity work)
      2. the adapter checkout's bundled ``user_data/strategies`` (fallback)

    Returns ``(path, source_note)`` where source_note is "" for the selected
    repo or "bundled fallback" when the bundled copy was used.
    """
    strategies_dir = Path(strategies_dir)
    hit = _search_strategies_dir(strategies_dir, name)
    if hit is not None:
        return hit, ""
    fallback = _search_strategies_dir(bundled_strategies_dir(), name)
    if fallback is not None:
        return fallback, (
            f"not found under {strategies_dir}; using bundled copy "
            f"{fallback}"
        )
    raise BacktestError(
        f"strategy {name!r} not found (searched {strategies_dir} recursively "
        f"and bundled {bundled_strategies_dir()})"
    )


def _count_candles_before(path: Path, start_ts: int) -> Optional[int]:
    """Count stored candles strictly before `start_ts` (reads the date column only)."""
    try:
        import pandas as pd

        df = pd.read_feather(path, columns=["date"])
        d = pd.to_datetime(df["date"], utc=True)
        ts = d.astype("int64") // 10**9  # ns since epoch (UTC) -> s
        return int((ts < start_ts).sum())
    except Exception:  # noqa: BLE001 - unreadable file: skip the check
        return None


def precheck_data(
    datadir: Path,
    exchange: str,
    strategy_file: Path,
    pairs: list[str],
    stake_currency: str = "USDT",
    start: Optional[datetime] = None,
) -> DataPrecheck:
    """Determine every (pair, timeframe) Freqtrade will need and check files.

    When `start` is given and the strategy declares `startup_candle_count`,
    the precheck also verifies each required dataset carries enough
    history BEFORE the timerange start (Freqtrade's indicator warmup) and
    records shortfalls as notes (Freqtrade itself decides whether to fail).
    """
    det = detect_strategy_timeframes(strategy_file)
    base_tf = det["timeframe"] or "5m"
    info_tfs = det["info_timeframes"] or []
    btc_info_tfs = det["btc_info_timeframes"] or []
    startup = det.get("startup_candle_count")

    from .timeframes import parse_timeframe

    base_secs = parse_timeframe(base_tf).seconds

    required_tfs = required_timeframes(base_tf, info_tfs, btc_info_tfs, stake_currency)
    btc_pair = btc_informative_pair(stake_currency)
    all_pairs = list(dict.fromkeys(list(pairs) + [btc_pair]))

    pre = DataPrecheck(
        required_pairs=all_pairs,
        required_timeframes=required_tfs,
        btc_pair=btc_pair,
        base_timeframe=base_tf,
    )
    missing: list[tuple[str, str, Path]] = []
    for pair in all_pairs:
        # The BTC informative pair only needs its own informative timeframes
        # (X8: 4h), unless it is also one of the trading pairs.
        if pair == btc_pair and btc_pair not in pairs and btc_info_tfs:
            tfs_for_pair = list(btc_info_tfs)
        else:
            tfs_for_pair = required_tfs
        for tf in tfs_for_pair:
            path = data_filename(datadir, exchange, pair, tf)
            if not path.is_file():
                pre.ok = False
                missing.append((pair, tf, str(path)))
                continue
            # startup-history check (warn only; Freqtrade is authoritative)
            if start is not None and startup:
                tf_secs = parse_timeframe(tf).seconds
                need = startup if tf == base_tf else max(
                    1, int(startup * base_secs // tf_secs) + 1
                )
                have = _count_candles_before(path, int(start.timestamp()))
                if have is not None and have < need:
                    pre.notes.append(
                        f"insufficient startup history for {pair} {tf}: "
                        f"have {have} candles before {start:%Y-%m-%d}, "
                        f"strategy needs ~{need} (download from an earlier start)"
                    )
    pre.missing = missing
    if not det["timeframe"]:
        pre.notes.append(
            "strategy has no explicit `timeframe` attribute; assumed "
            f"{base_tf}. Verify manually for non-X8 strategies."
        )
    return pre


def run_backtest(
    *,
    strategy: str,
    pairs: list[str],
    start: datetime,
    end: datetime,
    user_data_dir: Path,
    datadir: Path,
    strategies_dir: Path,
    configs_dir: Path,
    results_dir: Path,
    stake_currency: str = "USDT",
    initial_capital: float = 10_000.0,
    stake_amount: object = "unlimited",
    max_open_trades: int = 8,
    fee: Optional[float] = 0.002,
    blacklist: Optional[list[str]] = None,
    advanced: Optional[dict] = None,
    exchange: str = "nobitex",
    skip_precheck: bool = False,
    progress_cb=None,
    stop_event=None,
) -> dict:
    """Run a full backtest and return a result descriptor.

    Result descriptor keys:
        ok, strategy, config_path, results_zip, exit_code, elapsed, error,
        log (list[str] tail)
    """
    t0 = time.time()
    if progress_cb is None:
        emit = lambda **ev: None  # noqa: E731
    else:
        emit = lambda **ev: progress_cb(ev)  # noqa: E731

    strategy_file, strategy_note = find_strategy(strategies_dir, strategy)
    if strategy_note:
        emit(event="strategy_note", note=strategy_note)
        log.warning("strategy resolution: %s", strategy_note)
    emit(event="strategy_resolved", path=str(strategy_file))

    # Freqtrade resolves strategies FLAT under user_data/strategies, but real
    # strategy repos (e.g. NostalgiaForInfinity cloned as a subfolder) keep
    # their files one level deeper. Expose a nested file via a symlink named
    # like the module so Freqtrade can import it. NEVER overwrites an
    # existing real file; only manages adapter-created symlinks.
    try:
        flat = strategies_dir / strategy_file.name
        if strategy_file.resolve().parent != strategies_dir.resolve():
            if not flat.exists() and not flat.is_symlink():
                flat.symlink_to(strategy_file.resolve())
                emit(event="strategy_link", file=str(flat),
                     target=str(strategy_file.resolve()))
            elif flat.is_symlink() and flat.resolve() != strategy_file.resolve():
                flat.unlink()
                flat.symlink_to(strategy_file.resolve())
                emit(event="strategy_link", file=str(flat),
                     target=str(strategy_file.resolve()))
    except OSError as exc:
        log.warning("could not symlink strategy into flat dir: %s", exc)
    if not skip_precheck:
        pre = precheck_data(
            datadir, exchange, strategy_file, pairs, stake_currency, start=start
        )
        for note in pre.notes:
            emit(event="precheck_note", note=note)
            log.warning("precheck: %s", note)
        if not pre.ok:
            raise BacktestError(
                "required data is missing:\n  "
                + "\n  ".join(f"{p} {tf} -> {path}" for p, tf, path in pre.missing[:40])
                + f"\nDownload these first (timeframes needed: {', '.join(pre.required_timeframes)})"
            )

    config = build_backtest_config(
        strategy=strategy,
        pairs=pairs,
        base_timeframe=pre.base_timeframe if not skip_precheck else _base_tf(strategy_file),
        start=start,
        end=end,
        user_data_dir=user_data_dir,
        datadir=datadir,
        strategies_dir=strategies_dir,
        stake_currency=stake_currency,
        initial_capital=initial_capital,
        stake_amount=stake_amount,
        max_open_trades=max_open_trades,
        fee=fee,
        blacklist=blacklist,
        advanced=advanced,
    )
    config_path = write_backtest_config(config, configs_dir, tag=strategy)
    emit(event="config_written", config_path=str(config_path))

    # Freqtrade's exportdirectory must exist
    exportdir = user_data_dir / "backtest_results"
    exportdir.mkdir(parents=True, exist_ok=True)
    config["exportdirectory"] = str(exportdir)

    argv = [
        "backtesting",
        "--config", str(config_path),
        "--strategy", strategy,
        "--export", "trades",
    ]
    emit(event="backtest_start", strategy=strategy, pairs=len(pairs),
         timerange=config["timerange"])
    log.info("running freqtrade: %s", " ".join(argv))
    exit_code = run_freqtrade(argv)
    elapsed = time.time() - t0
    emit(event="backtest_end", exit_code=exit_code, elapsed=round(elapsed, 1))

    runtime = process_runtime()
    if exit_code != 0:
        return {
            "ok": False, "strategy": strategy, "config_path": str(config_path),
            "results_zip": None, "exit_code": exit_code, "elapsed": round(elapsed, 1),
            "error": f"freqtrade exited with code {exit_code}", "runtime": runtime,
        }

    zip_path = latest_results_zip(exportdir)
    if zip_path is None:
        return {
            "ok": False, "strategy": strategy, "config_path": str(config_path),
            "results_zip": None, "exit_code": exit_code, "elapsed": round(elapsed, 1),
            "error": "freqtrade finished but no results zip was found",
            "runtime": runtime,
        }

    return {
        "ok": True, "strategy": strategy, "config_path": str(config_path),
        "results_zip": str(zip_path), "exit_code": 0, "elapsed": round(elapsed, 1),
        "error": None, "runtime": runtime,
    }


def _base_tf(strategy_file: Path) -> str:
    det = detect_strategy_timeframes(strategy_file)
    return det["timeframe"] or "5m"


def process_runtime() -> dict:
    """Identify the Python/Freqtrade runtime of THIS process.

    Included in every backtest result descriptor so the user (CLI log and
    GUI job details) can verify WHICH Freqtrade actually ran the backtest.
    """
    from . import __version__
    from .runtime import adapter_package_root

    info: dict = {
        "adapter_version": __version__,
        "python": sys.executable,
        "python_version": sys.version.split()[0],
        "adapter_root": str(adapter_package_root()),
        "freqtrade": None,
        "freqtrade_module": None,
        "ccxt": None,
    }
    try:
        import freqtrade

        info["freqtrade"] = getattr(freqtrade, "__version__", None)
        info["freqtrade_module"] = getattr(freqtrade, "__file__", None)
    except Exception:  # noqa: BLE001
        pass
    try:
        import ccxt

        info["ccxt"] = getattr(ccxt, "__version__", None)
    except Exception:  # noqa: BLE001
        pass
    return info


def latest_results_zip(exportdir: Path) -> Optional[Path]:
    """Resolve the latest backtest zip via Freqtrade's latest_backtest.json."""
    latest = exportdir / "latest_backtest.json"
    if latest.is_file():
        try:
            name = json.loads(latest.read_text(encoding="utf-8")).get("latest_backtest")
            if name:
                p = exportdir / name
                if p.is_file():
                    return p
        except (OSError, json.JSONDecodeError):
            pass
    # fallback: newest zip
    zips = sorted(exportdir.glob("backtest-result-*.zip"), key=lambda p: p.stat().st_mtime)
    return zips[-1] if zips else None


def list_backtest_zips(exportdir: Path) -> list[dict]:
    out = []
    for p in sorted(exportdir.glob("backtest-result-*.zip"), reverse=True):
        m = re.search(r"backtest-result-(\d{4}-\d{2}-\d{2}_\d{2}-\d{2}-\d{2})\.zip", p.name)
        out.append({
            "file": p.name,
            "path": str(p),
            "timestamp": m.group(1) if m else "",
            "size": p.stat().st_size,
        })
    return out[:50]
