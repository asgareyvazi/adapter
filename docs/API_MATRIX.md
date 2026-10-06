# Nobitex Public API — Capability Matrix

**Audit target:** the *documented public* REST API, as published in the
official Nobitex docs repository
([github.com/nobitex/docs-api](https://github.com/nobitex/docs-api), master,
slate markdown sources under `source/includes/`), audited 2026-10-06.

**REST base URL:** `https://apiv2.nobitex.ir`
(docs changelog: the base moved from `api.nobitex.ir` to `apiv2.nobitex.ir`
in June 2025; all current docs target `apiv2`).

**Auth:** none for the endpoints below. (Private endpoints use header
`Authorization: Token <hex>` — out of scope for this milestone; the adapter
never sends credentials.)

---

## 1. Endpoint capability matrix

| # | Endpoint | Method | Auth | Purpose | Key parameters | Response shape | Rate limit (documented) | Used by adapter? |
|---|----------|--------|------|---------|----------------|----------------|--------------------------|------------------|
| 1 | `/market/stats` | GET | public | **Market discovery + spot ticker**: one entry per market with price, 24h volume, day OHLC, open/closed flag | `srcCurrency` (optional), `dstCurrency` (optional) — filter by base/quote | `{"status":"ok","stats":{"btc-usdt":{"isClosed":false,"bestBuy":"…","bestSell":"…","latest":"…","volumeSrc":"…","volumeDst":"…","dayOpen/High/Low/Close/Change":…,"mark":…}}}` | n/a (used at 0.3 req/s by the client) | **Yes** — `NobitexClient.discover_markets()` (GUI *Check Markets*, pair normalization) |
| 2 | `/market/udf/history` | GET | public | **Historical OHLCV** (candlestick) | `symbol` (e.g. `BTCUSDT`), `resolution` (see §2), `from`/`to` (unix **seconds**) or `countback`, `page` (1-based) | Columnar: `{"s":"ok","t":[…],"o":[…],"h":[…],"l":[…],"c":[…],"v":[…]}`; **≤ 500 candles per response**; `{"s":"no_data"}` when the range is empty | client paces at 10 req/s | **Yes** — the only data endpoint; paginated by `page` in `candles_range()` |
| 3 | `/v3/orderbook/{SYMBOL}` | GET | public | Live order book (30 levels) | path symbol | `{"status":"ok","bids":[[price,qty]…],"asks":[[price,qty]…],"lastUpdate":…}` | **300 req/min** | No (not needed for backtests; client method exists) |
| 4 | `/v2/trades/{SYMBOL}` | GET | public | Recent public trades | path symbol | `{"status":"ok","trades":[…]}` | **60 req/min** | No (client method exists) |
| 5 | `/v2/depth/{SYMBOL}` | GET | public | Depth snapshot (older v2 variant) | path symbol | `{"status":"ok",…}` | **300 req/min** | No |

Private endpoints (out of scope): account/balance, orders, positions,
`/wallet`-family — all require the `Token` header. The ccxt class
implements/forwards them only if credentials were provided; in this
milestone they are never exercised.

> **Note on the newer OpenAPI spec:** docs issue #343 references a newer
> OpenAPI-based API (`apidocs.nobitex.ir/openapi/spot_trade.yaml`,
> `/v2/common/pairs`, `/v2/spot/ticker`). That domain/spec was **not
> reachable from the build environment** at audit time, so the adapter
> implements the fully-documented `apiv2` contract above (the stable,
> in-production public API) and can adopt the new spec without changes to
> the service layer when it is available.

---

## 2. `resolution` values (timeframes) for `/market/udf/history`

Exactly as documented (no other values accepted):

| Class | Values | Freqtrade mapping (adapter) |
|-------|--------|------------------------------|
| Minutes | `1`, `5`, `15`, `30` | `1m`, `5m`, `15m`, `30m` |
| Hours | `60`, `180`, `240`, `360`, `720` | `1h`, `3h`, `4h`, `6h`, `12h` |
| Days | `D`, `2D`, `3D` | `1d`, `2d`, `3d` |

**X8 needs** `5`, `15`, `60`, `240`, `D` per pair + `240` for BTC/USDT —
all available.

### Data-availability limits (verified against the live API contract)

| Class | Earliest available candle |
|-------|---------------------------|
| Minute resolutions (`1/5/15/30`) | **≈ 2022-03-20** (documented "1401" limitation) |
| Hourly / daily resolutions | ≈ 2019-01-01 (deeper history) |

Consequences (handled in code):
* backtest **starts** earlier than 2022-03-20 at 5m are clamped with a
  warning — a 3.5-year backtest must start ≥ ~2022-04;
* a range fully before the data start returns `no_data` → the downloader
  marks the chunk `empty` (not an error).

---

## 3. Rate limiting & error contract (documented behavior)

| Signal | Shape | Adapter behavior |
|--------|-------|------------------|
| Rate limit exceeded | HTTP **429** with `{"status":"failed","code":"TooManyRequests","message":…,"backOff":<seconds>,"limit":<n>}` | sleep **exactly `backOff` seconds**, retry (up to 5×); per-endpoint client-side limiter stays under the documented caps (history 10 req/s, orderbook ≤ 300/min, trades ≤ 60/min, stats 20/min) |
| Logical failure | `{"status":"failed","code":…,"message":…}` | raise `NobitexAPIError` with code + request context (no retry for 4xx-class) |
| Server errors 5xx / network | — | exponential backoff 1s→2s→4s→8s→16s (≤30s), max 5 retries, then raise with full context |
| Success | `{"status":"ok", …}` (or `{"s":"ok"}` on the OHLCV endpoint) | parse |
| Empty OHLCV range | `{"s":"no_data"}` | `NobitexNoData` → treated as "data edge", never as failure |
| Repeated abuse | docs warn: repeated violations → ~2-minute token ban | avoided by honoring `backOff` + client pacing |

---

## 4. Mapping table used by the adapter

| Nobitex | Adapter internal | Freqtrade/CCXT |
|---------|------------------|----------------|
| `BTCUSDT` (concatenated symbol) | `BTC/USDT` (normalized, deterministic) | `BTC/USDT` (ccxt symbol) |
| quote detection = longest known quote suffix (`USDT`, `USDC`, `USD`, `IRT`, `BTC`, `ETH`, …) | `symbols._split` | `BASE/QUOTE` |
| `resolution` `5` | timeframe `5m` | `timeframe: "5m"` |
| `from`/`to` unix seconds (UTC) | tz-aware UTC datetimes | tz-aware UTC `date` column in feather |
| candle `ts` = **open time** (unix s) | — | freqtrade candle open time |
| `isClosed: true` | `Market.active = False` | excluded from "usable for backtest" |
| `volumeSrc` / `volumeDst` | 24h volume base/quote | recommended-pair ranking |

---

## 5. Capability summary vs. milestone requirements

| Requirement | Verdict | Evidence |
|-------------|---------|----------|
| Dynamic USDT market discovery | ✅ | `/market/stats?dstCurrency=USDT` → deterministic normalized pairlist; closed-market flag respected |
| OHLCV for arbitrary 3.5y ranges | ✅ (with documented start clamp) | `/market/udf/history` pagination ≤500/req; minute data from ≈2022-03-20 |
| Retry / backoff / rate limit | ✅ | 429 + `backOff` honored; per-endpoint pacing; exponential backoff |
| Ticker/orderbook/trades (live) | ✅ available | endpoints 1/3/4/5 (not required for backtests; client methods exist) |
| Private trading | ⛔ out of scope (by design) | Token auth documented; ccxt class future-ready; no keys ever read |
| Futures | ⛔ not exposed by Nobitex public spot docs | architecture future-proofed (`trading_mode` seam), GUI disables Futures |
