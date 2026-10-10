# AA-ARCH-01 — Architecture Audit & Hybrid Engine Roadmap

- **HEAD audited:** `da4c2361bb4530d75db2004bb892506dc736b21e` (branch `arena/dc3ed543-adapter`)
- **Audit date (UTC):** 2026-10-10. **Auditor:** Arena agent (read-only w.r.t. product code; strategy files untouched, SHA-verified).
- **Mission scope:** audit + plan only. No rewrite, no deletions, no test weakening, no commit/push.
- **Conventions:** FACT = verified by inspection/execution below; HYPOTHESIS = plausible, unproven; RECOMMENDATION = advisory.

---

## 1. Executive verdict: PARTIALLY READY

| Requirement | Verdict |
|---|---|
| Exchange market-data ingestion + normalization (Nobitex, AZBit) | READY (tested, documented) |
| Backtesting existing Freqtrade strategies with minimal changes | READY via Freqtrade engine (X8 runs unmodified, proven by e2e) |
| Compare strategies / exchanges / pairs / backtest results | **BLOCKED** — only single-run dashboards + within-run buy&hold exist; no cross-run/strategy/exchange comparison module, CLI, or API |
| Clean separation: adapters / data / execution / analysis | PARTIAL — provider abstraction exists for ingestion; execution and analysis are Freqtrade-coupled by design |
| Hybrid path (Freqtrade now, independent engine later) | VIABLE — data layer is already engine-independent; execution swap is a large, well-bounded future phase, not justified now |
| Wallex as preferred future provider | **NOT PRESENT** — zero references in repo; all Wallex claims would be unverified |

**Bottom line:** keep Freqtrade as the execution engine; invest next in (a) Wallex historical-data provider, (b) a result-normalization + comparison layer. Do not start an independent engine — X8's Freqtrade surface (custom exits, position adjustment/DCA, confirm/timeout callbacks, `dp` usage, informative merging) makes it a multi-month semantic-clone project with no triggering requirement.

---

## 2. Verified current architecture and data flow

### 2.1 Project structure (FACT — `find`, HEAD `da4c236`)

```text
nobitex_adapter/
  __init__.py  __main__.py            # version 0.1.0; `python -m nobitex_adapter` -> cli.main
  cli.py                             # 10 subcommands (see 2.2)
  providers/{base,nobitex,azbit}.py   # ExchangeProvider contract + registry get_provider()
  nobitex_client.py  azbit_client.py  # exchange HTTP: retry/backoff/rate-limit/strict parsing
  symbols.py  timeframes.py           # symbol mapping; normalize_timeframes single boundary
  downloader.py                      # chunking, resume manifests, merge/dedupe, feather writes
  validator.py                       # PASS/FAIL + quality verdict (CONTIGUOUS..INVALID)
  configgen.py  backtest.py          # generated configs; precheck; Freqtrade runner
  freqtrade_bootstrap.py             # ccxt registration + in-process freqtrade main()
  ccxt_nobitex.py                    # ccxt subclass (public only; private raises NotSupported)
  results.py                         # backtest-zip -> dashboard JSON (+ per-pair buy&hold)
  jobs.py                            # GUI background jobs (threads; backtest via subprocess CLI)
  runtime.py                         # stdlib-only venv discovery/probe/re-exec/doctor
  mockserver.py  azbit_mockserver.py  # deterministic offline mock APIs (testing tools)
  webui/{app.py,static/}             # FastAPI + dependency-free HTML/JS (17 routes)
tests/ (27 files)  docs/{API_MATRIX,AZBIT}.md  user_data/strategies/{NostalgiaForInfinityX8.py}
pyproject.toml (no .github/workflows — NO CI CONFIG, FACT)
```

### 2.2 Entry points (FACT — `cli.py:675-775`, `__main__.py`, `pyproject.toml`)

- CLI: `markets download ohlcv-probe probe depth validate backtest ui mock doctor`, plus global `--repo/--exchange/-v`. Global `--exchange {nobitex,azbit}` with per-command override via `argparse.SUPPRESS`.
- GUI: `ui` command serves FastAPI (`/`, `/api/status|runtime|repo|strategies|exchanges|markets|data-history|jobs…|presets|results/list|results/latest|results/{name}`).
- ⚠️ `pyproject.toml [project.scripts]` registers `nobitex-markets = cli:cmd_markets` etc. — **CONFIRMED BROKEN**: `cmd_*` take an `args` namespace, so entry-point invocation raises `TypeError: cmd_markets() missing 1 required positional argument: 'args'` (reproduced by direct call). Documented usage (`python -m`) is unaffected.

### 2.3 Exchange adapters (FACT)

- Contract `providers/base.py::ExchangeProvider`: Freqtrade-facing pairs/TFs in/out; `to_exchange_symbol/timeframe`, `discover_markets`, `fetch_window` (WHOLE window, ascending), `discover_depth`, `zero_data_error`, `request_count`.
- `NobitexProvider` wraps `NobitexClient` (`/market/stats`, `/market/udf/history` ≤500/page walk; `from/to` unix seconds; `s: ok/no_data/error`).
- `AzbitProvider` wraps `AzbitClient` (`/api/ohlc` ~1000-row cursor walk `cursor=last_ts+1s`, boundary dedupe, stall/max-request hard errors; strict ISO parsing, naive=UTC documented assumption, sub-second truncated never rounded; discovery via `/api/currencies/pairs` w/ `/api/tickers` fallback).
- Timeframes: `normalize_timeframes` is the single boundary (never char-splits; exchange-aware support sets). Nobitex: 12 TFs; AZBit: `1m/5m/15m/30m/1h/4h/1d` (`3h/6h/12h/2d/3d` raise).
- Symbols: Nobitex longest-quote-suffix split; AZBit generic `BASE/QUOTE ⇄ BASE_QUOTE`.

### 2.4 Data flow, traced (FACT — `downloader.py`, `validator.py`, `backtest.py`, `results.py`, `jobs.py`)

```text
exchange public HTTP
  → provider.fetch_window(pair, tf, [start,end))     [pagination inside provider]
  → Downloader: 2000-candle chunks → adaptive window narrowing (500→10)
  → resume manifests  user_data/nobitex_gui/manifests/<ex>/<PAIR>-<tf>.json
  → merge + dedupe (counted) + sort → feather (tz-aware UTC, canonical cols)
      user_data/data/<exchange>/<BASE>_<QUOTE>-<tf>.feather   (atomic tmp+rename)
  → validator.validate → PASS/PASS_WITH_GAPS/FAIL + quality + JSON report
  → backtest precheck (strategy-derived TFs; startup-history warnings)
  → configgen (generated config; master config never touched; api_server absent)
  → Freqtrade backtesting (in-process after re-exec, or subprocess from GUI)
  → results zip → results.parse_backtest_zip → dashboard JSON
      (cards, equity/drawdown, per-pair, exit reasons, monthly, buy&hold)
  → CLI --out JSON / GUI /api/results/*
```

- Caching: manifest chunk coverage (`complete/empty`) + merge-with-existing feather (resume); market list cached to file for GUI (`NOBITEX_ADAPTER_MARKETS_CACHE`).
- Storage contract: feather, `date` tz-aware UTC + `open/high/low/close/volume` float64, strictly monotonic post-merge.
- Zero-data policy: zero rows = task `ERROR` + exact empty requests + probe hint (exit 1), both providers.

### 2.5 Runtime/Freqtrade integration (FACT — `runtime.py`, `test_runtime.py`, `test_e2e_runtime.py`)

- `find_venv_python` (POSIX `bin/python` + Windows `Scripts/python.exe`), JSON probe subprocess, env identity by **resolved freqtrade module path** (not executable), transparent re-exec for `backtest --repo`, `doctor` diagnostic, `runtime` block in every result.
- GUI host may run anywhere; backtest jobs spawn `sys.executable -m nobitex_adapter --repo … --exchange … backtest …` (jobs.py:284-300) which re-execs into the selected venv.
- POSIX↔Windows: discovery covers both layouts; Windows-layout finding is unit-tested on POSIX hosts (`test_find_venv_python_windows_layout`); no Windows execution observed in this sandbox (Linux-only evidence).

### 2.6 Tests & markers (FACT — `pyproject.toml`, `tests/`)

- Markers registered: `integration` (mock HTTP), `e2e` (real Freqtrade backtests), `live` (real API, deselected by default via `addopts`). 27 test files; fixtures: scripted `FakeSession`, `mock_server_url`, `azbit_mock_server_url`, `tmp_repo`, `TinyStrategy`.
- ⚠️ `@pytest.mark.unit` used in `test_timeframe_normalization.py` + `test_webui.py` but NOT registered → `PytestUnknownMarkWarning` (cosmetic; see §5).
- NO CI configuration (`.github/workflows` absent) — regression protection is manual `pytest` runs.

### 2.7 Documentation & declared guarantees (FACT)

- `README.md` (580 lines): install, runtime architecture, GUI/CLI quickstarts, X8 requirements, data-quality guarantees, AZBit §7, tests, API audit, architecture notes, troubleshooting.
- `docs/API_MATRIX.md` (Nobitex endpoints/limits/mapping), `docs/AZBIT.md` (267 lines: endpoints, mapping, timestamp semantics w/ proven-vs-assumed table, pagination algorithm, gap policy, commands).
- Declared guarantees: historical+backtest only; no keys; zero-data hard fail; no silent rounding; strategy files unmodified; `Local==Remote` discipline in prior reports.

### 2.8 Duplication / dead code / coupling (evidence-backed)

| # | Finding | Evidence |
|---|---|---|
| D1 | `_RateLimiter` + `_get` retry/backoff logic duplicated between `nobitex_client.py` and `azbit_client.py` | side-by-side inspection; same structure, different envelopes (429+`backOff` vs 429+`Retry-After`/`{Code,Message}`) |
| D2 | `freqtrade_bootstrap.freqtrade_env()` context manager has ZERO callers | `grep freqtrade_env` → only def site |
| D3 | `mockserver.py` vs `azbit_mockserver.py` duplicate RNG/seed/cache scaffolding | inspection; same `_seed_for/_ensure` pattern |
| D4 | Default fee `0.002` in 3 places: `ccxt_nobitex.DEFAULT_SPOT_FEE`, `configgen.build_backtest_config(fee=0.002)`, `cli --fee default "0.002"`, plus `jobs.py` `"0.002"` | grep; values agree today, no single source |
| D5 | GUI backtest job reconstructs CLI argv (`jobs.py:284-300`) — CLI flag renames break jobs without a shared contract test | inspection; covered only indirectly by e2e |
| D6 | `results.py` + `webui._data_history` depend on downloader's on-disk layout/manifest schema without a versioned contract | `results.py:_buy_hold_from_data`, `app.py:_data_history` read `user_data/data/<ex>/…` + manifests directly |
| D7 | `NobitexClient.candles_range` now used only by `ccxt_nobitex` + tests (downloader uses provider path) | grep; not dead, but two pagination spellings coexist |

---

## 3. Strategy compatibility matrix (evidence)

### 3.1 Inventory (FACT)

| Strategy | Location | Size | Provenance |
|---|---|---|---|
| NostalgiaForInfinityX8 | `user_data/strategies/NostalgiaForInfinityX8.py` | 59,827 lines / 2.9 MB | iterativv/NostalgiaForInfinity @ `7663b39`, SHA-256 `f202e86…1cd0` — **re-verified match** |
| TinyStrategy (test fixture) | `tests/conftest.py::SMALL_STRATEGY` | ~30 lines | in-repo test double, not a trading strategy |

No other strategies exist in the repo. X8 was NOT modified (audit rule + SHA match).

### 3.2 X8 Freqtrade surface (FACT — grep + targeted reads)

- Base: `IStrategy` (`freqtrade.strategy.interface`); `timeframe=5m`, `info_timeframes=[15m,1h,4h,1d]`, `btc_info_timeframes=[4h]`, `startup_candle_count: int = 800`, `stoploss=-0.99`, `trailing_stop=False`, `process_only_new_candles=True`, `use_exit_signal=True`, `exit_profit_only=False`, `ignore_roi_if_entry_signal=True`, `position_adjustment_enable=True`, `use_custom_stoploss=False`.
- Lifecycle/signal methods: `populate_indicators/entry/exit_trend`, `informative_pairs` (uses `self.dp.current_whitelist()`), `merge_informative_pair(..., ffill=False)` + `df.ffill()`.
- Execution callbacks: `custom_exit` (tag-dispatched, large), `adjust_trade_position` (DCA/grind variants v3/v4), `order_filled`, `confirm_trade_entry/exit`, `check_entry/exit_timeout`, `bot_loop_start`, `leverage`.
- Dependencies: `talib.abstract` (TA-Lib), numpy/pandas, `freqtrade.persistence {Trade, Order}`, `rapidjson`; 25× `self.dp.*`; 277 enter/exit-tag references; futures attrs present but `futures_max_open_trades_{long,short}=0`.
- NOT used: `qtpylib`, `pandas_ta`/`finta` (only a comment mention), custom `populate_any_indicators`, `check_entry_timeout` beyond standard signature.

### 3.3 Classification

| Strategy | Class | Rationale |
|---|---|---|
| TinyStrategy | **A — likely compatible with minimal changes** | Pure `IStrategy` + 3 populate methods + ROI/stoploss scalars; no `dp`, no callbacks, no TA-Lib (pandas rolling only). Portable to any engine honoring enter/exit columns. |
| NostalgiaForInfinityX8 | **B — compatible only with full Freqtrade engine capabilities** (current path ✅) / **C — significant adaptation for an independent engine** | Runs unmodified on Freqtrade (proven: `test_full_pipeline_with_real_x8`, `test_cli_reexec_runs_in_selected_venv`). An independent engine must reproduce: informative-pair merging + ffill timing, `process_only_new_candles` signal timing, custom-exit dispatch, DCA/grind position adjustment economics, confirm/timeout/order callbacks, `dp` services (whitelist, orderbook access), fee/slippage/fill model, ROI/stoploss/trailing interplay. Extracting populate_* alone does NOT reproduce behavior. |

- Class D: none — inventory is complete (2 files); no missing evidence within repo scope. (External strategies the user may add later are out of scope.)

### 3.4 Execution semantics that affect results (must-match list for any engine evaluation)

Candle timing (`process_only_new_candles`, informative ffill at `merge_informative_pair` call sites X8:4832-4847); entry/exit column conflict resolution; `custom_exit` string protocol; DCA stake/grind math in `adjust_trade_position` variants; `confirm_*` vetoes; order timeouts; `order_filled` state; fees (`tradingFee` 0.002 default), slippage/fill model (Freqtrade backtest defaults), stoploss/ROI/trailing precedence; position sizing (`stake_amount unlimited`, `max_open_trades`); `startup_candle_count` warmup truncation.

---

## 4. Target architecture (recommended)

```text
┌─ Exchange connectors (per-exchange, behind ExchangeProvider) ─┐
│ NobitexProvider · AzbitProvider · WallexProvider(future)       │ ingest only
└───────────────┬───────────────────────────────────────────────┘
                ▼ normalized candles (ts, O/H/L/C, volume; UTC seconds)
┌─ Market-data core (engine-independent) ───────────────────────┐
│ downloader (chunk/resume/merge) · validator (PASS/FAIL+quality)│
│ storage: user_data/data/<ex>/<PAIR>-<tf>.feather (versioned)   │
└───────────────┬───────────────────────────────────────────────┘
                ▼
┌─ Strategy compatibility layer ────────────────────────────────┐
│ detect_strategy_timeframes · strategy capability manifest      │
│ (declares engine features a strategy needs: dp, DCA, callbacks)│
└───────────────┬───────────────────────────────────────────────┘
                ▼
┌─ Execution (HYBRID: Freqtrade now) ───────────────────────────┐
│ configgen → freqtrade backtesting (selected .venv runtime)     │
│ independent engine: NOT BUILT until §9-Phase 6 trigger fires   │
└───────────────┬───────────────────────────────────────────────┘
                ▼
┌─ Result normalization + comparison (TO BUILD, §9-Phase 5) ────┐
│ normalized run record {params, data fingerprints, metrics}     │
│ compare across strategies × exchanges × pairs × timeranges     │
└───────────────┬───────────────────────────────────────────────┘
                ▼
CLI + GUI presentation · reproducibility bundle (config+data hashes+runtime block+logs)
```

Module disposition:

| Module | Disposition |
|---|---|
| `providers/*`, `nobitex_client`, `azbit_client`, `symbols`, `timeframes` | RETAIN; extend via new provider, not edits |
| `downloader`, `validator` | RETAIN; add storage/manifest schema version (§9-Phase 2) |
| `configgen`, `backtest`, `freqtrade_bootstrap`, `ccxt_nobitex`, `runtime` | RETAIN untouched (execution path, proven) |
| `results.py` | REFACTOR outward: keep zip→dashboard, add normalized run records + comparison (new module, e.g. `compare.py`) |
| `jobs.py`, `webui/*`, `cli.py` | RETAIN; add `compare` command + `/api/compare` + GUI view (additive) |
| `mockserver`, `azbit_mockserver` | RETAIN; add Wallex mock with Phase 3 |
| `freqtrade_env()` (D2), console scripts (2.2) | FIX OR REMOVE only under Phase 7 with tests |

---

## 5. Confirmed defects vs suspected issues

### Confirmed (reproduced or directly demonstrated)

| ID | Severity | Finding | Evidence |
|---|---|---|---|
| C1 | Low | pip console-script entry points broken (`TypeError`) | executed `cmd_markets()` → missing `args` |
| C2 | Low | Unregistered `pytest.mark.unit` → warnings noise | grep + warning output (27 warnings include these) |
| C3 | Medium (gap) | No strategy/exchange/pair/run comparison capability | `results.py` single-run only; no compare CLI/API/GUI (route list §2.2) |
| C4 | Medium (gap) | No CI — regressions rely on manual runs | `.github/workflows` absent |
| C5 | Low | `freqtrade_env()` dead code (D2) | zero callers (grep) |

### Suspected / needs user-side or live-API evidence

| ID | Finding | Missing evidence |
|---|---|---|
| S1 | Windows-specific runtime failures (interpreter selection, `Scripts/python.exe` execution) | No Windows host in sandbox; POSIX-side path logic tested, execution not |
| S2 | `ccxt.async_support`/`ccxt.pro` registration path for `azbit` backtests (upstream `ccxt.azbit` used; never exercised offline) | Real backtest with `--exchange azbit` on networked machine |
| S3 | AZBit `/api/currencies/pairs` exact shape; `volume24h` unit | Real API response capture |
| S4 | Whether `experimental.block_bad_exchanges=false` + `nobitex`/`azbit` names pass future Freqtrade validations | Freqtrade upgrade testing |

---

## 6. Test baseline (executed 2026-10-10, Linux, `/home/user/ftenv`: freqtrade 2026.9, ccxt 4.5.85, pandas 3.0.6)

| Command | Exit | Result |
|---|---|---|
| `python -m pytest tests/test_runtime.py -q` | 0 | **18 passed** |
| `python -m pytest tests/ -q` (full suite) | 0 | **345 passed, 9 deselected (live), 0 failed**, 119s, 27 warnings |
| `python -m compileall -q nobitex_adapter tests` | 0 | OK, no output |
| `python -m nobitex_adapter --help` | 0 | OK (10 subcommands listed) |
| `python -m nobitex_adapter doctor` | 0 | host ft 2026.9/ccxt 4.5.85; no repo selected (expected) |
| `python -m nobitex_adapter markets --help` | 0 | OK |

- `tests/test_e2e_runtime.py`: covered inside full suite (all pass; includes bare-launcher re-exec, doctor, no-venv error, GUI binding e2e). Not run standalone to avoid double ~minute backtests; zero failures in-suite.
- Warnings (material): `PytestUnknownMarkWarning` (`unit`), pandas `Pandas4Warning` from X8's `pd.concat(copy=False)` (strategy file — must not edit; upstream issue), deprecation noise from libs.
- Live tests (`-m live`, 9): NOT executed — no external network in sandbox (stated, not claimed).
- Runtime-failure investigation: POSIX discovery/probe/re-exec/doctor all green here; Windows-only paths (S1) cannot be reproduced on Linux — code handles both layouts, execution on Windows remains user-side evidence.

---

## 7. Wallex assessment

**FACT: no Wallex integration or prototype exists in the repository** (`grep -ri wallex` → zero hits across `.py/.md/.toml/.js/.html`).

Therefore every Wallex capability question is currently UNVERIFIED:

- Endpoints/response format, symbols, timeframe mapping, tz semantics, pagination/window limits, dup/missing/incomplete handling, rate limits/retries, multi-year reliability, normalized-contract conformance — **all unknown, none claimed**.

What the repo DOES provide for a future Wallex provider (verified):

1. `ExchangeProvider` contract + `get_provider` registry (add `WallexProvider`, extend `SUPPORTED_EXCHANGES`).
2. Exchange-aware `normalize_timeframes(exchange=...)` + per-exchange maps pattern.
3. `Downloader` accepts any provider (chunk/resume/merge/validation/feather reused; zero new downloader code expected).
4. CLI `--exchange` plumbing pattern (add `"wallex"` to `choices`), jobs/GUI `exchange` params, `/api/exchanges` list.
5. Mock-server pattern + pagination/gap/contract test templates (`test_azbit_*` as blueprint).
6. `ohlcv-probe`-style raw diagnostics pattern for de-risking undocumented behavior.

Acceptance bar for any future Wallex claim (no multi-year/complete-coverage claim without): scripted unit tests + mock integration + `probe`/`depth` captures from the real API + validator `quality` report + loader-compat test.

---

## 8. Proposed target architecture

See §4 diagram. Responsibilities/interfaces:

1. **Exchange connectors** — one provider per exchange behind `ExchangeProvider`; Freqtrade-facing I/O; no provider syntax leaks.
2. **Normalization/validation** — provider field mapping + shared `validator` (columns, monotonicity, OHLC/volume sanity, spacing, gaps, `quality`).
3. **Storage/retrieval** — `user_data/data/<ex>/<PAIR>-<tf>.feather` + manifests; ADD schema version + fingerprint (sha per file) in Phase 2.
4. **Strategy loading/compat** — source-derived TF/requirements (`detect_strategy_timeframes`); ADD capability manifest (needs: dp/DCA/callbacks/leverage) in Phase 2–4.
5. **Backtest execution** — Freqtrade in selected `.venv` (retain); config generated, never master; runtime block recorded.
6. **Result normalization** — ADD run record `{run_id, strategy+sha, exchange, pairs, timerange, config, data fingerprints, runtime, metrics, artifacts}` (Phase 5).
7. **Comparison** — ADD `compare` over run records (strategies × exchanges × pairs × timeranges), CLI + `/api/compare` + GUI (Phase 5).
8. **CLI/GUI** — thin presentation over shared services (already true; preserve).
9. **Reproducibility/logging/evidence** — run bundle (config, data+strategy hashes, runtime block, logs, dashboard); live probes captured as fixtures, never fabricated.

---

## 9. Phased roadmap (proposed, NOT executed)

- **Phase 0 — Baseline (DONE by this mission).** Acceptance: this report + green suite record (§6). No code changes.
- **Phase 1 — Runtime/test portability (no regression).** Fix C1 (console scripts: proper `main()` wrappers or remove), register `unit` marker (C2); add Windows-execution evidence plan (S1). Acceptance: full suite green, warnings reduced, Windows checklist documented. Depends: Phase 0.
- **Phase 2 — Data-contract audit + tests.** Version storage/manifest schema; add fingerprinting; contract tests per provider (field mapping, tz, ordering, empty/error semantics); strategy capability manifest. Acceptance: contract test matrix green for nobitex+azbit. Depends: Phase 0.
- **Phase 3 — Wallex historical-data integration.** Raw API survey (endpoints, limits, tz) → `WallexProvider` + mock + unit/integration/live tests → `probe`/`depth` real captures → validator quality bar. Acceptance: §7 bar met; no other provider touched. Depends: Phase 2.
- **Phase 4 — Freqtrade execution + reproducible backtests.** Multi-exchange backtest matrix (nobitex/azbit/wallex × X8/Tiny), run bundles, precheck hardening. Acceptance: ≥1 real backtest per exchange with recorded runtime + artifacts. Depends: Phase 3 (for Wallex runs; nobitex/azbit runnable now).
- **Phase 5 — Cross-comparison.** Normalized run records + `compare` CLI/API/GUI (strategies × exchanges × pairs × timeranges). Acceptance: side-by-side report from ≥2 runs reproducible from bundles. Depends: Phase 4.
- **Phase 6 — Independent-engine evaluation (OPTIONAL).** Trigger ONLY if a concrete requirement appears (e.g. Freqtrade cannot express needed semantics). Start with TinyStrategy-class engines + differential testing vs Freqtrade; X8-class support is explicitly out of initial scope. Acceptance: parity report on defined strategy class. Depends: Phase 4–5 evidence.
- **Phase 7 — Evidence-based cleanup.** Remove/replace advisory-list files (§10) one at a time with migration + tests. Acceptance: suite green after each removal; docs updated. Depends: Phases 1–5 (replacement validated first).

Order note: Phase 5 before 6 is deliberate — comparison works on normalized Freqtrade outputs and needs no new engine; evidence shows no better order.

---

## 10. Advisory removal/replace list (DO NOT DELETE — approval required each)

| File/symbol | Evidence | Safe migration |
|---|---|---|
| `freqtrade_bootstrap.freqtrade_env()` | zero callers (§2.8 D2) | delete + suite green; trivial, Phase 7 |
| `[project.scripts]` console entries | broken (C1) | either implement `main(argv)` wrappers + smoke test, or remove entries; Phase 1 |
| `NobitexClient.candles_range` | only ccxt+tests use it (D7) | keep while `ccxt_nobitex` needs it; replace with provider call if ccxt class is refactored; Phase 7 |
| duplicated `_RateLimiter`/retry (D1), mock scaffolding (D3) | inspection | extract shared `http.py`/mock helpers with zero behavior change + full suite; Phase 2/7 |
| scattered `0.002` defaults (D4) | grep | single `DEFAULT_FEE` constant; behavior-neutral; Phase 2 |

---

## 11. Risks, open questions, decisions needing approval

**Risks:** (R1) Independent-engine scope explosion — X8's callback/DCA/dp surface; contain via Phase 6 trigger rule. (R2) Undocumented exchange behavior (Wallex unknown; AZBit shapes partially assumed) — contain via probe-first rule + mocks. (R3) No CI — regressions caught late; mitigate with Phase 1 CI proposal (GitHub Actions: install + `pytest -m "not e2e"` + `compileall`). (R4) X8 must never be edited (mission rule + SHA pin) — any engine work must treat it as read-only fixture. (R5) Freqtrade/ccxt upgrades can break `ccxt_nobitex`/validations (S4) — pin + upgrade-test in Phase 4.

**Open questions:** (Q1) Wallex public OHLCV endpoint + history depth? (Q2) Which comparisons matter first (strategy-vs-strategy? exchange-vs-exchange same strategy)? (Q3) Windows execution environment for S1 evidence? (Q4) Is `block_bad_exchanges=false` acceptable long-term, or should exchange classes mature?

**Decisions requiring explicit approval:** (A1) Any deletion from §10. (A2) Starting Phase 6 (needs triggering requirement + parity criteria). (A3) Editing `NostalgiaForInfinityX8.py` (currently forbidden). (A4) Any private-API/key/trading work (out of scope). (A5) Commit/push/branch operations (not authorized in this mission).
