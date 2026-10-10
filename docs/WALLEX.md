# Wallex provider — public API matrix & data semantics

Source: the official Wallex API reference, https://api-docs.wallex.ir/
(markets sections, read 2026-10-10). All endpoints below are PUBLIC and
keyless — only the account/order/OTC sections of the reference need the
`X-API-Key` header, and the adapter never touches those.

Base URL (per the reference): `https://api.wallex.ir`
(`WALLEX_API_BASE` overrides it for tests / the mock server).

## 1. Endpoints (public, keyless)

### `GET /v1/markets` — market discovery

No parameters. Response envelope:

```json
{
  "success": true,
  "message": "...",
  "result": {
    "symbols": {
      "BTCUSDT": {
        "symbol": "BTCUSDT",
        "baseAsset": "BTC",
        "quoteAsset": "USDT",
        "stats": {
          "bidPrice": "20900.0000000000000000",
          "askPrice": "21097.7400000000000000",
          "24h_ch": -0.04,
          "24h_volume": "1.9666210000000000",
          "24h_quoteVolume": "41471.0065635500000000",
          "24h_highPrice": "22888.0000000000000000",
          "24h_lowPrice": "20349.0000000000000000",
          "lastPrice": "20991.4300000000000000"
        },
        "createdAt": "2020-04-01T00:00:00.000000Z"
      }
    }
  }
}
```

Notes:

* discovery uses the explicit `baseAsset`/`quoteAsset` fields (falling back
  to symbol splitting when they are absent);
* market entries carry **no active/disabled flag** — every listed symbol is
  reported active;
* prices/volumes are number-strings (`"20991.43…"`) and parse with `float()`.

### `GET /v1/udf/history` — OHLCV candles

UDF TradingView-style history (a format sibling of Nobitex
`/market/udf/history`):

```
GET /v1/udf/history?symbol=BTCTMN&resolution=60&from=1654350171&to=1655502171
```

| Param | Required | Meaning |
| --- | --- | --- |
| `symbol` | yes | market symbol, English, e.g. `BTCTMN` |
| `resolution` | yes | candle timeframe precision (see §2 — only `60` is exemplified) |
| `from` / `to` | yes | range as **unix-second** timestamps |

Response — RAW UDF, no `success` envelope:

```json
{
  "s": "ok",
  "t": [1654351200, 1654354800],
  "o": ["950625912.0000000000", "948896452.0000000000"],
  "h": ["954590146.0000000000", "961381312.0000000000"],
  "l": ["948896452.0000000000", "948896452.0000000000"],
  "c": ["948896452.0000000000", "960015360.0000000000"],
  "v": ["0.2314400000", "0.3148050000"]
}
```

* `t` = candle OPEN time, unix seconds (UTC);
* `o/h/l/c/v` = number-strings;
* `s: "no_data"` = no candles in the window (NOT an error at the client
  layer; the downloader turns a fully-empty task into a hard `ERROR`);
* any other `s` (e.g. `s: "error"`) = `WallexAPIError` with the exact
  request context.

## 2. Symbol & timeframe mapping

Symbols are concatenated exactly like Nobitex (`BTCUSDT`), with the extra
fiat quote **TMN (Toman)** where Nobitex uses `IRT`/`RLS`:

* `wallex_to_freqtrade`: `BTCUSDT` → `BTC/USDT`, `BTCTMN` → `BTC/TMN`;
* `freqtrade_to_wallex`: `BTC/USDT` → `BTCUSDT` (generic, no pair lists).

### Provisional resolution ladder

The reference documents the `resolution` parameter but gives only ONE
example value: **`resolution=60`** (minutes). The ladder below mirrors the
documented Nobitex UDF ladder (same minute/hour/day convention) and is
**UNVERIFIED** — no entry has passed a live request yet (see
[Live acceptance status](WALLEX-LIVE-ACCEPTANCE.md)). Only `60` has
documentation evidence (the reference's own example); every other entry is
a provisional inference that must be confirmed live before use.

| Freqtrade | Wallex `resolution` | Status |
| --- | --- | --- |
| `1m` | `1` | UNVERIFIED |
| `5m` | `5` | UNVERIFIED |
| `15m` | `15` | UNVERIFIED |
| `30m` | `30` | UNVERIFIED |
| `1h` | `60` | DOCUMENTED example (not live-verified) |
| `3h` | `180` | UNVERIFIED |
| `4h` | `240` | UNVERIFIED |
| `6h` | `360` | UNVERIFIED |
| `12h` | `720` | UNVERIFIED |
| `1d` | `D` | UNVERIFIED |
| `2d` | `2D` | UNVERIFIED |
| `3d` | `3D` | UNVERIFIED |

Verify on a networked machine (fails loudly on any rejected resolution,
which then gets corrected in `timeframes.WALLEX_RESOLUTIONS`):

```bash
python -m pytest tests/test_wallex_live_api.py -m live -q
```

A rejected resolution surfaces as `WallexAPIError` naming the exact
request — resolutions are never silently substituted.

## 3. Timestamp semantics

* `t` values are unix **seconds**, candle-open aligned (docs example
  `1654351200` = an hour boundary for `resolution=60`);
* the client treats them as UTC open times without rounding or alignment;
* the validator flags irregular spacing, so any non-grid data can never
  look clean — OHLCV availability ≠ strategy compatibility.

## 4. Pagination (`history_range`)

The reference documents **no `page` parameter and no row cap**, so the
client walks the window with a cursor (`from = last_ts + 1`):

1. request `[cursor, end_ts)` (cursor starts at the window start);
2. empty response / `no_data` → done (no data remains in range);
3. `last_ts >= end_ts - 1` → done (range fully covered);
4. otherwise advance the cursor past the last received candle;
5. boundary rows re-fetched across pages are deduplicated by `ts`;
6. STALL (no forward progress) or `max_requests` → hard error —
   never an infinite loop, never a silent truncation.

Termination never depends on a guessed cap. `discover_depth` uses single
wide windows (not a full-history walk): the wide probe's earliest row is
the first available candle (UDF windows answer oldest-first; verified by
live depth runs).

## 5. Rate limits, retry, timeouts

* Wallex documents **no rate limits**: the client defaults to gentle
  limits (`/v1/markets` 1 rps, `/v1/udf/history` 5 rps);
* `429` honors the `Retry-After` header, then JSON body hints
  (`retry_after`/`backOff`), defaulting to 5 s, with capped retries;
* network errors / 5xx retry with exponential backoff (5 attempts);
* 4xx (other than 429) and `success: false` envelopes raise immediately
  with full request context. No credentials are ever sent.

## 6. Data quality & gap policy

Same as every provider: strict UDF parsing (column-length match, numeric
fields, timestamp sanity), chronological ordering, duplicate removal,
gap detection in the validator (`CONTIGUOUS`/`GAPPED`/`DUPLICATE`/
`OUT_OF_ORDER`/`EMPTY`/`INVALID`), zero-data tasks as hard `ERROR` with
the exact empty requests + a probe command. Gaps are reported, never
filled, never hidden.

## 7. Commands (exact)

```bash
# market discovery (USDT + TMN quotes)
python -m nobitex_adapter --exchange wallex markets --quote USDT
python -m nobitex_adapter --exchange wallex markets --quote TMN --json

# read-only quality proof BEFORE downloading (rows/first/last/dups/gaps/quality)
python -m nobitex_adapter --exchange wallex probe \
  --pair BTC/USDT --timeframes "5m,15m,1h,4h,1d" \
  --start 2024-06-01 --end 2024-06-06

# historical depth + COMMON range for X8
python -m nobitex_adapter --exchange wallex depth \
  --pair BTC/USDT --timeframes "5m,15m,1h,4h,1d"

# download (writes user_data/data/wallex/BTC_USDT-<tf>.feather)
python -m nobitex_adapter --exchange wallex download \
  --pairs BTC/USDT --timeframes "5m,15m,1h,4h,1d" \
  --start 2024-06-01 --end 2024-06-06

# validate what was stored
python -m nobitex_adapter --exchange wallex validate \
  --pairs BTC/USDT --timeframes "5m,15m,1h,4h,1d" \
  --start 2024-06-01 --end 2024-06-06

# offline testing against the mock Wallex API
nobitex-mock --exchange wallex --port 8901   # terminal 1
WALLEX_API_BASE=http://127.0.0.1:8901 python -m nobitex_adapter \
  --exchange wallex markets --quote USDT     # terminal 2
```

## 8. Fees (for backtest config)

The account-fee endpoint (`GET /v1/account/fee`, private — not used by the
adapter) documents `makerFeeRate`/`takerFeeRate` of `"0.00200000"` on spot
markets, matching the adapter-wide assumed spot fee
(`configgen.DEFAULT_SPOT_FEE = 0.002`). Override per run with
`backtest --fee`.

## 9. Live verification checklist (networked machine)

```bash
# 1) discovery + history shape + pagination + resolution ladder
python -m pytest tests/test_wallex_live_api.py -m live -q

# 2) end-to-end download of the X8 set on real data (small range first)
python -m nobitex_adapter --exchange wallex download \
  --pairs BTC/USDT --timeframes "5m,15m,1h,4h,1d" \
  --start 2024-06-01 --end 2024-06-06

# 3) depth discovery on real data (confirms the oldest-first assumption)
python -m nobitex_adapter --exchange wallex depth \
  --pair BTC/USDT --timeframes "5m,15m,1h,4h,1d"
```

Record the outcomes (row counts, earliest timestamps, any rejected
resolution) before trusting Wallex data for strategy decisions.
