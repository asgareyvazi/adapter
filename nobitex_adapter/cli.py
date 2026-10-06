"""Command-line interface.

Every major operation available in the GUI is available here (scriptable).
The GUI calls the same service functions; nothing is duplicated.

Commands (also exposed as console scripts when installed with pip):
  nobitex-markets    [--quote USDT] [--json]
  nobitex-download   --pairs BTC/USDT,ETH/USDT --timeframes 5m,15m,1h,4h,1d
                     --start 2024-01-01 --end 2024-06-30 [options]
  nobitex-validate   --pairs ... --timeframes ... --start ... --end ...
  nobitex-backtest   --strategy NostalgiaForInfinityX8 --pairs ... --start ... --end ... [options]
  nobitex-ui         [--host 127.0.0.1] [--port 8765]
  nobitex-mock       [--host 127.0.0.1] [--port 8900]   (offline test server)
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from .configgen import DEFAULT_BLACKLIST_PATTERNS, default_repo_root, default_user_data_layout


def _setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s [%(name)s] %(message)s",
        datefmt="%H:%M:%S",
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("requests").setLevel(logging.WARNING)
    logging.getLogger("urllib3").setLevel(logging.WARNING)


def _parse_dt(s: str, *, default_now: bool = False) -> datetime:
    s = s.strip()
    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%d", "%Y-%m-%dT%H:%M", "%Y%m%d"):
        try:
            dt = datetime.strptime(s, fmt)
            return dt.replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    if default_now and s in ("now", ""):
        return datetime.now(timezone.utc)
    raise SystemExit(f"cannot parse date {s!r} (use YYYY-MM-DD or 'YYYY-MM-DD HH:MM', UTC)")


def _parse_pairs(s: str) -> list[str]:
    out = [p.strip().upper() for p in s.replace(";", ",").split(",") if p.strip()]
    for p in out:
        if "/" not in p:
            # allow nobitex-style symbols in the CLI
            from .symbols import nobitex_to_freqtrade
            try:
                out[out.index(p)] = nobitex_to_freqtrade(p)
            except Exception:
                raise SystemExit(f"bad pair {p!r} (use BASE/QUOTE or Nobitex symbol)")
    return out


def _parse_startup(s: Optional[str]) -> Optional[dict[str, int]]:
    if not s:
        return None
    out: dict[str, int] = {}
    for part in s.split(","):
        if not part.strip():
            continue
        tf, n = part.split(":")
        out[tf.strip()] = int(n)
    return out


# ---------------------------------------------------------------- markets
def cmd_markets(args) -> int:
    from .nobitex_client import NobitexClient

    _setup_logging(args.verbose)
    client = NobitexClient()
    try:
        markets = client.discover_markets(quote=args.quote)
    finally:
        client.close()
    if args.json:
        print(json.dumps([m.to_dict() for m in markets], indent=1))
    else:
        print(f"{'SYMBOL':<18} {'ACTIVE':<7} {'PRICE':>16} {'VOL(24h,quote)':>18} {'DAY%':>8}")
        for m in markets:
            print(
                f"{m.ft_symbol:<18} {str(m.active).lower():<7} "
                f"{m.price:>16.8g} {m.volume_quote:>18,.2f} {m.day_change_pct:>8.2f}"
            )
        print(f"\n{len(markets)} markets" + (f" (quote={args.quote})" if args.quote else ""))
    return 0


# --------------------------------------------------------------- download
def cmd_download(args) -> int:
    from .downloader import DownloadRequest, Downloader
    from .nobitex_client import NobitexClient

    _setup_logging(args.verbose)
    root = Path(args.repo) if args.repo else default_repo_root()
    paths = default_user_data_layout(root)
    pairs = _parse_pairs(args.pairs)
    tfs = [t.strip() for t in args.timeframes.split(",") if t.strip()]

    client = NobitexClient()

    last = {"line": ""}

    def progress(ev: dict) -> None:
        e = ev.get("event")
        if e == "page":
            last["line"] = (
                f"\r  {ev.get('pair')} {ev.get('timeframe')}  "
                f"chunk {ev.get('chunk')}/{ev.get('chunks_total')}  "
                f"candles {ev.get('candles'):,}    "
            )
            print(last["line"], end="", flush=True)
        elif e == "task_done":
            print()
            print(
                f"[ok] {ev.get('pair')} {ev.get('timeframe')}: "
                f"{ev.get('rows')} rows (+{ev.get('new_rows')}), validation={ev.get('validation')}"
            )
        elif e == "task_empty":
            print()
            print(f"[empty] {ev.get('pair')} {ev.get('timeframe')}: {ev.get('reason')}")

    dl = Downloader(client, paths["datadir"], paths["manifests_dir"], paths["reports_dir"], progress)
    try:
        summary = dl.download(
            DownloadRequest(
                pairs=pairs,
                timeframes=tfs,
                start=_parse_dt(args.start),
                end=_parse_dt(args.end, default_now=True),
                exchange=args.exchange,
                startup_candles=_parse_startup(args.startup),
                drop_incomplete_last=not args.keep_incomplete,
                force=args.force,
            )
        )
    finally:
        client.close()

    print(f"\nTotal rows: {summary.total_rows:,}")
    for t in summary.tasks:
        flag = "PASS" if (t.validation and t.validation.ok) else (t.status)
        print(f"  {t.pair} {t.timeframe}: {t.rows:,} rows [{flag}]")
    return 0 if summary.ok else 1


# ---------------------------------------------------------------- validate
def cmd_validate(args) -> int:
    from .downloader import Downloader

    _setup_logging(args.verbose)
    root = Path(args.repo) if args.repo else default_repo_root()
    paths = default_user_data_layout(root)
    pairs = _parse_pairs(args.pairs)
    tfs = [t.strip() for t in args.timeframes.split(",") if t.strip()]
    dl = Downloader(None, paths["datadir"], paths["manifests_dir"], paths["reports_dir"])  # type: ignore[arg-type]
    reports = dl.validate_dataset(
        exchange=args.exchange,
        pairs=pairs,
        timeframes=tfs,
        start=_parse_dt(args.start),
        end=_parse_dt(args.end, default_now=True),
        end_is_open=not args.closed_range,
    )
    all_ok = True
    for rep in reports:
        print()
        print(rep.render())
        if rep.status not in ("PASS", "PASS_WITH_GAPS"):
            all_ok = False
    return 0 if all_ok else 1


# ---------------------------------------------------------------- backtest
def cmd_backtest(args) -> int:
    from .backtest import run_backtest
    from .runtime import diagnose, format_diagnostic

    _setup_logging(args.verbose)
    root = Path(args.repo) if args.repo else default_repo_root()
    paths = default_user_data_layout(root)
    pairs = _parse_pairs(args.pairs)

    # We are (post re-exec) on the selected runtime; show exactly what will run.
    print(format_diagnostic(diagnose(root)))

    stake = args.stake
    if stake != "unlimited":
        try:
            stake = float(stake)
        except ValueError:
            raise SystemExit("--stake must be 'unlimited' or a number")

    def progress(ev: dict) -> None:
        e = ev.get("event")
        if e == "config_written":
            print(f"[config] {ev['config_path']}")
        elif e == "backtest_start":
            print(f"[backtest] {ev['strategy']} pairs={ev['pairs']} timerange={ev['timerange']}")
        elif e == "backtest_end":
            print(f"[backtest] finished in {ev['elapsed']}s (exit={ev['exit_code']})")

    try:
        res = run_backtest(
            strategy=args.strategy,
            pairs=pairs,
            start=_parse_dt(args.start),
            end=_parse_dt(args.end, default_now=True),
            user_data_dir=paths["user_data_dir"],
            datadir=paths["datadir"],
            strategies_dir=paths["strategies_dir"],
            configs_dir=paths["configs_dir"],
            results_dir=paths["results_dir"],
            stake_currency=args.stake_currency,
            initial_capital=args.capital,
            stake_amount=stake,
            max_open_trades=args.max_open,
            fee=None if args.fee == "" else float(args.fee),
            skip_precheck=args.skip_precheck,
            progress_cb=progress,
        )
    except Exception as exc:  # noqa: BLE001
        print(f"[error] {exc}", file=sys.stderr)
        return 1

    if args.out:
        Path(args.out).write_text(json.dumps(res, indent=1), encoding="utf-8")
        print(f"[out] {args.out}")
    if not res["ok"]:
        print(f"[error] {res.get('error')}", file=sys.stderr)
        return 1
    print(f"[done] results: {res['results_zip']}")
    return 0


# ---------------------------------------------------------------- doctor
def cmd_doctor(args) -> int:
    """Print the runtime diagnostic for the selected Freqtrade repo."""
    _setup_logging(args.verbose)
    from .runtime import AdapterRuntimeError, diagnose, format_diagnostic

    repo = Path(args.repo) if args.repo else None
    try:
        print(format_diagnostic(diagnose(repo)))
    except AdapterRuntimeError as exc:
        print(f"[error] {exc}", file=sys.stderr)
        return 1
    return 0


# ---------------------------------------------------------------- ui
def cmd_ui(args) -> int:
    _setup_logging(args.verbose)
    import uvicorn

    from .webui.app import create_app

    # The GUI always knows which Freqtrade repo it is bound to (default: auto).
    # Backtest jobs run as a subprocess that re-execs into that repo's .venv.
    root = Path(args.repo) if args.repo else None
    app = create_app(root=root)

    from .runtime import diagnose, format_diagnostic

    print(format_diagnostic(diagnose(root)))
    print(f"Nobitex Backtest Manager UI  ->  http://{args.host}:{args.port}")
    print("BACKTEST / MARKET DATA MODE - NO REAL ORDERS")
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")
    return 0


# ---------------------------------------------------------------- mock
def cmd_mock(args) -> int:
    _setup_logging(args.verbose)
    import uvicorn

    from .mockserver import app as mock_app

    print(f"Mock Nobitex public API  ->  http://{args.host}:{args.port}")
    uvicorn.run(mock_app, host=args.host, port=args.port, log_level="warning")
    return 0


# ---------------------------------------------------------------- main
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="nobitex", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument(
        "--repo", default=None,
        help="Freqtrade repository root containing .venv + user_data "
             "(default: auto = the adapter checkout)",
    )
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="command", required=True)

    pm = sub.add_parser("markets", help="discover Nobitex markets")
    pm.add_argument("--quote", default="USDT", help="filter by quote asset (default USDT)")
    pm.add_argument("--json", action="store_true")
    pm.set_defaults(func=cmd_markets)

    pdl = sub.add_parser("download", help="download historical OHLCV")
    pdl.add_argument("--pairs", required=True)
    pdl.add_argument("--timeframes", required=True, help="comma list, e.g. 5m,15m,1h,4h,1d")
    pdl.add_argument("--start", required=True)
    pdl.add_argument("--end", default="now")
    pdl.add_argument("--exchange", default="nobitex")
    pdl.add_argument("--force", action="store_true", help="ignore resume manifest")
    pdl.add_argument("--keep-incomplete", action="store_true",
                     help="keep the still-open last candle")
    pdl.add_argument("--startup", default=None, help="tf:candles overrides, e.g. 5m:850,1d:260")
    pdl.set_defaults(func=cmd_download)

    pv = sub.add_parser("validate", help="validate stored data")
    pv.add_argument("--pairs", required=True)
    pv.add_argument("--timeframes", required=True)
    pv.add_argument("--start", required=True)
    pv.add_argument("--end", default="now")
    pv.add_argument("--exchange", default="nobitex")
    pv.add_argument("--closed-range", action="store_true",
                    help="end is a closed period (last candle complete)")
    pv.set_defaults(func=cmd_validate)

    pb = sub.add_parser("backtest", help="run a Freqtrade backtest")
    pb.add_argument("--strategy", required=True)
    pb.add_argument("--pairs", required=True)
    pb.add_argument("--start", required=True)
    pb.add_argument("--end", default="now")
    pb.add_argument("--stake-currency", default="USDT")
    pb.add_argument("--capital", type=float, default=10_000.0)
    pb.add_argument("--stake", default="unlimited")
    pb.add_argument("--max-open", type=int, default=8)
    pb.add_argument("--fee", default="0.002", help="'0.002' or '' for exchange default")
    pb.add_argument("--skip-precheck", action="store_true")
    pb.add_argument("--out", default=None, help="write result descriptor JSON here")
    pb.set_defaults(func=cmd_backtest)

    pu = sub.add_parser("ui", help="start the web UI")
    pu.add_argument("--host", default="127.0.0.1")
    pu.add_argument("--port", type=int, default=8765)
    pu.set_defaults(func=cmd_ui)

    pmk = sub.add_parser("mock", help="start the mock Nobitex API (offline testing)")
    pmk.add_argument("--host", default="127.0.0.1")
    pmk.add_argument("--port", type=int, default=8900)
    pmk.set_defaults(func=cmd_mock)

    pd_ = sub.add_parser("doctor", help="print the runtime diagnostic for --repo")
    pd_.set_defaults(func=cmd_doctor)

    return p


# commands that must run inside the SELECTED Freqtrade runtime
RUNTIME_COMMANDS = {"backtest"}


def main(argv: Optional[list[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    # Bind to the selected Freqtrade runtime BEFORE importing Freqtrade.
    # If `--repo` points at a Freqtrade clone whose .venv differs from the
    # current interpreter, transparently re-exec the whole CLI with that
    # venv's python so the backtest uses the user's real Freqtrade install.
    if args.command in RUNTIME_COMMANDS and args.repo:
        from .runtime import reexec_into_runtime

        rc = reexec_into_runtime(Path(args.repo))
        if rc is not None:
            # the whole CLI was re-run under the selected repo's venv python;
            # propagate its exit code and do NOT run the command here.
            sys.exit(rc)

    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
