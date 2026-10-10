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

from . import __version__
from .configgen import (
    DEFAULT_BLACKLIST_PATTERNS,
    DEFAULT_SPOT_FEE,
    default_repo_root,
    default_user_data_layout,
)


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
    from .providers import get_provider

    _setup_logging(args.verbose)
    provider = get_provider(args.exchange)
    try:
        markets = provider.discover_markets(quote=args.quote)
    finally:
        provider.close()
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
    from .providers import get_provider
    from .timeframes import TimeframeError, normalize_timeframes

    _setup_logging(args.verbose)
    root = Path(args.repo) if args.repo else default_repo_root()
    paths = default_user_data_layout(root)
    pairs = _parse_pairs(args.pairs)
    try:
        tfs = normalize_timeframes(
            args.timeframes, param_name="--timeframes", exchange=args.exchange
        )
    except TimeframeError as exc:
        print(f"error: {exc}")
        return 2

    provider = get_provider(args.exchange)

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
            print(f"[zero-data FAIL] {ev.get('pair')} {ev.get('timeframe')}")
            for line in str(ev.get("reason", "")).splitlines():
                print(f"  {line}")

    dl = Downloader(provider, paths["datadir"], paths["manifests_dir"], paths["reports_dir"], progress)
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
        provider.close()

    print(f"\nTotal rows: {summary.total_rows:,}")
    for t in summary.tasks:
        flag = "PASS" if (t.validation and t.validation.ok) else (t.status)
        print(f"  {t.pair} {t.timeframe}: {t.rows:,} rows [{flag}]")
    return 0 if summary.ok else 1


# ------------------------------------------------------------------ probe
def cmd_ohlcv_probe(args) -> int:
    """Raw public-OHLCV diagnostic: inspect the exact request/response for a
    pair/timeframe/range, then auto-run a battery of probes that pin down
    WHY a range comes back empty (history depth vs range limit vs symbol).

    PUBLIC endpoint only — no auth, no private data.

    This is the Nobitex raw diagnostic. For other exchanges (or the
    generic quality report) use the `probe` command instead.
    """
    if getattr(args, "exchange", "nobitex") != "nobitex":
        print("error: ohlcv-probe is Nobitex-specific; for other exchanges use:\n"
              "  python -m nobitex_adapter --exchange EXCHANGE probe "
              "--pair PAIR --timeframes TFS --start START --end END")
        return 2
    from .nobitex_client import NobitexClient, NobitexNoData, NobitexError
    from .timeframes import (
        TimeframeError,
        normalize_timeframes,
        parse_timeframe,
        to_nobitex_resolution,
    )

    _setup_logging(args.verbose)
    try:
        tfs = normalize_timeframes([args.timeframe], param_name="--timeframe")
    except TimeframeError as exc:
        print(f"error: {exc}")
        return 2
    tf = tfs[0]
    interval = parse_timeframe(tf).seconds
    resolution = to_nobitex_resolution(tf)
    sym = args.pair.replace("/", "")

    client = NobitexClient()
    end_ts = int(_parse_dt(args.end, default_now=True).timestamp())
    start_ts: Optional[int] = (
        int(_parse_dt(args.start).timestamp()) if args.start else None
    )

    def _show(label: str, params: dict) -> Optional[int]:
        """Run one probe. Returns candle count, -1 for no_data, None for
        transport/API error."""
        url = f"{client.base_url}/market/udf/history?" + "&".join(
            f"{k}={v}" for k, v in params.items()
        )
        print(f"\n=== {label} ===")
        print(f"GET {url}")
        try:
            payload = client._get("/market/udf/history", params)
        except NobitexNoData as e:
            print(f"RESPONSE: no_data  ({e.context()})")
            return -1
        except NobitexError as e:
            print(f"RESPONSE: ERROR {e.context()}")
            return None
        raw = json.dumps(payload)
        print(f"RESPONSE: {raw[: args.raw_chars]}")
        t = payload.get("t") or []
        if t:
            from .downloader import data_ts_iso

            print(f"-> {len(t)} candles, first={data_ts_iso(int(t[0]))} "
                  f"last={data_ts_iso(int(t[-1]))}")
            return len(t)
        print(f"-> {payload.get('s')} (no candle arrays)")
        return 0

    # P1: the requested range, first page
    p1: Optional[int] = None
    if start_ts is not None:
        p1 = _show("P1: requested range (page 1)",
                   {"symbol": sym, "resolution": resolution,
                    "from": start_ts, "to": end_ts, "page": 1})
    # P2: narrow 10-candle window ending at `end` (same era)
    p2 = _show("P2: narrow 10-candle window ending at end (same era)",
               {"symbol": sym, "resolution": resolution,
                "from": end_ts - 10 * interval, "to": end_ts, "page": 1})
    # P3: most recent 10 candles (does this timeframe exist AT ALL recently?)
    now_ts = int(datetime.now(timezone.utc).timestamp())
    p3 = _show("P3: most recent 10 candles (last ~2h of data)",
               {"symbol": sym, "resolution": resolution,
                "from": now_ts - 10 * interval, "to": now_ts, "page": 1})
    # P4: documented countback mode (candles before `to`, priority over from)
    p4 = _show("P4: countback=10 ending at end (documented countback mode)",
               {"symbol": sym, "resolution": resolution,
                "to": end_ts, "countback": 10})
    client.close()

    print("\n=== diagnosis ===")
    if p3 is None:
        print("A probe hit a transport/API error — fix connectivity/API "
              "first, then re-run this probe.")
    elif p3 in (0, -1):
        print("P3 (recent data) is EMPTY too -> the symbol/timeframe itself "
              "returns no data at all: check the symbol with 'markets' or "
              "the resolution (an invalid resolution answers s=error, not "
              "no_data).")
    elif p1 is not None and p1 > 0:
        print(f"Requested range DOES return data ({p1} candles on page 1) "
              f"-> re-run the download; if it still reports zero, inspect "
              f"the task error lines for the exact empty responses.")
    else:
        # recent data exists, but the requested era does not (or the wide
        # request is empty)
        if p2 is not None and p2 > 0:
            print("The NARROW window at the end of the requested era has "
                  "data while the wide request is empty -> the API limits "
                  "per-request RANGE WIDTH; the adapter's downloader now "
                  "retries wide windows narrower (adaptive narrowing), so "
                  "re-run the download.")
        else:
            print("HISTORY-DEPTH GAP: recent data exists but the requested "
                  "era does NOT. Nobitex has no candles for this "
                  "pair/timeframe that far back (minute-level 5m/15m "
                  "history is the usual suspect; documented floor is "
                  "~2022-03-20). Choose a later --start or a coarser "
                  "timeframe.")
            if p4 is not None and p4 > 0:
                print(f"P4 countback returned {p4} candle(s) ending near "
                      f"`end` -> data exists close to `end` but not at "
                      f"`start` (a mid-range depth gap, not a range limit).")
    print("\n(probe uses the PUBLIC endpoint only; no keys, no private data)")
    # non-zero only when an executed probe hit a transport/API error (not
    # no_data, which is a valid, informative answer)
    executed = [p2, p3, p4] + ([p1] if start_ts is not None else [])
    return 1 if any(r is None for r in executed) else 0


# ---------------------------------------------------------------- probe
def cmd_probe(args) -> int:
    """Generic quality probe: fetch a range via the selected exchange and
    report what REALLY came back (rows, first/last, duplicates, gaps,
    spacing statistics, quality verdict per timeframe).

    PUBLIC endpoints only — no auth, no private data. Unlike `download`,
    nothing is written: this is the read-only way to prove an exchange
    has enough valid history for a strategy BEFORE downloading.
    """
    from .providers import get_provider
    from .timeframes import TimeframeError, normalize_timeframes, parse_timeframe

    _setup_logging(args.verbose)
    try:
        tfs = normalize_timeframes(
            args.timeframes, param_name="--timeframes", exchange=args.exchange
        )
    except TimeframeError as exc:
        print(f"error: {exc}")
        return 2
    try:
        start_ts = int(_parse_dt(args.start).timestamp())
        end_ts = int(_parse_dt(args.end, default_now=True).timestamp())
    except SystemExit as exc:
        print(f"error: {exc}")
        return 2
    if end_ts <= start_ts:
        print("error: --end must be after --start")
        return 2

    provider = get_provider(args.exchange)
    results: list[dict] = []
    try:
        for tf in tfs:
            results.append(_probe_one(provider, args.pair, tf, start_ts, end_ts))
    finally:
        provider.close()

    if args.json:
        print(json.dumps(
            {"provider": args.exchange, "pair": args.pair,
             "start": start_ts, "end": end_ts, "timeframes": results},
            indent=1,
        ))
    else:
        for r in results:
            print()
            print(f"provider   : {r['provider']}")
            print(f"pair       : {r['pair']}")
            print(f"timeframe  : {r['timeframe']} (expected interval {r['expected_interval_s']}s)")
            print(f"range      : {r['range_start']} .. {r['range_end']}")
            print(f"requests   : {r['request_count']}")
            print(f"rows       : {r['returned_rows']}")
            print(f"first      : {r['first_candle']}")
            print(f"last       : {r['last_candle']}")
            print(f"duplicates : {r['duplicates']}")
            print(f"gaps       : {r['gaps']} (missing-interval events)")
            print(f"avg gap    : {r['avg_gap_s']}s{min_max_gap(r)}")
            print(f"quality    : {r['quality']}")
            print(f"validation : {r['validation']}")
            for p in r["problems"][:6]:
                print(f"  problem: {p}")
            if r["error"]:
                print(f"  error: {r['error']}")
        print()
        bad = [r["timeframe"] for r in results if r["quality"] in ("EMPTY", "ERROR")]
        if bad:
            print(f"verdict: NO USABLE DATA for {', '.join(bad)} "
                  f"(see quality lines above)")
        else:
            print("verdict: every timeframe returned rows (check gaps/quality "
                  "before trusting a backtest on this range)")
    return 1 if any(r["quality"] in ("EMPTY", "ERROR") for r in results) else 0


def min_max_gap(r: dict) -> str:
    if r["min_gap_s"] is None:
        return ""
    return f"   min {r['min_gap_s']}s / max {r['max_gap_s']}s"


def _probe_one(provider, pair: str, tf: str, start_ts: int, end_ts: int) -> dict:
    from .downloader import data_ts_iso
    from .timeframes import parse_timeframe
    from .validator import validate

    import pandas as pd

    interval = parse_timeframe(tf).seconds
    base = {
        "provider": provider.name, "pair": pair, "timeframe": tf,
        "expected_interval_s": interval,
        "range_start": data_ts_iso(start_ts), "range_end": data_ts_iso(end_ts),
        "request_count": 0, "returned_rows": 0,
        "first_candle": "-", "last_candle": "-",
        "duplicates": 0, "gaps": 0,
        "avg_gap_s": None, "min_gap_s": None, "max_gap_s": None,
        "quality": "ERROR", "validation": "-", "problems": [], "error": None,
    }
    before = provider.request_count
    try:
        candles = provider.fetch_window(pair, tf, start_ts, end_ts)
    except Exception as exc:  # noqa: BLE001 - probe reports, never tracebacks
        base["request_count"] = provider.request_count - before
        base["error"] = f"{exc.__class__.__name__}: {exc}"
        base["problems"] = [base["error"]]
        return base
    base["request_count"] = provider.request_count - before
    base["returned_rows"] = len(candles)
    if not candles:
        base["quality"] = "EMPTY"
        base["problems"] = ["exchange returned zero rows for the requested range"]
        return base
    arrival = [int(c.ts) for c in candles]
    base["first_candle"] = data_ts_iso(arrival[0])
    base["last_candle"] = data_ts_iso(arrival[-1])
    base["duplicates"] = len(arrival) - len(set(arrival))
    # gap statistics over the de-duplicated, sorted series
    uniq = sorted(set(arrival))
    diffs = [b - a for a, b in zip(uniq, uniq[1:])]
    if diffs:
        base["avg_gap_s"] = round(sum(diffs) / len(diffs), 1)
        base["min_gap_s"] = min(diffs)
        base["max_gap_s"] = max(diffs)
        base["gaps"] = sum(1 for d in diffs if d > interval)
    df = pd.DataFrame(
        [(c.ts, c.open, c.high, c.low, c.close, c.volume) for c in candles],
        columns=["date", "open", "high", "low", "close", "volume"],
    )
    _, rep = validate(
        df, pair, tf, expected_start_ts=start_ts, expected_end_ts=end_ts,
        end_is_open=True,
    )
    base["validation"] = rep.status
    base["quality"] = rep.quality
    base["problems"] = list(rep.problems)
    return base


# ---------------------------------------------------------------- depth
def cmd_depth(args) -> int:
    """Historical-depth discovery: earliest/latest available candle per
    timeframe (verified by real probes) plus the COMMON range all
    timeframes share — the range a multi-timeframe strategy can use.

    PUBLIC endpoints only. Nothing is downloaded.
    """
    from .providers import get_provider
    from .timeframes import TimeframeError, normalize_timeframes

    _setup_logging(args.verbose)
    try:
        tfs = normalize_timeframes(
            args.timeframes, param_name="--timeframes", exchange=args.exchange
        )
    except TimeframeError as exc:
        print(f"error: {exc}")
        return 2

    provider = get_provider(args.exchange)
    try:
        infos = [provider.discover_depth(args.pair, tf) for tf in tfs]
    finally:
        provider.close()

    from .downloader import data_ts_iso

    rows = []
    for info in infos:
        rows.append({
            "timeframe": info.timeframe,
            "earliest": data_ts_iso(info.earliest_ts) if info.earliest_ts else "-",
            "latest": data_ts_iso(info.latest_ts) if info.latest_ts else "-",
            "requests": info.requests_made,
            "notes": info.notes,
        })
    earliest_all = [i.earliest_ts for i in infos if i.earliest_ts]
    latest_all = [i.latest_ts for i in infos if i.latest_ts]
    common_earliest = max(earliest_all) if earliest_all else None
    common_latest = min(latest_all) if latest_all else None

    if args.json:
        print(json.dumps(
            {"provider": args.exchange, "pair": args.pair, "depth": rows,
             "common_earliest_ts": common_earliest,
             "common_latest_ts": common_latest},
            indent=1,
        ))
    else:
        print(f"provider: {args.exchange}   pair: {args.pair}")
        print(f"{'TF':<6} {'EARLIEST':<28} {'LATEST':<28} {'REQ':>4}")
        for r in rows:
            print(f"{r['timeframe']:<6} {r['earliest']:<28} {r['latest']:<28} {r['requests']:>4}")
        for r, info in zip(rows, infos):
            for n in info.notes:
                print(f"  [{r['timeframe']}] {n}")
        print()
        if common_earliest:
            print(f"COMMON_EARLIEST = {data_ts_iso(common_earliest)} "
                  f"(= max over timeframes; download --start at/after this)")
        else:
            print("COMMON_EARLIEST = - (no timeframe returned any history)")
        if common_latest:
            print(f"COMMON_LATEST   = {data_ts_iso(common_latest)}")
        missing = [r["timeframe"] for r in rows if r["earliest"] == "-"]
        if missing:
            print(f"note: no history at all for {', '.join(missing)}")
    return 0


# ---------------------------------------------------------------- validate
def cmd_validate(args) -> int:
    from .downloader import Downloader
    from .timeframes import TimeframeError, normalize_timeframes

    _setup_logging(args.verbose)
    root = Path(args.repo) if args.repo else default_repo_root()
    paths = default_user_data_layout(root)
    pairs = _parse_pairs(args.pairs)
    try:
        tfs = normalize_timeframes(
            args.timeframes, param_name="--timeframes", exchange=args.exchange
        )
    except TimeframeError as exc:
        print(f"error: {exc}")
        return 2
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
            exchange=args.exchange,
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
    if res.get("run_id"):
        print(f"[run] {res['run_id']}")
    return 0


# ---------------------------------------------------------------- compare
def cmd_compare(args) -> int:
    """List registered backtest runs or compare a set of them."""
    _setup_logging(args.verbose)
    from .compare import compare_runs, list_runs

    root = Path(args.repo) if args.repo else default_repo_root()
    results_dir = default_user_data_layout(root)["results_dir"]

    if args.list:
        runs = list_runs(results_dir)
        if args.json:
            print(json.dumps(runs, indent=1, default=str))
            return 0
        if not runs:
            print("no registered runs "
                  f"(results dir: {results_dir}; run `backtest` to register one)")
            return 0
        print(f"{len(runs)} registered run(s):")
        for r in runs:
            spec = r.get("spec", {})
            print(f"  {r['run_id']}  [{r.get('status')}] "
                  f"{spec.get('strategy')} / {spec.get('exchange')} / "
                  f"{','.join(spec.get('pairs') or [])} / "
                  f"{','.join(spec.get('timeframes') or [])} / "
                  f"{spec.get('start')}..{spec.get('end')}")
        return 0

    run_ids = [r.strip() for r in (args.runs or "").split(",") if r.strip()]
    if not run_ids:
        print("error: give --runs id1,id2... or --list", file=sys.stderr)
        return 2
    try:
        cmp = compare_runs(results_dir, run_ids)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps(cmp, indent=1, default=str))
        return 0
    _print_comparison(cmp)
    return 0


def _print_comparison(cmp: dict) -> None:
    rows = cmp.get("runs", [])
    header = ("run_id", "status", "strategy", "exchange", "trades", "return%",
              "maxDD%", "win%", "PF", "sharpe", "BH%")
    print(" | ".join(header))
    for r in rows:
        def _n(v):
            return "-" if v is None else (f"{v:.2f}" if isinstance(v, float) else str(v))

        print(" | ".join([
            str(r.get("run_id")), str(r.get("status")), str(r.get("strategy")),
            str(r.get("exchange")), _n(r.get("trades")), _n(r.get("return_pct")),
            _n(r.get("max_drawdown_pct")), _n(r.get("win_rate_pct")),
            _n(r.get("profit_factor")), _n(r.get("sharpe")), _n(r.get("buy_hold_pct")),
        ]))
    warns = cmp.get("warnings", [])
    if warns:
        print("\nwarnings:")
        for w in warns:
            print(f"  [{w['severity']}] {w['code']}: {w['message']}")
    else:
        print("\nno incompatibility warnings: runs are directly comparable")


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

    if args.exchange == "azbit":
        from .azbit_mockserver import app as mock_app

        print(f"Mock AZBit public API  ->  http://{args.host}:{args.port}")
    elif args.exchange == "wallex":
        from .wallex_mockserver import app as mock_app

        print(f"Mock Wallex public API  ->  http://{args.host}:{args.port}")
    else:
        from .mockserver import app as mock_app

        print(f"Mock Nobitex public API  ->  http://{args.host}:{args.port}")
    uvicorn.run(mock_app, host=args.host, port=args.port, log_level="warning")
    return 0


# ---------------------------------------------------------------- main
def build_parser() -> argparse.ArgumentParser:
    # stdlib-only import: build_parser must work on a bare interpreter
    # (doctor/re-exec without third-party packages installed).
    from .exchanges import SUPPORTED_EXCHANGES

    EXCHANGE_CHOICES = sorted(SUPPORTED_EXCHANGES)
    p = argparse.ArgumentParser(prog="nobitex", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument(
        "--repo", default=None,
        help="Freqtrade repository root containing .venv + user_data "
             "(default: auto = the adapter checkout)",
    )
    p.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    p.add_argument(
        "--exchange", default="nobitex", choices=EXCHANGE_CHOICES,
        help="exchange provider for market data (default nobitex); "
             "a per-command --exchange overrides this",
    )
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="command", required=True)

    pm = sub.add_parser("markets", help="discover markets on the selected exchange")
    pm.add_argument("--quote", default="USDT", help="filter by quote asset (default USDT)")
    pm.add_argument("--json", action="store_true")
    pm.add_argument("--exchange", default=argparse.SUPPRESS,
                    choices=EXCHANGE_CHOICES)
    pm.set_defaults(func=cmd_markets)

    pdl = sub.add_parser("download", help="download historical OHLCV")
    pdl.add_argument("--pairs", required=True)
    pdl.add_argument("--timeframes", required=True, help="comma list, e.g. 5m,15m,1h,4h,1d")
    pdl.add_argument("--start", required=True)
    pdl.add_argument("--end", default="now")
    pdl.add_argument("--exchange", default=argparse.SUPPRESS,
                     choices=EXCHANGE_CHOICES)
    pdl.add_argument("--force", action="store_true", help="ignore resume manifest")
    pdl.add_argument("--keep-incomplete", action="store_true",
                     help="keep the still-open last candle")
    pdl.add_argument("--startup", default=None, help="tf:candles overrides, e.g. 5m:850,1d:260")
    pdl.set_defaults(func=cmd_download)

    pp = sub.add_parser(
        "ohlcv-probe",
        help="raw public OHLCV diagnostic: show exact request/response for a pair/timeframe/range",
    )
    pp.add_argument("--pair", required=True, help="e.g. BTC/USDT")
    pp.add_argument("--timeframe", required=True, help="single timeframe, e.g. 5m")
    pp.add_argument("--start", default=None, help="YYYY-MM-DD (enables the requested-range probe)")
    pp.add_argument("--end", default="now")
    pp.add_argument("--raw-chars", type=int, default=1200,
                    help="how much of each raw JSON response to print")
    pp.set_defaults(func=cmd_ohlcv_probe)

    pg = sub.add_parser(
        "probe",
        help="quality probe: fetch a range via the selected exchange and "
             "report rows/first/last/duplicates/gaps/quality (read-only)",
    )
    pg.add_argument("--pair", required=True, help="e.g. BTC/USDT")
    pg.add_argument("--timeframes", required=True,
                    help="comma list, e.g. \"5m,15m,1h,4h,1d\"")
    pg.add_argument("--start", required=True)
    pg.add_argument("--end", default="now")
    pg.add_argument("--json", action="store_true")
    pg.add_argument("--exchange", default=argparse.SUPPRESS,
                    choices=EXCHANGE_CHOICES)
    pg.set_defaults(func=cmd_probe)

    pdp = sub.add_parser(
        "depth",
        help="historical-depth discovery: earliest/latest candle per "
             "timeframe + the COMMON range all share (read-only)",
    )
    pdp.add_argument("--pair", required=True, help="e.g. BTC/USDT")
    pdp.add_argument("--timeframes", required=True,
                     help="comma list, e.g. \"5m,15m,1h,4h,1d\"")
    pdp.add_argument("--json", action="store_true")
    pdp.add_argument("--exchange", default=argparse.SUPPRESS,
                     choices=EXCHANGE_CHOICES)
    pdp.set_defaults(func=cmd_depth)

    pv = sub.add_parser("validate", help="validate stored data")
    pv.add_argument("--pairs", required=True)
    pv.add_argument("--timeframes", required=True)
    pv.add_argument("--start", required=True)
    pv.add_argument("--end", default="now")
    pv.add_argument("--exchange", default=argparse.SUPPRESS,
                    choices=EXCHANGE_CHOICES)
    pv.add_argument("--closed-range", action="store_true",
                    help="end is a closed period (last candle complete)")
    pv.set_defaults(func=cmd_validate)

    pb = sub.add_parser("backtest", help="run a Freqtrade backtest")
    pb.add_argument("--strategy", required=True)
    pb.add_argument("--pairs", required=True)
    pb.add_argument("--start", required=True)
    pb.add_argument("--end", default="now")
    pb.add_argument("--exchange", default=argparse.SUPPRESS,
                    choices=EXCHANGE_CHOICES)
    pb.add_argument("--stake-currency", default="USDT")
    pb.add_argument("--capital", type=float, default=10_000.0)
    pb.add_argument("--stake", default="unlimited")
    pb.add_argument("--max-open", type=int, default=8)
    pb.add_argument("--fee", default=str(DEFAULT_SPOT_FEE),
                    help=f"'{DEFAULT_SPOT_FEE}' or '' for exchange default")
    pb.add_argument("--skip-precheck", action="store_true")
    pb.add_argument("--out", default=None, help="write result descriptor JSON here")
    pb.set_defaults(func=cmd_backtest)

    pu = sub.add_parser("ui", help="start the web UI")
    pu.add_argument("--host", default="127.0.0.1")
    pu.add_argument("--port", type=int, default=8765)
    pu.set_defaults(func=cmd_ui)

    pmk = sub.add_parser("mock", help="start a mock exchange API (offline testing)")
    pmk.add_argument("--host", default="127.0.0.1")
    pmk.add_argument("--port", type=int, default=8900)
    pmk.add_argument("--exchange", default=argparse.SUPPRESS,
                     choices=EXCHANGE_CHOICES)
    pmk.set_defaults(func=cmd_mock)

    pd_ = sub.add_parser("doctor", help="print the runtime diagnostic for --repo")
    pd_.set_defaults(func=cmd_doctor)

    pc = sub.add_parser("compare", help="list registered backtest runs or compare a set")
    pc.add_argument("--runs", default=None, help="comma-separated run IDs to compare")
    pc.add_argument("--list", action="store_true", help="list registered runs")
    pc.add_argument("--json", action="store_true")
    pc.set_defaults(func=cmd_compare)

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


# ---------------------------------------------------------------------------
# Installed console-script entry points (pyproject [project.scripts]).
#
# Each `nobitex-<command>` script calls its `<command>_main()` with NO
# arguments, so these wrappers translate `sys.argv` into the equivalent
# `main([...])` call. Global options (--repo/--exchange/-v) may appear
# anywhere on the script command line; they are hoisted before the
# subcommand so argparse accepts them exactly as `python -m` does.
# (Earlier declarations pointed at cmd_* directly, which raised TypeError
# because cmd_* require an `args` namespace.)
# ---------------------------------------------------------------------------

_GLOBAL_VALUE_OPTS = ("--repo", "--exchange")
_GLOBAL_FLAG_OPTS = ("-v", "--verbose", "--version")


def _split_script_argv(argv: list[str]) -> tuple[list[str], list[str]]:
    """Split script argv into (global_opts, command_opts).

    Deterministic, platform-independent, no argparse involved: only known
    global spellings (`--opt value`, `--opt=value`, flags) move; `-h/--help`
    and everything else (including unknown `--flags` and anything after a
    bare `--`) stays in place so subcommand parsing/errors are unchanged.
    """
    glob: list[str] = []
    rest: list[str] = []
    i = 0
    n = len(argv)
    while i < n:
        tok = argv[i]
        if tok == "--":
            rest.extend(argv[i:])
            break
        moved = False
        for opt in _GLOBAL_VALUE_OPTS:
            if tok == opt:
                if i + 1 < n:
                    glob.extend([tok, argv[i + 1]])
                    i += 2
                else:
                    rest.append(tok)  # missing value: leave for argparse to reject
                    i += 1
                moved = True
                break
            if tok.startswith(opt + "="):
                glob.append(tok)
                i += 1
                moved = True
                break
        if moved:
            continue
        if tok in _GLOBAL_FLAG_OPTS:
            glob.append(tok)
            i += 1
            continue
        rest.append(tok)
        i += 1
    return glob, rest


def _script_main(command: str, argv: Optional[list[str]] = None) -> int:
    glob, rest = _split_script_argv(list(sys.argv[1:] if argv is None else argv))
    return main(glob + [command] + rest)


def markets_main(argv: Optional[list[str]] = None) -> int:
    return _script_main("markets", argv)


def download_main(argv: Optional[list[str]] = None) -> int:
    return _script_main("download", argv)


def validate_main(argv: Optional[list[str]] = None) -> int:
    return _script_main("validate", argv)


def backtest_main(argv: Optional[list[str]] = None) -> int:
    return _script_main("backtest", argv)


def ui_main(argv: Optional[list[str]] = None) -> int:
    return _script_main("ui", argv)


def mock_main(argv: Optional[list[str]] = None) -> int:
    return _script_main("mock", argv)


def probe_main(argv: Optional[list[str]] = None) -> int:
    return _script_main("probe", argv)


def depth_main(argv: Optional[list[str]] = None) -> int:
    return _script_main("depth", argv)


def doctor_main(argv: Optional[list[str]] = None) -> int:
    return _script_main("doctor", argv)


def compare_main(argv: Optional[list[str]] = None) -> int:
    return _script_main("compare", argv)


if __name__ == "__main__":
    raise SystemExit(main())
