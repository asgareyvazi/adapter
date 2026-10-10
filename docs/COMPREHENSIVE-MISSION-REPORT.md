# Comprehensive Engineering Mission — Final Report

Date: 2026-10-10. Branch: `arena/dc3ed543-adapter` (HEAD `da4c236` + uncommitted
work, left uncommitted per mission §13). No commits, pushes, branches or releases
were made in this mission.

## Verdict: ACCEPTED

Every check executable in this environment passes (469 passed / 0 failed).
Three checks are **blocked by the sandbox** (no external network: TLS EOF to
`api.wallex.ir`, same egress block as the earlier AZBit work) and are specified
below as exact remaining commands. One mapping (Wallex resolutions) is
**provisional** until those live checks run — it is marked as such in code,
docs and tests, and a wrong entry fails loudly (no silent substitution).

## Validation evidence (exact commands, exit codes, counts)

Environment: Linux, `/home/user/ftenv` CPython 3.11.2, freqtrade 2026.9,
ccxt 4.5.85, pandas 3.0.6. All commands run from the repo root.

| # | Command | Exit | Result |
| --- | --- | --- | --- |
| 1 | `/home/user/ftenv/bin/python -m pytest -q` | 0 | **469 passed, 14 deselected, 0 failed** in ~130 s (baseline at mission start: 345 passed / 9 deselected / 0 failed; +124 tests, +5 live) |
| 2 | `/home/user/ftenv/bin/python -m compileall -q nobitex_adapter tests` | 0 | `COMPILE_OK` |
| 3 | fresh `python3 -m venv` + `pip install --no-deps .`, then `nobitex-{markets,download,validate,backtest,ui,mock,probe,depth,doctor,compare} --help` | 0 | all 10 exit 0 |
| 4 | installed `nobitex-doctor --version` | 0 | `nobitex 0.1.0` |
| 5 | installed `nobitex-download` (no args) | 2 | argparse wiring proof |
| 6 | `pip install "/home/user/adapter[test]"` in a fresh venv | 0 | proves the new `test` extra + full dep closure (48 s) |
| 7 | CI CLI smokes (`--help` ×4, `compare --list` on empty repo) | 0 | `CLI_SMOKES_OK` |
| 8 | `sha256sum user_data/strategies/NostalgiaForInfinityX8.py` | 0 | `f202e860…21cd0` — byte-identical to `STRATEGY_SOURCE.txt` (X8 UNCHANGED) |
| 9 | bare `/usr/bin/python3 -m nobitex_adapter --help` (no third-party deps) | 0 | bare-interpreter path intact (regression caught & fixed, §5) |

Warnings in the suite run (13, all pre-existing/external, none from new code):
6× `Pandas4Warning` (X8's `copy` kwarg under pandas 3), 1×
`StarletteDeprecationWarning` (`httpx` vs `httpx2`), plus summary lines.
Zero `PytestUnknownMarkWarning` (the `unit` marker is now registered).

## Completed checks (per mission section)

**§3 baseline** — verified, not assumed: full suite 345/9/0, `compileall`,
`--help`/`doctor` exit 0, console-script `TypeError` reproduced pre-fix,
Wallex 0 hits pre-work, X8 sha prefix `f202e860ee5937a9` match.

**§4 implementation audit** — every touched module read in full (clients,
providers, downloader, validator, configgen, backtest, results, jobs, webui,
runtime, symbols, timeframes). Findings that drove the work: no provider
abstraction gaps (Wallex slots into `ExchangeProvider` cleanly); CLI
`--exchange` choices hardcoded ×8; `--version` missing; no run registry or
comparison; unversioned data sidecars; ×3 identical rate limiters; ×5
`0.002` fee literals; exchange-specific `ohlcv-probe` guard message.

**§5 runtime/packaging repair** — console scripts now target zero-arg
`*_main` wrappers with global-option hoisting (`--repo`/`--exchange` value
and `=` forms, `-v`, `--version`; `-h/--help`, unknowns and `--`-tails stay
put); new `nobitex-probe/-depth/-doctor` scripts; `unit` marker registered;
`runtime.is_usable_python()` (exit-0 + exact `ok`, never raises) gates the
`bare_python` fixture against Windows Store stubs; `--version` added.
`test_downloader.py:87-88` POSIX literal assessed HARMLESS (`as_posix()`
normalizes on Windows). 19 + 6 new tests; real installed-binary smoke (§V3–5).

**§6 Wallex first-class provider** — `wallex_client.py` (strictly per
https://api-docs.wallex.ir/: `/v1/markets` envelope, raw-UDF `/v1/udf/history`,
cursor pagination with stall/max-request guards, gentle undocumented-limit
defaults, `Retry-After`-honoring 429 path), `providers/wallex.py`
(`ExchangeProvider`), registry + centralized CLI `choices` + `mock --exchange
wallex` + `/api/exchanges` entry, `wallex_mockserver.py`, `TMN`-quote symbols.
59 new tests (unit + mock-HTTP integration + 5 live). Live validation BLOCKED
(sandbox, see below). Depth uses single wide windows (a full-walk first cut
cost 116 s vs 15 s in tests — fixed before merge).

**§7 data contract v1** — `datacontract.py`: deterministic fingerprint
(`sha256` over struct-packed canonical rows: sorted UTC-second ts + float64
OHLCV; layout/dtype-invariant, mutation-sensitive, known-answer vector),
`contract`/`provenance`/`fingerprint` stamped on every manifest + report,
legacy v0 sidecars resume unchanged and upgrade on save. 14 tests.

**§8 strategy audit + execution** — X8 sha256 FULL match, UNCHANGED
(`git status` clean for `user_data/`); 13 Freqtrade callback overrides;
50 `self.dp` occurrences; `startup_candle_count = 800`; TinyStrategy
plumbing-only (5m, sma(5), startup 10). Execution proven by
`test_full_pipeline_with_real_x8` (in-process real backtest, green) and
`test_cli_reexec_runs_in_selected_venv` (real CLI backtest in a venv, green).
CORRECTIONS to `docs/AA-ARCH-01-AUDIT.md` (left untouched as a prior-mission
artifact): that report said "15 callbacks / 25× self.dp" — recount gives
13 callback overrides (`grep -nE "def (populate|confirm|bot_|check_|adjust|
order_|leverage|custom|position_|stake_)"`) and 50 occurrences
(`grep -o "self\.dp" | wc -l`).

**§9 comparison** — `compare.py`: content-addressed run IDs, run registry
(`results/runs/*.run.json`, schema v1: spec, config hash, strategy hash,
data fingerprints, summary, runtime), `compare_runs` metric table +
13-code incompatibility-warning matrix (`RANGE/PAIRS/TIMEFRAMES/DATA_FP`
high; `EXCHANGE/CAPITAL/FEE/STAKE/CONFIG/STRATEGY_SRC` medium;
`FREQTRADE/ADAPTER` low; `FAILED_RUN`, `SINGLE_RUN`), CLI `compare`
(`--list`/`--runs`/`--json`, + `nobitex-compare` script), GUI
`/api/runs` + `/api/runs/compare`, registration hooked into `run_backtest`
(never fails a backtest; covers CLI+GUI since GUI backtests shell to the
CLI). 20 tests + e2e assertion that a real X8 backtest registers.

**§10 deterministic tests** — all new/repaired behavior covered; live tests
stay `-m live` (14 deselected by default: 9 pre-existing + 5 Wallex).

**§11 CI** — `.github/workflows/ci.yml` (new): install `.[test]` + compile +
console-script smoke + full non-live suite + CLI smokes, Linux/py3.11 only,
with an explicit no-Windows-claims header. Every step verified locally,
including a from-scratch `pip install .[test]`.

**§12 docs + evidence-gated cleanup** — README (§8 Wallex, renumbered §9–12,
compare/contract/fee/scripts/`--version`/live docs), new `docs/WALLEX.md`
(matrix + provisional ladder + live checklist). Cleanup verdicts:
RATE-LIMITER ×3 (byte-identical, proven by diff) → extracted to
`ratelimit.py` with compat aliases; FEE `0.002` ×5 → single
`configgen.DEFAULT_SPOT_FEE`; `freqtrade_env()` → KEPT (zero callers but a
documented intentional seam — removal not gated); `candles_range`,
`scripts/` → nothing on disk, nothing to do; per-client retry loops →
deliberately NOT deduped (failure envelopes differ per exchange).

## Blocked checks (sandbox, need a networked machine)

```bash
# 1) Wallex live probes: discovery, TMN quotes, history shape, pagination,
#    and verification of EVERY provisional WALLEX_RESOLUTIONS entry
python -m pytest tests/test_wallex_live_api.py -m live -q

# 2) real-data X8-range download (small range first)
python -m nobitex_adapter --exchange wallex download \
  --pairs BTC/USDT --timeframes "5m,15m,1h,4h,1d" \
  --start 2024-06-01 --end 2024-06-06

# 3) real-data depth (confirms the oldest-first-window assumption)
python -m nobitex_adapter --exchange wallex depth \
  --pair BTC/USDT --timeframes "5m,15m,1h,4h,1d"
```

Success criteria: (1) 5/5 pass — any `WallexAPIError` on a resolution means
that ladder entry must be corrected in `timeframes.py`; (2) exit 0 with
5 feathers under `user_data/data/wallex/`; (3) exit 0 with plausible
`COMMON_EARLIEST`. Record row counts + earliest timestamps before trusting
Wallex data for strategy decisions. (Pre-existing Nobitex/AZBit live suites
are unaffected and equally sandbox-blocked.)

## Change summary (uncommitted, per §13)

22 tracked files modified, 17 new files (`.github/workflows/ci.yml`,
`docs/WALLEX.md`, this report, `compare.py`, `datacontract.py`,
`exchanges.py`, `ratelimit.py`, `wallex_client.py`,
`providers/wallex.py`, `wallex_mockserver.py`, 6 test files +
`test_wallex_live_api.py`). `git diff --stat`: 807 insertions,
102 deletions (tracked files). X8 and `STRATEGY_SOURCE.txt` untouched.
No secrets added; no private/trading APIs touched; no features removed.
