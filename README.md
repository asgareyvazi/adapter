# Nobitex Adapter — Market-Data + Backtest Manager (Freqtrade / NFI X8)

A production market-data integration layer that connects the **Nobitex public
API** to **Freqtrade** backtesting with **NostalgiaForInfinityX8 (X8)**, plus a
simple self-contained **graphical Backtest Manager** (FastAPI + HTML/JS).

**Milestone scope: HISTORICAL DATA + BACKTEST ONLY.** No live/private trading.
No API keys are read, stored, or sent anywhere. The architecture is
future-ready for private trading (see *Roadmap*), but the private API is
explicitly **out of scope** for this milestone.

```
Nobitex public REST API (apiv2.nobitex.ir)
        │  GET /market/stats  ·  GET /market/udf/history  (500 candles/page)
        ▼
nobitex_adapter (ccxt subclass + public client: retry, backoff, rate limit)
        ▼
Freqtrade data layout:  user_data/data/nobitex/BTC_USDT-5m.feather  (UTC)
        ▼
strict per-dataset validation (monotonicity, dups, OHLC, gaps, tz)  →  PASS/FAIL
        ▼
Freqtrade backtesting  ──  NostalgiaForInfinityX8  (base 5m, info 15m/1h/4h/1d, BTC 4h)
        ▼
results zip  →  dashboard (cards, equity curve, drawdown, per-pair, monthly,
               exit reasons, buy-&-hold comparison)  →  web UI + JSON
```

Both the **CLI** and the **GUI** call the *same* service functions
(`downloader`, `validator`, `configgen`, `backtest`, `results`) — nothing is
duplicated.

---

## Repository layout

| Path | What it is |
| --- | --- |
| `nobitex_adapter/` | The adapter package (clients, providers, downloader, validator, ccxt exchange class, config generator, backtest runner, results parser, job manager, mock API servers, **runtime binding**) |
| `nobitex_adapter/providers/` | Exchange abstraction: `ExchangeProvider` → `NobitexProvider` / `AzbitProvider` (registry: `get_provider`) |
| `nobitex_adapter/azbit_client.py` | AZBit public client (`/api/ohlc` cursor pagination, retry/backoff, strict parsing) + `azbit_mockserver.py` (offline test server) |
| `nobitex_adapter/runtime.py` | Stdlib-only runtime layer: venv discovery, probe, re-exec, `doctor` diagnostic |
| `nobitex_adapter/webui/` | FastAPI app + dependency-free static UI (dark, RTL/farsi + English) |
| `tests/` | unit, integration (against the in-repo mock APIs), e2e (real Freqtrade + X8), live (opt-in real-API) |
| `user_data/strategies/NostalgiaForInfinityX8.py` | The real X8 strategy, **unmodified** (provenance in `STRATEGY_SOURCE.txt`) |
| `user_data/nobitex_gui/` | Generated run configs, job state, logs, saved dashboards (git-ignored) |
| `user_data/data/{nobitex,azbit}/` | Downloaded feather data (git-ignored) |
| `docs/API_MATRIX.md` | **Nobitex public API capability matrix** (audit deliverable) |
| `docs/AZBIT.md` | **AZBit public API matrix + timestamp semantics + gap policy** |

---

## 1. Install

Python **3.11–3.12** recommended (tested with 3.11.11). Freqtrade is the main
heavy dependency; TA-Lib is required by Freqtrade.

### Linux / macOS (bash)

```bash
git clone <this-repo> adapter && cd adapter

python3 -m venv .venv
source .venv/bin/activate
pip install -U pip wheel
pip install -e .            # installs freqtrade, TA-Lib wrapper, fastapi, uvicorn, pyarrow
```

> If `TA-Lib` fails to build, install the TA-Lib C library first
> (`sudo apt install ta-lib` / `brew install ta-lib`) or use a wheel
> (`pip install TA-Lib==0.5.0 --global-option=build_ext ...`), then retry.

### Windows (PowerShell) — exact commands

```powershell
# 0) Prereqs (install once): Git for Windows, Python 3.12 from python.org
#    (tick "Add python.exe to PATH"), and the TA-Lib C library
#    (https://ta-lib.org/installation.html  ->  Windows 64-bit msi)

# 1) Clone + enter
git clone <this-repo> adapter
cd adapter

# 2) Virtual environment (repo-local, git-ignored)
python -m venv .venv
.venv\Scripts\Activate.ps1        # if blocked: Set-ExecutionPolicy -Scope CurrentUser RemoteFirst

# 3) Dependencies
python -m pip install -U pip wheel
pip install -e .

# 4) Sanity check
python -m nobitex_adapter --help
```

---

## 2. Runtime architecture — which Freqtrade runs the backtest

**The rule: a backtest always runs on the Freqtrade that belongs to the
selected Freqtrade repository — never on a "leftover" Freqtrade in whatever
happens to be the active Python.**

### Why this exists

The adapter can be launched by *any* Python (system Python, the adapter's
own venv, the GUI host). Before this repair, `--repo` only controlled file
paths, while Freqtrade was imported **in the adapter's own process** — so a
user whose Freqtrade repo `.venv` has `2026.9-dev` could silently get a
backtest on the system Python's `2026.8` (version mismatch, different
strategy semantics, different results).

### How it works (design decision, documented)

Four options were evaluated:

| Option | Verdict |
| --- | --- |
| A. "Run the adapter from the repo venv" (user must activate it) | Fragile: depends on the user remembering to activate the right venv; GUI host and backtest runtime still could diverge. |
| B. A separate launcher program | Duplicates entry points; two places to keep in sync. |
| **C. Transparent re-exec into the repo's venv (CHOSEN)** | The adapter stays a single entry point; the *process* that runs Freqtrade is provably the repo's venv; zero changes to Freqtrade core; zero duplicated trees. |
| D. Install the adapter inside the repo venv | Couples the adapter to one specific repo; breaks when the user has several repos. |

Mechanics (all in `nobitex_adapter/runtime.py`, stdlib-only):

1. **Discovery** — `find_venv_python(<repo>)` looks for `.venv/bin/python`
   (POSIX) or `.venv/Scripts/python.exe` (Windows) *inside the selected repo*
   only. No guessing, no walking upward.
2. **Probe** — a short subprocess runs the venv's Python and reports JSON:
   Python version, Freqtrade version + module path, CCXT version. This is
   the *selected* runtime, verified, not assumed.
3. **Bind** — for `backtest --repo X`, the CLI re-execs **itself** with that
   venv's Python *before any Freqtrade import* (`sys.argv` preserved,
   `PYTHONPATH` set to the adapter root). You see it in the log:
   `[runtime] re-executing with selected Freqtrade runtime: …`.
4. **Environment identity** — "already in the right runtime" is decided by
   comparing the **resolved Freqtrade module path**, not the executable path
   (a venv's `bin/python` is a *symlink to the base interpreter*, so path
   comparison is wrong). Same environment → no re-exec; different → re-exec.
5. **Proof, not assumption** — every backtest result (CLI `--out` JSON, GUI
   job details) carries a `runtime` block: `python`, `python_version`,
   `freqtrade`, `freqtrade_module`, `ccxt`, `adapter_version`. The GUI job
   log also contains the re-exec line, so you can audit which Freqtrade ran.

**`doctor`** prints the full diagnostic at any time:

```bash
# Linux/macOS
python -m nobitex_adapter --repo /path/to/freqtrade doctor
# Windows PowerShell (docs-only example — no Windows path is hardcoded in logic)
python -m nobitex_adapter --repo C:\Users\A-Eyvazi\Desktop\New\ folder\freqtrade doctor
```

Output: host runtime (GUI/CLI process) **and** selected runtime
(repo → `.venv` python → Freqtrade version + module path → CCXT).

**GUI**: the repo field at the top of the settings panel binds the whole
session; a runtime chip shows the *backtest* runtime (green = verified
venv found, yellow = fallback). Switching repos at runtime is a single
`POST /api/repo` — data/GUI keep running, backtests follow the new venv.

### Strategy discovery (deterministic, documented)

1. The **selected repo's** `user_data/strategies` — searched recursively, so
   nested strategy-repo layouts work out of the box, e.g.
   `user_data/strategies/NostalgiaForInfinity/NostalgiaForInfinityX8.py`
   (NFI cloned as a subfolder). Shallowest match wins; ties break
   lexicographically; `__pycache__`/hidden dirs are skipped.
   For nested layouts the adapter exposes a **symlink** named
   `NostalgiaForInfinityX8.py` in the flat strategies dir so Freqtrade can
   import it — it never copies or overwrites an existing real file, and it
   never touches anything under `NostalgiaForInfinity/configs/`.
2. The **adapter checkout's bundled** `user_data/strategies` (fallback,
   announced with a `bundled fallback` note in logs/UI).

### X8 requirements are derived, not hardcoded

`detect_strategy_timeframes()` parses the strategy source: `timeframe`
(5m), `info_timeframes` (15m/1h/4h/1d), `btc_info_timeframes` (4h) and
`startup_candle_count` (800). The pre-check uses the **strategy's own**
`startup_candle_count` to verify warmup history and warns on shortfalls.
A different strategy with different needs works without code changes.

---

## 3. Quickstart — the GUI (Backtest Manager)

```bash
# Linux/macOS
source .venv/bin/activate
python -m nobitex_adapter ui --host 0.0.0.0 --port 8765
# Windows PowerShell
.venv\Scripts\Activate.ps1
python -m nobitex_adapter ui --host 0.0.0.0 --port 8765
```

Open **http://localhost:8765** and follow the 5 steps:

0. **Freqtrade repo** (top of settings) — paste your Freqtrade clone path
   (the one with `.venv/` + `user_data/`). A runtime chip verifies the
   venv's Freqtrade; **backtests run on that venv**, while data + GUI keep
   running in the GUI host Python. Switch repos anytime — it rebinds
   instantly (`POST /api/repo`). If left blank, the GUI host's own Python is
   used (must have Freqtrade).
1. **Check Markets** — discovers USDT markets from `GET /market/stats`
   (deterministic `BASE/QUOTE` normalization; closed markets flagged).
2. **Pairs & Timeframes** — search/select (or "recommended" top-50 by 24h
   volume, stablecoin-wrapped pairs excluded). Timeframes are auto-derived
   from the selected strategy (X8 → 5m + 15m/1h/4h/1d + BTC informative).
3. **Date range & capital** — presets 1M…3Y or custom; capital, stake
   (currency + amount), max open trades, fee; optional JSON override.
4. **Download → Validate → Run Backtest** — background jobs with live
   progress, streaming logs and **cancel**.
5. **Results** — cards (total profit, return %, max DD, trades, win rate,
   profit factor, Sharpe, Sortino, CAGR), equity + drawdown charts,
   per-pair table, exit-reason table, monthly table, buy-&-hold comparison,
   and the download-history section.

Every backtest run writes its own config to
`user_data/nobitex_gui/configs/nobitex-<strategy>-<stamp>-<rand>.json` —
**your master config is never touched**, and the Freqtrade REST `api_server`
is never enabled in generated configs.

### Offline demo mode (mock API)

The repo ships a faithful mock of the documented Nobitex public API
(deterministic synthetic candles, 500/page, `no_data`, 429 + `backOff`):

```bash
# terminal 1
python -m nobitex_adapter mock --host 127.0.0.1 --port 8900
# terminal 2  (point the client at the mock)
NOBITEX_API_BASE=http://127.0.0.1:8900 python -m nobitex_adapter ui --port 8765   # Linux/macOS
$env:NOBITEX_API_BASE="http://127.0.0.1:8900"; python -m nobitex_adapter ui --port 8765   # PowerShell
```

> `NOBITEX_API_BASE` is the single seam used by tests and the mock; by
> default the client targets the real `https://apiv2.nobitex.ir`.

---

## 4. Quickstart — the CLI

The same services, scriptable:

```bash
# discover USDT markets (deterministic BASE/QUOTE symbols)
python -m nobitex_adapter markets --quote USDT

# download X8 data for 3 pairs, Q1-2024 (auto startup lead-in, resume-safe)
python -m nobitex_adapter download \
    --pairs BTC/USDT,ETH/USDT,SOL/USDT \
    --timeframes 5m,15m,1h,4h,1d \
    --start 2024-01-01 --end 2024-03-31

# strict validation report per dataset (PASS/FAIL, gaps, dups, OHLC, tz)
python -m nobitex_adapter validate \
    --pairs BTC/USDT,ETH/USDT,SOL/USDT \
    --timeframes 5m,15m,1h,4h,1d \
    --start 2024-01-01 --end 2024-03-31

# backtest with X8 (spot, dry-run, 10k USDT, fee 0.2%)
# --repo BINDS the backtest to that Freqtrade clone's .venv (see §2):
# the process that runs Freqtrade is provably the repo's venv.
python -m nobitex_adapter --repo /path/to/freqtrade backtest \
    --strategy NostalgiaForInfinityX8 \
    --pairs BTC/USDT,ETH/USDT,SOL/USDT \
    --start 2024-01-01 --end 2024-03-31 \
    --capital 10000 --stake unlimited --max-open 8 --fee 0.002 \
    --out results.json      # machine-readable descriptor incl. a `runtime` block

# what runtime would this command use? (host + selected, versions + paths)
python -m nobitex_adapter --repo /path/to/freqtrade doctor

# raw public OHLCV diagnostic: print the EXACT request URL + raw JSON for a
# pair/timeframe/range, then auto-probe (requested range, narrow window,
# recent range, countback) and diagnose WHY a range can come back empty.
# Public endpoint only — no keys, no private data.
python -m nobitex_adapter ohlcv-probe \
    --pair BTC/USDT --timeframe 5m --start 2024-06-01 --end 2024-06-06

# Windows PowerShell equivalents — same commands, backslash→newline via ``.
# NOTE: QUOTE the --timeframes list in PowerShell (see below).
python -m nobitex_adapter --repo C:\Users\A-Eyvazi\Desktop\New\ folder\freqtrade backtest `
    --strategy NostalgiaForInfinityX8 `
    --pairs BTC/USDT,ETH/USDT,SOL/USDT `
    --start 2024-01-01 --end 2024-03-31
```

Useful flags: `--repo` (bind backtest to a Freqtrade clone's `.venv`; global
option, before the subcommand), `doctor` (runtime diagnostic), `ohlcv-probe`
(raw public OHLCV request/response diagnostic); `download --force` (ignore
resume manifest), `--startup 5m:850,1d:260` (per-tf lead-in overrides),
`--keep-incomplete`; `backtest --out results.json` (machine-readable result
descriptor incl. `runtime`), `--skip-precheck`.

### `--timeframes` is normalized at one boundary (shell-safe)

`--timeframes` (download/validate) and the GUI timeframes both go through a
single canonical normalizer (`timeframes.normalize_timeframes`) that:

* accepts `"5m"`, `"5m,15m,1h"`, `"5m 15m 1h"`, `["5m","15m"]`, tuples, sets,
  and nested forms — all to a canonical deduplicated list;
* **never** iterates a string character-by-character, so `1d` can only ever
  be `["1d"]` (it can *never* become `["1","d"]`);
* trims whitespace, splits on comma/semicolon/space;
* rejects invalid or Nobitex-unsupported timeframes with a clear error that
  shows the raw received value (no traceback).

> **PowerShell note (real incident):** in PowerShell an unquoted comma list
> like `--timeframes 5m,15m,1h,4h,1d` can be mangled by the shell's
> comma-array handling before it reaches Python. Always **quote** the list:
> `--timeframes "5m,15m,1h,4h,1d"`. The normalizer also accepts the
> space-joined form a shell may produce, and if a broken token still reaches
> it, you get an actionable `invalid timeframe '…' in --timeframes=[…]`
> error instead of a mid-download crash.

---

## 5. X8 data requirements (auto-handled)

`NostalgiaForInfinityX8` (pinned copy in `user_data/strategies/`, see
`STRATEGY_SOURCE.txt` for commit + SHA-256) requires:

| What | Value |
| --- | --- |
| Base timeframe | **5m** |
| Informative timeframes (per pair) | **15m, 1h, 4h, 1d** |
| BTC informative pair | **BTC/USDT @ 4h** (auto-added to the pairlist) |
| `startup_candle_count` | 800 (5m) |
| Spot long-only | yes (futures path is future-proofed, not used) |

The pre-backtest check resolves *every* required `(pair, timeframe)` file and
fails fast with a precise download command if anything is missing. The
downloader prepends generous per-timeframe warmup (e.g. 850×5m, 260×1d) so
informative indicators are fully warmed before the timerange start.

**Data availability:** Nobitex minute candles exist only from ≈ **2022-03-20**
(documented). Backtest starts earlier than that are clamped with a warning —
3.5-year backtests therefore start on/after 2022-04.

---

## 6. Data quality guarantees

Every downloaded dataset is validated **strictly** and a JSON + human report
is written per pair/timeframe:

* monotonic timestamps (and repair-on-demand), duplicate detection/removal
* OHLC relations (`high ≥ max(open,close)`, `low ≤ min(open,close)`, `high ≥ low`, positive prices, finite values)
* volume sanity (≥ 0, finite)
* candle spacing == timeframe; **gap/missing-candle detection** with ranges
* timezone/epoch sanity (unix-seconds era check); stored as tz-aware UTC
* expected-window coverage (late start / early end flagged, open last candle tolerated)
* **PASS/FAIL status per dataset**; the download job reports `all_valid`

Storage: Freqtrade feather layout (`date` tz-aware UTC, `open/high/low/close/volume`),
`user_data/data/nobitex/{BASE}_{QUOTE}-{tf}.feather`, written atomically
(tmp + rename). Downloads are **chunked + manifest-tracked** so re-running a
range skips covered chunks (resume) and merges deduplicated, chronologically
ordered data.

Downloader robustness: per-endpoint rate limiting (≤ documented caps),
exponential backoff on network errors/5xx, **honors `429 {backOff}`**
(documented rate-limit response), 500-candle page pagination, malformed
candle rejection with full request context, cancellation at chunk boundaries.

**Zero data is a hard failure, never a silent success.** If a
pair/timeframe ends with zero candles over the requested range, the task is
`ERROR` (exit code 1) and the output carries the exact empty responses seen
(`no_data symbol=… res=… from=… to=… page=…`) plus the `ohlcv-probe` command
to inspect the raw API answers. A download can no longer report "success"
while a timeframe quietly has no data.

**Adaptive window narrowing.** Each chunk is fetched in ≤500-candle windows.
If a *wide* window comes back empty, the downloader retries it narrower
(500 → 250 → … → 10 candles) before declaring it empty. This makes the
downloader immune to an undocumented per-request **range-width limit** (an
API that answers `no_data` for wide ranges but serves narrow ones) while
adding **zero** extra requests when data is present. A genuinely empty
region is not crawled candle-by-candle (the cursor jumps to the full window
end once a 10-candle probe is empty).

### Nobitex resolution mapping & minute-data depth

Freqtrade timeframes map to Nobitex `/market/udf/history` `resolution` values
exactly as documented: `1m→1`, `5m→5`, `15m→15`, `30m→30`, `1h→60`, `3h→180`,
`4h→240`, `6h→360`, `12h→720`, `1d→D`, `2d→2D`, `3d→3D`. `from`/`to` are unix
seconds; an invalid resolution answers `{"s":"error","errmsg":"Invalid
resolution!"}` (the client surfaces that as an error, not as zero data).

> **Documented minute-data depth:** Nobitex states minute-level candles are
> available from the start of 1401 (≈ **2022-03-20**); hourly/daily go back
> further. In practice the *effective* depth of a **5m/15m series for a
> specific pair can be shorter than the documented floor** (a pair's minute
> history may only start when that pair's minute data was first recorded).
> If a 5m/15m range returns zero while 1h/4h/1d of the same range have
> data, that is the likely cause — run `ohlcv-probe --timeframe 5m` to see
> the raw responses (it probes the requested era, a narrow same-era window,
> the most-recent candles, and `countback`, then prints a diagnosis:
> history-depth gap vs range limit vs symbol). Choose a later `--start` or a
> coarser timeframe in that case. This is an API data-availability limit,
> handled explicitly (hard fail + diagnosis), not papered over with zeros.

---

## 7. AZBit provider (public historical OHLCV)

The adapter serves a second exchange through the same pipeline — same
downloader, validator, CLI/GUI and Freqtrade format. Full matrix:
**[docs/AZBIT.md](docs/AZBIT.md)**.

| Topic | AZBit |
| --- | --- |
| Public API | `https://data.azbit.com` — `GET /api/ohlc` (docs: https://data.azbit.com/docs/, https://docs.azbit.com/docs/spot/tickers/) |
| Auth | none (public endpoints only; no keys, no private API) |
| Symbols | generic `BASE/QUOTE` ⇄ `BASE_QUOTE` (`BTC/USDT` ⇄ `BTC_USDT`) |
| Timeframes | `5m→minutes5`, `15m→minutes15`, `1h→hour`, `4h→hour4`, `1d→day` (+`1m→minute`, `30m→minutes30`); no `3h/6h/12h/2d/3d` |
| Pagination | ~1000 rows/response (observed) → cursor walk (`cursor = last_ts + 1s`), boundary dedupe, stall/max-request guards |
| Zero data | hard `ERROR` + exact empty requests + probe command (same policy as Nobitex) |
| Gaps | reported (`quality` = `CONTIGUOUS`/`GAPPED`/`DUPLICATE`/`OUT_OF_ORDER`/`EMPTY`/`INVALID`); never filled, never hidden |

> **Do not assume AZBit `minutes5` means perfectly continuous 300-second
> Freqtrade candles. The adapter validates actual timestamps and reports
> gaps.**

Real AZBit rows are floating (`00:00:59`, `00:06:37`, …) with real gaps —
the adapter stores them verbatim (no `floor(ts/300)*300`, ever) and the
validator flags irregular spacing, so a gapped dataset can never look
clean. OHLCV availability ≠ strategy compatibility: `probe` proves rows
exist, `depth` finds the `COMMON_EARLIEST` all X8 timeframes share,
`download` prepends warmup, `validate` + the backtest precheck refuse
partial data.

```bash
# market discovery
python -m nobitex_adapter --exchange azbit markets --quote USDT

# read-only quality proof BEFORE downloading
python -m nobitex_adapter --exchange azbit probe \
  --pair BTC/USDT --timeframes "5m,15m,1h,4h,1d" \
  --start 2024-06-01 --end 2024-06-06

# historical depth + COMMON range for X8
python -m nobitex_adapter --exchange azbit depth \
  --pair BTC/USDT --timeframes "5m,15m,1h,4h,1d"

# download + validate (writes user_data/data/azbit/…feather)
python -m nobitex_adapter --exchange azbit download \
  --pairs BTC/USDT --timeframes "5m,15m,1h,4h,1d" \
  --start 2024-06-01 --end 2024-06-06
python -m nobitex_adapter --exchange azbit validate \
  --pairs BTC/USDT --timeframes "5m,15m,1h,4h,1d" \
  --start 2024-06-01 --end 2024-06-06
```

PowerShell: same commands with `` ` `` continuations and quoted
`--timeframes` (see §4). Per-command `--exchange` also works
(`download --exchange azbit …`).

---

## 8. Tests

```bash
# everything (unit + integration vs in-repo mock + full X8 e2e, ~90 s)
python -m pytest tests/ -q

# fast loop (no backtests)
python -m pytest tests/ -m "not e2e" -q

# individual layers
python -m pytest tests/ -m "not integration and not e2e and not live" -q   # unit
python -m pytest tests/ -m "integration" -q                                 # mock HTTP
python -m pytest tests/ -m "e2e" -q                                         # real Freqtrade + X8
```

Coverage highlights: symbol normalization, timeframe mapping, OHLCV parsing
(malformed/`no_data`/mismatch), retry + backoff + `429 backOff`, pagination
walk, dedupe/merge/order, incomplete-last-candle drop, gap/missing/OHLC
validation, config generation (no `api_server`, entry/exit pricing,
`block_bad_exchanges`), results-zip parsing, mock API contract, GUI job
lifecycle (discover → download → validate → backtest-subprocess → results,
incl. cancel), and a **full-pipeline e2e** running the unmodified X8
strategy on a small controlled range end to end.

**Timeframe-normalization + zero-data tests**
(`test_timeframe_normalization.py`, `test_cli_timeframes.py`,
`test_downloader.py`, `test_freqtrade_compat.py`): the canonical boundary
(`"5m"` / `"5m,15m,1h"` / `"5m 15m 1h"` / lists / tuples / sets; `1d` can
never become `["1","d"]`; invalid tokens rejected with the raw value shown),
the real incident command end to end (all five X8 timeframes download;
PowerShell space-join accepted; char-split input → clean exit 2, no
traceback), zero-data = hard `ERROR` with the exact empty requests + probe
hint, adaptive window narrowing against a wide-range-refusing API, bounded
request count on truly-empty regions, and a **Freqtrade-compatibility
proof** (downloaded feather loaded through Freqtrade's own data handler:
tz-aware UTC, canonical columns, strictly monotonic, no duplicates).

**AZBit tests** (`test_azbit_client.py`, `test_azbit_pagination.py`,
`test_azbit_gaps.py`, `test_providers.py`, `test_azbit_cli.py`,
`test_azbit_freqtrade_compat.py`, `test_azbit_live_api.py`): symbol/timeframe
mapping, URL construction, strict parsing (null/missing/non-finite/era),
timestamp forms (naive/`Z`/millis/offsets/unix), error envelopes,
retry/`Retry-After`/backoff, reference+pairs/tickers discovery, the
1000+430 pagination walk, boundary dedupe, stall/max-request termination,
gap quality verdicts (`CONTIGUOUS`…`INVALID`), provider registry +
legacy-client wrap, the exact `--exchange azbit` CLI commands end to end
(markets/download/probe/depth/zero-data), Freqtrade-loader compatibility,
and opt-in (`-m live`) real-API probes.

**Runtime-binding tests** (`test_runtime.py`, `test_strategy_discovery.py`,
`test_e2e_runtime.py`): venv discovery (POSIX + Windows layouts), runtime
probe, `resolve_runtime` error paths, re-exec environment-identity (no
re-exec when already in the venv), `doctor`/diagnostic output, deterministic
strategy discovery (nested NFI layout, flat-wins-over-nested, bundled
fallback, `__pycache__`/hidden skipping), and the **cross-interpreter
proof**: a Freqtrade-less Python launched with `--repo` re-execs and runs the
backtest on the repo's venv Freqtrade, with the exact runtime recorded in
the result; plus GUI repo binding (`/api/runtime`, `POST /api/repo`, job →
bound runtime).

---

## 9. Nobitex public API audit

The authoritative audit of the documented public API — endpoint-by-endpoint
capability matrix, rate limits, data-availability limits, response shapes,
and the mapping used by the adapter — is in **[docs/API_MATRIX.md](docs/API_MATRIX.md)**.

Key facts (details + sources in the matrix):

* REST base: `https://apiv2.nobitex.ir` (per docs changelog, active since Jun 2025)
* Market discovery: `GET /market/stats?srcCurrency=&dstCurrency=` (per-market
  `isClosed`, `bestBuy/bestSell`, day OHLC, 24h volume)
* OHLCV history: `GET /market/udf/history?symbol=&resolution=&from=&to=&page=`
  — columnar `{s,t,o,h,l,c,v}`, **≤ 500 candles/request**, `no_data` when
  empty, **minute candles only from ≈ 2022-03-20**
* Rate limiting: `429` + JSON `{"code":"TooManyRequests","backOff":N}` — the
  client sleeps exactly `backOff` seconds and retries; order book 300 req/min,
  trades 60 req/min
* Success envelope `{"status":"ok"}` / failure `{"status":"failed",...}`
  (OHLCV endpoint uses the short `{"s":"ok"|"no_data"}` form)

---

## 10. Architecture notes

* **ccxt integration without forking Freqtrade**: `ccxt_nobitex.Nobitex`
  registers into ccxt's sync + async (ccxt.pro) registries; public
  `fetch_markets` is overridden to map `/market/stats`; private methods
  raise clearly (no keys needed for this milestone).
* **One backtest implementation**: `run_backtest()` is shared by CLI, GUI
  (subprocess job) and tests. GUI backtests run in a **subprocess** for
  clean Freqtrade globals and robust cancellation (process kill).
* **Runtime binding**: the CLI re-execs into the selected repo's `.venv`
  before importing Freqtrade (§2). The GUI host may run anywhere; each
  backtest job subprocess re-binds to the *selected* repo's venv. The
  `runtime` block in every result is the audit trail of which Freqtrade ran.
* **Results layer**: `results.parse_backtest_zip()` converts Freqtrade's
  results zip (stats JSON + wallet feather) into a stable dashboard payload;
  per-pair **buy-&-hold** is computed from the downloaded base-timeframe data
  (the zip's `market_change.feather` is an aggregate, not per-pair).
* **Future-ready (out of scope now)**: private API would plug into the same
  ccxt class (keys via env), a `trading_mode: futures` config path already
  exists in `configgen`/symbols, and the job manager generalizes to live jobs.

## 11. Troubleshooting

| Symptom | Cause / fix |
| --- | --- |
| `[runtime] ERROR: Selected .venv does not exist: …` (exit 2) | `--repo` points at a dir without a Freqtrade `.venv`. Point it at your real Freqtrade clone (the one with `.venv/`), or activate/`pip install -e .` a Freqtrade env |
| Backtest ran, but `runtime` shows a Freqtrade version I didn't expect | The GUI/CLI host had its own Freqtrade and no `--repo`/repo-v was selected, so it fell back. Select the intended repo (CLI `--repo` or GUI repo field) and check the `runtime` block |
| `Freqtrade is not installed in selected .venv` | The repo's venv is incomplete. Inside that venv: `pip install -e .` (the Freqtrade repo) or `pip install freqtrade` |
| Two Pythons, two Freqtrade versions, which ran? | Trust the `runtime` block in the result JSON / GUI job details (`freqtrade_module` path). The re-exec log line `[runtime] re-executing …` confirms the switch |
| `required data is missing: …` before backtest | Run `download` with the printed pair/timeframes (or use the GUI Download step) |
| Backtest start clamped to 2022-03-20+ | Nobitex minute-candle history limit — start later |
| `rate limited … backOff=2` in logs | Normal — the client honors the server `backOff` and retries |
| `SUSHI/USDT` etc. greyed out in the UI | Exchange marks it `isClosed` (inactive) |
| Windows: `Activate.ps1` blocked | `Set-ExecutionPolicy -Scope CurrentUser RemoteFirst` |
| TA-Lib build failure | Install the TA-Lib C library first, or use a wheel; then `pip install -e .` |
