"""A faithful mock of the Nobitex public market-data API.

Implements EXACTLY the documented endpoints (github.com/nobitex/docs-api):
  * GET /market/stats?srcCurrency=&dstCurrency=
  * GET /market/udf/history?symbol=&resolution=&from=&to=&page=
  * GET /v3/orderbook/{symbol}
  * GET /v2/trades/{symbol}

Data is synthetic but DETERMINISTIC (seeded per symbol+resolution) and
structurally valid:
  * consistent OHLC (high >= max(o,c), low <= min(o,c))
  * minute data starts 2022-03-20 (mirrors the documented 1401 limitation)
  * hourly/daily data starts 2019-01-01
  * max 500 candles per response, `page` pagination, `no_data` before start
  * rate limiting via 429 + backOff when `rlimit` is set (for retry tests)

This is a TESTING tool. It is never used against real trading paths.
"""
from __future__ import annotations

import hashlib
import math
import random
import time
from collections import defaultdict, deque
from typing import Optional

from fastapi import FastAPI, Query, Request
from fastapi.responses import JSONResponse

app = FastAPI(title="Nobitex mock (public market data)")

# ---------------------------------------------------------------- markets
# Deterministic market catalog: (symbol, active, base_price_seed)
USDT_PAIRS = [
    "BTCUSDT", "ETHUSDT", "LTCUSDT", "XRPUSDT", "BCHUSDT", "BNBUSDT",
    "EOSUSDT", "XLMUSDT", "ETCUSDT", "TRXUSDT", "DOGEUSDT", "UNIUSDT",
    "DAIUSDT", "LINKUSDT", "DOTUSDT", "AAVEUSDT", "ADAUSDT", "SHIBUSDT",
    "FTMUSDT", "MATICUSDT", "AXSUSDT", "MANAUSDT", "SANDUSDT", "AVAXUSDT",
    "MKRUSDT", "GMTUSDT", "USDCUSDT", "BANDUSDT", "COMPUSDT", "HBARUSDT",
    "WBTCUSDT", "GLMUSDT", "ENSUSDT", "ATOMUSDT", "XTZUSDT", "FLOWUSDT",
    "GALUSDT", "CVCUSDT", "NMRUSDT", "BATUSDT", "TRBUSDT", "RDNTUSDT",
    "YFIUSDT", "SOLUSDT", "TUSDT", "QNTUSDT", "IMXUSDT", "ETHFIUSDT",
    "MEMEUSDT", "BALUSDT", "DAOUSDT", "ONEUSDT", "1INCHUSDT", "OMUSDT",
    "SSVUSDT", "RNDRUSDT", "NEARUSDT", "WOOUSDT", "100K_FLOKIUSDT",
    "JSTUSDT", "ZROUSDT", "ARBUSDT", "APTUSDT", "CELRUSDT", "DYDXUSDT",
    "CVXUSDT", "ALGOUSDT", "MASKUSDT", "SUSHIUSDT", "FETUSDT", "JSTIRT",
]
IRT_PAIRS = ["BTCIRT", "ETHIRT", "USDTIRT", "XRPIRT", "BNBIRT", "ADAIRT", "DOGEIRT"]
# markets that are closed (isClosed=true) to exercise active filtering
CLOSED = {"SUSHIUSDT", "DYDXUSDT"}

KNOWN_QUOTES = {
    "usdt": [p for p in USDT_PAIRS if p.endswith("USDT")],
    "irt": IRT_PAIRS,
}

# data start per timeframe class
MINUTE_DATA_START = 1_647_756_800  # 2022-03-20 00:00:00 UTC (start of 1401)
HOURLY_DATA_START = 1_546_300_800  # 2019-01-01 00:00:00 UTC

RESOLUTIONS = {
    "1": 60, "5": 300, "15": 900, "30": 1800,
    "60": 3600, "180": 10800, "240": 14400, "360": 21600, "720": 43200,
    "D": 86400, "2D": 172800, "3D": 259200,
}

def _split_symbol(symbol: str) -> tuple[str, str]:
    for q in ("USDT", "USDC", "IRT", "BTC", "ETH", "USD", "EUR", "DAI"):
        if symbol.endswith(q) and len(symbol) > len(q):
            return symbol[: -len(q)], q
    raise ValueError(f"unknown symbol {symbol}")


# simple per-symbol base prices (for realism only)
_BASE_PRICE: dict[str, float] = {}
for _p in USDT_PAIRS + IRT_PAIRS:
    _b, _q = _split_symbol(_p)
    _seed = int(hashlib.sha256(_p.encode()).hexdigest()[:8], 16)
    _base = 1.0 + (_seed % 100000) / 1000.0
    if _q == "IRT":
        _base *= 42_000_000  # rials are big
    _BASE_PRICE[_p] = _base


def _seed_for(symbol: str, resolution: str) -> int:
    return int(hashlib.sha256(f"{symbol}:{resolution}".encode()).hexdigest()[:12], 16)


def _base_price(symbol: str) -> float:
    return _BASE_PRICE.get(symbol, 1.0)


# cache: (symbol,resolution) -> {"lst": [...], "wm": last_ts, "state": rng state, "price": float}
_CACHE: dict[tuple, dict] = {}


def _step(rng: random.Random, price: float, drift: float, vol: float, t: int):
    o = price
    c = price * (1.0 + drift + rng.gauss(0, vol))
    h = max(o, c) * (1.0 + abs(rng.gauss(0, vol / 3)))
    lo = min(o, c) * (1.0 - abs(rng.gauss(0, vol / 3)))
    v = rng.uniform(0.1, 50.0)
    return (t, o, h, lo, c, v), c


def _ensure(symbol: str, resolution: str, upto_ts: int) -> list:
    """Extend the deterministic candle cache for (symbol, resolution) to `upto_ts`."""
    key = (symbol, resolution)
    interval = RESOLUTIONS[resolution]
    minute_data = interval < 3600
    data_start = MINUTE_DATA_START if minute_data else HOURLY_DATA_START
    if data_start % interval:
        data_start += interval - (data_start % interval)
    now_stop = int(time.time()) + interval

    st = _CACHE.get(key)
    if st is None:
        rng = random.Random(_seed_for(symbol, resolution))
        drift = (rng.random() - 0.48) * 0.0004
        vol = 0.002 + rng.random() * 0.008
        st = {
            "lst": [],
            "wm": data_start - interval,
            "price": _base_price(symbol),
            "drift": drift,
            "vol": vol,
            "rng": rng,
        }
        _CACHE[key] = st
    lst = st["lst"]
    if st["wm"] >= upto_ts:
        return lst
    rng = st["rng"]
    price = st["price"]
    t = st["wm"] + interval
    while t < min(upto_ts, now_stop) + interval:
        (ts, o, h, lo, c, v), price = _step(rng, price, st["drift"], st["vol"], t)
        if ts >= upto_ts:
            break
        lst.append((ts, o, h, lo, c, v))
        t = ts + interval
    st["wm"] = lst[-1][0] if lst else st["wm"]
    st["price"] = price
    st["rng"] = rng
    return lst


# ------------------------------------------------------------- rate limiting
_rl_buckets: dict[str, deque] = defaultdict(deque)


def _rate_limited(client: str, rlimit: Optional[int]) -> Optional[JSONResponse]:
    if not rlimit:
        return None
    now = time.time()
    dq = _rl_buckets[client]
    while dq and now - dq[0] > 10.0:
        dq.popleft()
    if len(dq) >= rlimit * 10:
        dq.append(now)
        return JSONResponse(
            status_code=429,
            content={
                "status": "failed",
                "code": "TooManyRequests",
                "message": "mock: rate limit exceeded",
                "backOff": 2,
                "limit": rlimit,
            },
        )
    dq.append(now)
    return None


@app.get("/market/stats")
def market_stats(
    request: Request,
    srcCurrency: Optional[str] = None,
    dstCurrency: Optional[str] = None,
    rlimit: Optional[int] = Query(None, description="mock only: requests per second"),
):
    if (rl := _rate_limited(str(request.url.port), rlimit)) is not None:
        return rl
    stats: dict = {}
    for symbol in USDT_PAIRS + IRT_PAIRS:
        base, quote = _split_symbol(symbol)
        if srcCurrency and base.lower() != srcCurrency.lower():
            continue
        if dstCurrency and quote.lower() != dstCurrency.lower():
            continue
        rng = random.Random(_seed_for(symbol, "stats"))
        price = _base_price(symbol) * (1.0 + rng.uniform(-0.02, 0.02))
        day_open = price * (1.0 + rng.uniform(-0.03, 0.03))
        is_closed = symbol in CLOSED
        stats[f"{base.lower()}-{quote.lower()}"] = {
            "isClosed": is_closed,
            "bestSell": str(round(price * 1.0001, 8)),
            "bestBuy": str(round(price * 0.9999, 8)),
            "volumeSrc": str(round(rng.uniform(10, 50000), 6)),
            "volumeDst": str(round(price * rng.uniform(10, 50000), 6)),
            "latest": str(round(price, 8)),
            "mark": str(round(price, 8)),
            "dayLow": str(round(min(price, day_open) * 0.99, 8)),
            "dayHigh": str(round(max(price, day_open) * 1.01, 8)),
            "dayOpen": str(round(day_open, 8)),
            "dayClose": str(round(price, 8)),
            "dayChange": str(round(100 * (price / day_open - 1), 3)),
        }
    return {"status": "ok", "stats": stats}


@app.get("/market/udf/history")
def udf_history(
    request: Request,
    symbol: str = Query(...),
    resolution: str = Query(...),
    from_: Optional[int] = Query(None, alias="from"),
    to: int = Query(...),
    countback: Optional[int] = Query(None),
    page: int = Query(1, ge=1),
    rlimit: Optional[int] = Query(None),
):
    if (rl := _rate_limited(str(request.url.port), rlimit)) is not None:
        return rl
    if resolution not in RESOLUTIONS:
        return {"s": "error", "errmsg": "Invalid resolution!"}
    if symbol not in _BASE_PRICE:
        return {"s": "error", "errmsg": "Invalid market!"}
    minute_data = RESOLUTIONS[resolution] < 3600
    data_start = MINUTE_DATA_START if minute_data else HOURLY_DATA_START
    if from_ is None:
        from_ = to - (countback or 100) * RESOLUTIONS[resolution]
    if to <= data_start or from_ >= to:
        return {"s": "no_data"}
    candles = _ensure(symbol, resolution, to)
    if not candles:
        return {"s": "no_data"}
    sel = [c for c in candles if from_ <= c[0] < to]
    if not sel:
        return {"s": "no_data"}
    PAGE = 500
    chunk = sel[(page - 1) * PAGE : page * PAGE]
    if not chunk:
        return {"s": "no_data"}
    return {
        "s": "ok",
        "t": [c[0] for c in chunk],
        "o": [round(c[1], 10) for c in chunk],
        "h": [round(c[2], 10) for c in chunk],
        "l": [round(c[3], 10) for c in chunk],
        "c": [round(c[4], 10) for c in chunk],
        "v": [round(c[5], 8) for c in chunk],
    }


@app.get("/v3/orderbook/{symbol}")
def orderbook(symbol: str):
    rng = random.Random(_seed_for(symbol, "book") + int(time.time() // 30))
    price = _base_price(symbol) * (1.0 + rng.uniform(-0.001, 0.001))
    bids = [[round(price * (1 - 0.0001 * i), 8), round(rng.uniform(0.01, 2), 6)] for i in range(1, 11)]
    asks = [[round(price * (1 + 0.0001 * i), 8), round(rng.uniform(0.01, 2), 6)] for i in range(1, 11)]
    return {
        "status": "ok",
        "lastUpdate": int(time.time() * 1000),
        "lastTradePrice": str(round(price, 8)),
        "asks": asks,
        "bids": bids,
    }


@app.get("/v2/trades/{symbol}")
def trades(symbol: str):
    rng = random.Random(_seed_for(symbol, "trades") + int(time.time() // 60))
    price = _base_price(symbol)
    out = []
    now = time.time()
    for i in range(20):
        p = price * (1.0 + rng.uniform(-0.002, 0.002))
        out.append({
            "time": int((now - i * rng.uniform(5, 60)) * 1000),
            "price": str(round(p, 8)),
            "volume": str(round(rng.uniform(0.001, 5), 6)),
            "type": "buy" if rng.random() < 0.5 else "sell",
        })
    return {"status": "ok", "trades": out}
