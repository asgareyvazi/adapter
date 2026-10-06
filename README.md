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
| `nobitex_adapter/` | The adapter package (client, downloader, validator, ccxt exchange class, config generator, backtest runner, results parser, job manager, mock API server) |
| `nobitex_adapter/webui/` | FastAPI app + dependency-free static UI (dark, RTL/farsi + English) |
| `tests/` | 154 tests: unit, integration (against the in-repo mock API), e2e (real Freqtrade + X8) |
| `user_data/strategies/NostalgiaForInfinityX8.py` | The real X8 strategy, **unmodified** (provenance in `STRATEGY_SOURCE.txt`) |
| `user_data/nobitex_gui/` | Generated run configs, job state, logs, saved dashboards (git-ignored) |
| `user_data/data/nobitex/` | Downloaded feather data (git-ignored) |
| `docs/API_MATRIX.md` | **Nobitex public API capability matrix** (audit deliverable) |

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

## 2. Quickstart — the GUI (Backtest Manager)

```bash
# Linux/macOS
source .venv/bin/activate
python -m nobitex_adapter ui --host 0.0.0.0 --port 8765
# Windows PowerShell
.venv\Scripts\Activate.ps1
python -m nobitex_adapter ui --host 0.0.0.0 --port 8765
```

Open **http://localhost:8765** and follow the 5 steps:

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

## 3. Quickstart — the CLI

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
python -m nobitex_adapter backtest \
    --strategy NostalgiaForInfinityX8 \
    --pairs BTC/USDT,ETH/USDT,SOL/USDT \
    --start 2024-01-01 --end 2024-03-31 \
    --capital 10000 --stake unlimited --max-open 8 --fee 0.002

# Windows PowerShell equivalents — same commands, backslash→newline via ``
python -m nobitex_adapter download `
    --pairs BTC/USDT,ETH/USDT,SOL/USDT `
    --timeframes 5m,15m,1h,4h,1d `
    --start 2024-01-01 --end 2024-03-31
```

Useful flags: `download --force` (ignore resume manifest), `--startup
5m:850,1d:260` (per-tf lead-in overrides), `--keep-incomplete`; `backtest
--out results.json` (machine-readable result descriptor), `--skip-precheck`.

---

## 4. X8 data requirements (auto-handled)

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

## 5. Data quality guarantees

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

---

## 6. Tests

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

---

## 7. Nobitex public API audit

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

## 8. Architecture notes

* **ccxt integration without forking Freqtrade**: `ccxt_nobitex.Nobitex`
  registers into ccxt's sync + async (ccxt.pro) registries; public
  `fetch_markets` is overridden to map `/market/stats`; private methods
  raise clearly (no keys needed for this milestone).
* **One backtest implementation**: `run_backtest()` is shared by CLI, GUI
  (subprocess job) and tests. GUI backtests run in a **subprocess** for
  clean Freqtrade globals and robust cancellation (process kill).
* **Results layer**: `results.parse_backtest_zip()` converts Freqtrade's
  results zip (stats JSON + wallet feather) into a stable dashboard payload;
  per-pair **buy-&-hold** is computed from the downloaded base-timeframe data
  (the zip's `market_change.feather` is an aggregate, not per-pair).
* **Future-ready (out of scope now)**: private API would plug into the same
  ccxt class (keys via env), a `trading_mode: futures` config path already
  exists in `configgen`/symbols, and the job manager generalizes to live jobs.

## 9. Troubleshooting

| Symptom | Cause / fix |
| --- | --- |
| `required data is missing: …` before backtest | Run `download` with the printed pair/timeframes (or use the GUI Download step) |
| Backtest start clamped to 2022-03-20+ | Nobitex minute-candle history limit — start later |
| `rate limited … backOff=2` in logs | Normal — the client honors the server `backOff` and retries |
| `SUSHI/USDT` etc. greyed out in the UI | Exchange marks it `isClosed` (inactive) |
| Windows: `Activate.ps1` blocked | `Set-ExecutionPolicy -Scope CurrentUser RemoteFirst` |
| TA-Lib build failure | Install the TA-Lib C library first, or use a wheel; then `pip install -e .` |
