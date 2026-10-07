"""A faithful mock of the AZBit public market-data API (testing tool).

Implements the documented public endpoints used by the adapter:

  * GET /api/ohlc?interval=&currencyPairCode=&start=&end=
  * GET /api/currencies/pairs
  * GET /api/tickers?currencyPairCode=

Data is synthetic but DETERMINISTIC (seeded per pair+interval) and
structurally valid:

  * consistent OHLC (max >= max(open,close), min <= min(open,close))
  * aligned candle grid by default (interval-anchored), so downloads of
    the mock are CONTIGUOUS; irregular-timestamp behavior is covered by
    scripted unit tests instead (see tests/test_azbit_client.py)
  * minute data starts 2024-01-01, hour/day data starts 2020-01-01
    (mirrors "shallower minute history" without copying real AZBit depth,
    which is UNKNOWN — depth must be discovered via the real API)
  * max 1000 rows per response (observed real cap), cursor pagination
    (empty array when the window has no data)
  * error envelope {"Code": ..., "Message": ...} for unknown intervals

This is a TESTING tool. It is never used against real trading paths.
"""
from __future__ import annotations

import hashlib
import random
import time
from datetime import datetime, timezone
from typing import Optional

from fastapi import FastAPI, Query
from fastapi.responses import JSONResponse

app = FastAPI(title="AZBit mock (public market data)")

# ------------------------------------------------------------------ catalog
PAIRS = [
    "BTC_USDT", "ETH_USDT", "SOL_USDT", "XRP_USDT", "DOGE_USDT",
    "ADA_USDT", "AVAX_USDT", "LINK_USDT", "DOT_USDT", "TRX_USDT",
    "MATIC_USDT", "LTC_USDT", "BCH_USDT", "ATOM_USDT", "NEAR_USDT",
    "UNI_USDT", "ARB_USDT", "OP_USDT", "INJ_USDT", "SEI_USDT",
    "BTC_ETH", "ETH_BTC",
]
INACTIVE = {"SEI_USDT"}  # listed but inactive (exercises active filtering)

INTERVALS = {
    "minute": 60,
    "minutes3": 180,
    "minutes5": 300,
    "minutes15": 900,
    "minutes30": 1800,
    "hour": 3600,
    "hour4": 14400,
    "day": 86400,
    "month": 2_592_000,   # 30d (mock convention; real semantics undocumented)
    "year": 31_536_000,   # 365d (mock convention)
}

MINUTE_DATA_START = 1_704_067_200  # 2024-01-01 00:00:00 UTC
HOURLY_DATA_START = 1_577_836_800  # 2020-01-01 00:00:00 UTC

PAGE_CAP = 1000


def _base_price(pair: str) -> float:
    seed = int(hashlib.sha256(pair.encode()).hexdigest()[:8], 16)
    return 1.0 + (seed % 100000) / 1000.0


def _seed_for(pair: str, interval: str) -> int:
    return int(hashlib.sha256(f"{pair}:{interval}".encode()).hexdigest()[:12], 16)


_CACHE: dict[tuple, dict] = {}


def _ensure(pair: str, interval: str, upto_ts: int) -> list:
    """Extend the deterministic candle cache for (pair, interval)."""
    key = (pair, interval)
    step = INTERVALS[interval]
    data_start = MINUTE_DATA_START if step < 3600 else HOURLY_DATA_START
    now_stop = int(time.time()) + step
    st = _CACHE.get(key)
    if st is None:
        rng = random.Random(_seed_for(pair, interval))
        st = {
            "rows": [],
            "wm": data_start - step,
            "price": _base_price(pair),
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


def _parse_ts(value: Optional[str]) -> Optional[int]:
    if value is None:
        return None
    text = value.strip().replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp())


@app.get("/api/ohlc")
def ohlc(
    interval: str = Query(...),
    currencyPairCode: str = Query(...),
    start: Optional[str] = Query(None),
    end: Optional[str] = Query(None),
):
    if interval not in INTERVALS:
        return JSONResponse(
            status_code=200,
            content={"Code": 110101, "Message": f"Unknown interval '{interval}'"},
        )
    if currencyPairCode not in PAIRS:
        return JSONResponse(
            status_code=200,
            content={"Code": 110401, "Message": f"Unknown pair '{currencyPairCode}'"},
        )
    start_ts = _parse_ts(start)
    end_ts = _parse_ts(end)
    if start_ts is None or end_ts is None or end_ts <= start_ts:
        return JSONResponse(
            status_code=200,
            content={"Code": 110101, "Message": "Invalid start/end range"},
        )
    step = INTERVALS[interval]
    data_start = MINUTE_DATA_START if step < 3600 else HOURLY_DATA_START
    if end_ts <= data_start:
        return []
    rows = _ensure(currencyPairCode, interval, end_ts)
    sel = [r for r in rows if start_ts <= r[0] < end_ts]
    if not sel:
        return []
    chunk = sel[:PAGE_CAP]
    return [
        {
            "date": datetime.fromtimestamp(r[0], tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S"),
            "open": round(r[1], 10),
            "max": round(r[2], 10),
            "min": round(r[3], 10),
            "close": round(r[4], 10),
            "volume": round(r[5], 8),
            "volumeBase": round(r[4] * r[5], 8),
        }
        for r in chunk
    ]


@app.get("/api/currencies/pairs")
def currencies_pairs():
    return [
        {"currencyPairCode": p, "isActive": p not in INACTIVE}
        for p in PAIRS
    ]


@app.get("/api/tickers")
def tickers(currencyPairCode: Optional[str] = None):
    out = []
    for p in PAIRS:
        if currencyPairCode and p.lower() != currencyPairCode.lower():
            continue
        rng = random.Random(_seed_for(p, "ticker"))
        price = _base_price(p) * (1.0 + rng.uniform(-0.02, 0.02))
        ago = price * (1.0 + rng.uniform(-0.03, 0.03))
        out.append({
            "timestamp": int(time.time() * 1000),
            "currencyPairCode": p,
            "price": round(price, 8),
            "price24hAgo": round(ago, 8),
            "priceChangePercentage24h": round(100 * (price / ago - 1), 3),
            "volume24h": round(rng.uniform(10, 50000), 6),
            "bidPrice": round(price * 0.9999, 8),
            "askPrice": round(price * 1.0001, 8),
            "low24h": round(min(price, ago) * 0.99, 8),
            "high24h": round(max(price, ago) * 1.01, 8),
        })
    return out
