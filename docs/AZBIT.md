# AZBit provider — public API matrix & data semantics

Public historical OHLCV from **AZBit** (`https://data.azbit.com`) into the
adapter's normalized candles → Freqtrade data format → backtest pipeline.
**Historical data + backtest only** — no keys, no private endpoints, no
orders, no synthetic data.

> **Do not assume AZBit `minutes5` means perfectly continuous 300-second
> Freqtrade candles. The adapter validates actual timestamps and reports
> gaps.**

Evidence classes used below: **DOCS** (official documentation),
**OBSERVED** (verified against the real API from PowerShell by the user),
**MOCK** (in-repo mock behaviour — never mistaken for real depth),
**ASSUMPTION** (explicitly flagged where the docs are silent).

---

## 1. Endpoints (public, keyless)

| Endpoint | Purpose | Docs |
| --- | --- | --- |
| `GET /api/ohlc?interval=&currencyPairCode=&start=&end=` | Historical OHLCV | **DOCS** — https://data.azbit.com/docs/ (`Get candles`), https://docs.azbit.com/docs/spot/tickers/ (`/api/ohlc`) |
| `GET /api/currencies/pairs` | Reference pair listing (market discovery, primary) | **DOCS** — Reference-data operations list on https://data.azbit.com/docs/ |
| `GET /api/tickers?currencyPairCode=` | 24h tickers (market discovery fallback + price enrichment) | **DOCS** — https://docs.azbit.com/docs/spot/tickers/ (`/api/tickers`) |

Base URL default `https://data.azbit.com` (**DOCS**); override with
`AZBIT_API_BASE` (tests/mock only).

### `GET /api/ohlc` — request

| Param | Type | Example | Notes |
| --- | --- | --- | --- |
| `interval` | string | `minutes5` | **DOCS** vocabulary: `year, month, day, hour4, hour, minutes30, minutes15, minutes5, minutes3, minute` |
| `currencyPairCode` | string | `BTC_USDT` | **DOCS** example |
| `start` / `end` | date-time | `2021-02-05T14:00:00` | **DOCS** example (offset-less); the adapter sends UTC `YYYY-MM-DDTHH:MM:SS` |

### `GET /api/ohlc` — response

A bare JSON **array** (**DOCS** example + **OBSERVED**):

```json
[
  {
    "date": "2024-06-01T00:00:59",
    "open": 67560.4,
    "max": 67627.4,
    "min": 67544.6,
    "close": 67627.4,
    "volume": 1.4022894,
    "volumeBase": 94777.97787651
  }
]
```

Field mapping (Freqtrade `date/open/high/low/close/volume`):

| AZBit | Freqtrade | Notes |
| --- | --- | --- |
| `date` | `date` | ISO-8601 → unix seconds (UTC); see §3 |
| `open` | `open` | verbatim |
| `max` | `high` | verbatim |
| `min` | `low` | verbatim |
| `close` | `close` | verbatim |
| `volume` | `volume` | verbatim (base-asset volume) |
| `volumeBase` | — (auxiliary) | kept on the candle, **never** substituted for `volume` |

Errors use `{ "Code": <int>, "Message": "<text>" }` (**DOCS** — branch on
`Code`). The client raises on that envelope even on HTTP 200.

---

## 2. Symbol & timeframe mapping

**Symbols (generic, nothing hardcoded):** `BASE/QUOTE` ⇄ `BASE_QUOTE`
(`BTC/USDT` ⇄ `BTC_USDT`; `symobls.freqtrade_to_azbit` /
`azbit_to_freqtrade`). Callers never write provider syntax.

**Timeframes** (`timeframes.to_azbit_interval`, validated against the
**DOCS** vocabulary — never blind-hardcoded):

| Freqtrade | AZBit `interval` |
| --- | --- |
| `1m` | `minute` |
| `5m` | `minutes5` |
| `15m` | `minutes15` |
| `30m` | `minutes30` |
| `1h` | `hour` |
| `4h` | `hour4` |
| `1d` | `day` |

AZBit has **no** `3h/6h/12h/2d/3d` equivalent → `TimeframeError` when
`exchange="azbit"` (via the same `normalize_timeframes` boundary every
caller uses; `--exchange azbit download --timeframes "5m,3h"` fails
clearly with exit 2).

X8 (`5m/15m/1h/4h/1d`) is fully covered by the mapping — but mapping ≠
data: use `probe`/`depth` to prove history exists (§6).

---

## 3. Timestamp semantics (critical)

**OBSERVED** (real `minutes5` rows): `00:00:59, 00:06:37, 00:11:30,
00:16:15, 00:21:48, …` — floating, not grid-aligned. Gaps in the sample:
avg ≈ 303s, min 27s, max 7731s.

What is proven vs open:

| Question | Answer |
| --- | --- |
| Is `date` the candle open time? | **UNPROVEN** — the docs do not define it (open vs close vs aggregation time is unknown). Treated as the candle's timestamp, stored verbatim. |
| Is `minutes5` a fixed 300s bucket grid? | **DISPROVEN as a safe assumption** — observed rows are not on 5-minute boundaries. |
| Are large gaps missing candles? | **LIKELY** (7731s ≈ 25 missing 5m slots) but unproven per-gap; reported as gaps, never filled. |
| Timezone of offset-less `date`? | **ASSUMPTION: UTC** (exchange convention; docs examples carry no offset). Flagged here, not hidden. |
| Sub-second fractions (`…22.621Z` in docs)? | **Truncated** (never rounded) when stored as unix seconds; the raw string is kept for diagnostics. A resolution reduction, NOT a bucket alignment. |

**Policy: the adapter NEVER aligns timestamps** (`floor(ts/300)*300` and
friends are forbidden). Floating rows stay floating; grid conformance is
reported by validation (`quality` = `CONTIGUOUS` / `GAPPED` / …), and a
gapped dataset can never masquerade as a clean one.

---

## 4. Pagination (`ohlc_range`)

**OBSERVED**: `2024-06-01→2024-06-06 minutes5` returned `COUNT=1000`
(`2024-06-01T00:00:59` … `2024-06-04T12:11:43`); the remainder query
returned 430 rows. So responses are **capped (~1000 rows)** and the range
must be walked — the fetcher never trusts one response.

Algorithm (`azbit_client.ohlc_range`):

1. request `[cursor, end)` (`cursor` starts at `start`)
2. empty array → done (**OBSERVED**: `start` skips empty prefixes — a query
   from `12:10:00` returned rows from `12:11:43` — so empty means *no data
   remains in range*, not *a gap*)
3. short page (`< page_cap`) → done (range fully covered)
4. full page → `cursor = last_ts + 1s`, continue
5. boundary rows re-fetched across pages are deduplicated (last wins)
6. **stall** (full page, no forward progress) or `max_requests` (default
   2000) → hard `AzbitAPIError`, never an infinite loop, never a silent
   truncation
7. result sorted ascending by `ts`

`page_cap` (default 1000) and `max_requests` are constructor-configurable
because the cap is observed, not documented.

---

## 5. Data quality & gap policy

The shared validator (`validator.validate`, `quality` verdict) classifies
every dataset — AZBit and Nobitex alike:

| Quality | Meaning |
| --- | --- |
| `CONTIGUOUS` | every candle exactly one interval apart, no defects |
| `GAPPED` | spacing defects (missing intervals / irregular spacing) |
| `DUPLICATE` | repeated timestamps |
| `OUT_OF_ORDER` | non-monotonic timestamps |
| `EMPTY` | no rows |
| `INVALID` | broken OHLC/volume values |

Priority: `EMPTY > INVALID > OUT_OF_ORDER > DUPLICATE > GAPPED >
CONTIGUOUS`. All counters stay visible next to the verdict.

Hard rules (shared with Nobitex, enforced in code + tests):

* **no synthetic candles** — no forward-fill, no zero-volume fabrication
* **no silent drops** — duplicates are removed only with `repair=True`,
  recorded in the report; the downloader's merge dedupe is counted
  (`duplicates_removed`)
* **zero data = hard `ERROR`** (task `ERROR`, summary not ok, exit 1) with
  the exact empty requests seen + the probe command to reproduce
* **gaps fail loudly**: a `GAPPED` AZBit dataset keeps `status=FAIL`-level
  visibility through spacing problems (never `PASS`), so a backtest on
  floating `minutes5` rows cannot look clean

---

## 6. Historical depth & the X8 common range

Nothing about real AZBit depth is hardcoded: `depth` discovers it with
real probes per (pair, timeframe):

* **wide probe** `[2015-01-01 .. now]` — first row = earliest (prefix
  skipping); if empty, bounded bisection (~12 probes) finds the first
  non-empty window
* **recent probe** (10-candle window at now) — last row = latest

`COMMON_EARLIEST = max(earliest over timeframes)` (and `COMMON_LATEST =
min(latest)`) is the range every timeframe shares — the only honest
download `--start` for a multi-timeframe strategy. Strategy requirements
(`5m/15m/1h/4h/1d` for X8) are read from the strategy source
(`configgen.detect_strategy_timeframes`), never hardcoded.

> Real-depth numbers can only come from the real API (unreachable from the
> Arena sandbox). Run the commands in §8 from a networked machine.

---

## 7. Rate limits, retry, timeouts

* Client-side: 5 rps `/api/ohlc`, 2 rps discovery endpoints (conservative —
  AZBit publishes no anonymous limits; 30 rps is documented for signed
  trading only).
* `429` honours `Retry-After` (then JSON `retryAfter*`/`backOff*`,
  default 5s) with bounded retries; network errors/5xx retry with
  exponential backoff (default 5 attempts, 25s timeout — all
  constructor-configurable).

---

## 8. Commands (exact)

```powershell
cd "C:\Users\A-Eyvazi\Downloads\adapter-arena-dc3ed543-adapter"

# market discovery
python -m nobitex_adapter --exchange azbit markets --quote USDT

# read-only quality proof BEFORE downloading (rows/first/last/dups/gaps/quality)
python -m nobitex_adapter --exchange azbit probe `
  --pair BTC/USDT `
  --timeframes "5m,15m,1h,4h,1d" `
  --start 2024-06-01 `
  --end 2024-06-06

# historical depth + COMMON range for X8
python -m nobitex_adapter --exchange azbit depth `
  --pair BTC/USDT `
  --timeframes "5m,15m,1h,4h,1d"

# download (writes user_data/data/azbit/BTC_USDT-<tf>.feather)
python -m nobitex_adapter --exchange azbit download `
  --pairs BTC/USDT `
  --timeframes "5m,15m,1h,4h,1d" `
  --start 2024-06-01 `
  --end 2024-06-06

# validate what was stored
python -m nobitex_adapter --exchange azbit validate `
  --pairs BTC/USDT `
  --timeframes "5m,15m,1h,4h,1d" `
  --start 2024-06-01 `
  --end 2024-06-06
```

Per-command `--exchange` also works (`download --exchange azbit …`);
`--json` on `probe`/`depth` prints machine-readable output.

---

## 9. OHLCV availability ≠ strategy compatibility

Proving rows exist (`probe` shows 1430 rows) does **not** prove X8 can use
them: X8 needs indicator warmup (`startup_candle_count = 800` on 5m plus
informative warmup) and informative alignment across `15m/1h/4h/1d`. The
pipeline keeps the distinction explicit:

1. `probe` → rows exist and how clean they are (`quality`, gap stats)
2. `depth` → the `COMMON_EARLIEST` all timeframes share
3. `download` → prepends per-timeframe warmup automatically
4. `validate` → per-dataset verdict before any backtest
5. backtest precheck → fails fast listing every missing (pair, timeframe)
   file instead of running on partial data
