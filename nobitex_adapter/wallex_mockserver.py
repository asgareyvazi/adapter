"""A faithful mock of the Wallex public market-data API (testing tool).

Implements the documented public endpoints used by the adapter
(https://api-docs.wallex.ir/ -> markets section):

  * GET /v1/markets
  * GET /v1/udf/history?symbol=&resolution=&from=&to=

Data is synthetic but DETERMINISTIC (seeded per symbol+resolution) and
structurally valid:

  * documented envelope for /v1/markets (success/result.symbols with
    baseAsset/quoteAsset/stats)
  * UDF columnar history (s/t/o/h/l/c/v with number-strings)
  * consistent OHLC (high >= max(open,close), low <= min(open,close))
  * aligned candle grid by default (resolution-anchored), so downloads of
    the mock are CONTIGUOUS; irregular-timestamp behavior is covered by
    scripted unit tests instead (see tests/test_wallex_client.py)
  * minute data starts 2024-01-01, hour/day data starts 2020-01-01
    (mirrors "shallower minute history" without copying real Wallex depth,
    which is UNKNOWN — depth must be discovered via the real API)
  * max 1000 rows per response (MOCK convention — the real row cap is
    UNDOCUMENTED; the client never depends on it), cursor pagination via
    the from/to window (empty arrays when the window has no data,
    s=no_data when the whole window predates available history)
  * s=error for unknown symbols/resolutions

This is a TESTING tool. It is never used against real trading paths.
"""
from __future__ import annotations

import hashlib
import random
import time

from fastapi import FastAPI, Query
from fastapi.responses import JSONResponse

app = FastAPI(title="Wallex mock (public market data)")

# ------------------------------------------------------------------ catalog
SYMBOLS = [
    "BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT", "DOGEUSDT",
    "ADAUSDT", "AVAXUSDT", "LINKUSDT", "TRXUSDT", "LTCUSDT",
    "BTCTMN", "ETHUSDT", "USDTTMN", "SOLTMN", "DOGETMN",
]
SYMBOLS = sorted(set(SYMBOLS))

RESOLUTIONS = {
    "1": 60,
    "5": 300,
    "15": 900,
    "30": 1800,
    "60": 3600,
    "180": 10800,
    "240": 14400,
    "360": 21600,
    "720": 43200,
    "D": 86400,
    "2D": 172800,
    "3D": 259200,
}

MINUTE_DATA_START = 1_704_067_200  # 2024-01-01 00:00:00 UTC
HOURLY_DATA_START = 1_577_836_800  # 2020-01-01 00:00:00 UTC

PAGE_CAP = 1000  # mock convention only — real cap undocumented


def _base_price(symbol: str) -> float:
    seed = int(hashlib.sha256(symbol.encode()).hexdigest()[:8], 16)
    scale = 50_000_000.0 if symbol.endswith("TMN") else 1.0
    return (1.0 + (seed % 100000) / 1000.0) * scale


def _seed_for(symbol: str, resolution: str) -> int:
    return int(hashlib.sha256(f"{symbol}:{resolution}".encode()).hexdigest()[:12], 16)


_CACHE: dict[tuple, dict] = {}


def _ensure(symbol: str, resolution: str, upto_ts: int) -> list:
    """Extend the deterministic candle cache for (symbol, resolution)."""
    key = (symbol, resolution)
    step = RESOLUTIONS[resolution]
    data_start = MINUTE_DATA_START if step < 3600 else HOURLY_DATA_START
    now_stop = int(time.time()) + step
    st = _CACHE.get(key)
    if st is None:
        rng = random.Random(_seed_for(symbol, resolution))
        st = {
            "rows": [],
            "wm": data_start - step,
            "price": _base_price(symbol),
            "drift": (rng.random() - 0.48) * 0.0004,
            "vol": 0.002 + rng.random() * 0.008,
            "rng": rng,
        }
        _CACHE[key] = st
    rows = st["rows"]
    if st["wm"] >= upto_ts:
        return rows
    rng, price = st["rng"], st["price"]
    t = st["wm"] + step
    while t < min(upto_ts, now_stop) + step:
        if t >= upto_ts:
            break
        o = price
        c = price * (1.0 + st["drift"] + rng.gauss(0, st["vol"]))
        h = max(o, c) * (1.0 + abs(rng.gauss(0, st["vol"] / 3)))
        lo = min(o, c) * (1.0 - abs(rng.gauss(0, st["vol"] / 3)))
        v = rng.uniform(0.1, 50.0)
        rows.append((t, o, h, lo, c, v))
        price = c
        t += step
    st["wm"] = rows[-1][0] if rows else st["wm"]
    st["price"] = price
    return rows


def _split_symbol(symbol: str) -> tuple[str, str]:
    s = symbol.upper()
    for quote in ("USDT", "USDC", "TMN", "BTC", "ETH"):
        if s.endswith(quote) and len(s) > len(quote):
            return s[: -len(quote)], quote
    return s, ""


@app.get("/v1/markets")
def markets():
    symbols: dict[str, dict] = {}
    for sym in SYMBOLS:
        base, quote = _split_symbol(sym)
        rng = random.Random(_seed_for(sym, "ticker"))
        price = _base_price(sym) * (1.0 + rng.uniform(-0.02, 0.02))
        chg = rng.uniform(-5.0, 5.0)
        vol = rng.uniform(10, 50000)
        symbols[sym] = {
            "symbol": sym,
            "baseAsset": base,
            "baseAssetPrecision": 8,
            "quoteAsset": quote,
            "quotePrecision": 8,
            "faName": f"{base} - {quote}",
            "stepSize": 6,
            "tickSize": 2,
            "minQty": 1e-06,
            "minNotional": 10,
            "stats": {
                "bidPrice": f"{price * 0.9999:.10f}",
                "askPrice": f"{price * 1.0001:.10f}",
                "24h_ch": round(chg, 2),
                "7d_ch": round(chg * 2.5, 2),
                "24h_volume": f"{vol:.10f}",
                "24h_quoteVolume": f"{vol * price:.10f}",
                "24h_highPrice": f"{price * 1.01:.10f}",
                "24h_lowPrice": f"{price * 0.99:.10f}",
                "lastPrice": f"{price:.10f}",
            },
            "createdAt": "2020-04-01T00:00:00.000000Z",
        }
    return {"success": True, "message": "ok", "result": {"symbols": symbols}}


@app.get("/v1/udf/history")
def udf_history(
    symbol: str = Query(...),
    resolution: str = Query(...),
    from_: int = Query(..., alias="from"),
    to: int = Query(...),
):
    if resolution not in RESOLUTIONS:
        return {"s": "error", "errmsg": f"Unknown resolution '{resolution}'"}
    if symbol not in SYMBOLS:
        return {"s": "error", "errmsg": f"Unknown symbol '{symbol}'"}
    if to <= from_:
        return {"s": "error", "errmsg": "Invalid from/to range"}
    step = RESOLUTIONS[resolution]
    data_start = MINUTE_DATA_START if step < 3600 else HOURLY_DATA_START
    if to <= data_start:
        return {"s": "no_data"}
    rows = _ensure(symbol, resolution, to)
    sel = [r for r in rows if from_ <= r[0] < to]
    if not sel:
        return {"s": "ok", "t": [], "o": [], "h": [], "l": [], "c": [], "v": []}
    chunk = sel[:PAGE_CAP]
    return {
        "s": "ok",
        "t": [r[0] for r in chunk],
        "o": [f"{r[1]:.10f}" for r in chunk],
        "h": [f"{r[2]:.10f}" for r in chunk],
        "l": [f"{r[3]:.10f}" for r in chunk],
        "c": [f"{r[4]:.10f}" for r in chunk],
        "v": [f"{r[5]:.10f}" for r in chunk],
    }


@app.get("/__debug__/reset")
def debug_reset():
    _CACHE.clear()
    return JSONResponse(status_code=200, content={"reset": True})
