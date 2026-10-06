"""Parse Freqtrade backtest results (zip) into a compact dashboard JSON.

Input: ``user_data/backtest_results/backtest-result-<dt>.zip`` produced by
``freqtrade backtesting --export trades``. Contains:
  * ``backtest-result-<dt>.json`` -- strategy stats (per pair, exit reasons,
    periodic breakdowns, wallet stats, trades, market_change, daily profit)
  * ``backtest-result-<dt>_<Strategy>_wallet.feather`` -- per-candle wallet
    value (equity curve source)
  * ``backtest-result-<dt>_market_change.feather`` -- aggregate market move

Per-pair Buy & Hold is computed directly from the downloaded data files
(equal-weight of first->last close over the backtest window) -- this is the
comparison the user asked for after the LBank backtest surprise.
"""
from __future__ import annotations

import json
import logging
import zipfile
from io import BytesIO
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd

log = logging.getLogger("nobitex.results")


def _f(v, default=0.0) -> float:
    try:
        if v is None:
            return default
        return float(v)
    except (TypeError, ValueError):
        return default


def _buy_hold_from_data(datadir: Path, exchange: str, pairs: list[str], tf: str,
                        start_iso: str, end_iso: str) -> dict[str, float]:
    """Per-pair buy & hold % over [start, end) from the stored base-tf data."""
    out: dict[str, float] = {}

    def _ts(s: str) -> pd.Timestamp:
        t = pd.Timestamp(s)
        if t.tzinfo is None:
            t = t.tz_localize("UTC")
        return t.tz_convert("UTC")

    start = _ts(start_iso)
    end = _ts(end_iso)
    for pair in pairs:
        path = datadir / exchange / f"{pair.replace('/', '_')}-{tf}.feather"
        if not path.is_file():
            continue
        try:
            df = pd.read_feather(path, columns=["date", "close"])
        except Exception:  # noqa: BLE001
            continue
        d = pd.to_datetime(df["date"], utc=True)
        m = (d >= start) & (d < end)
        closes = df["close"].loc[m].astype(float)
        if len(closes) < 2:
            continue
        f, l = float(closes.iloc[0]), float(closes.iloc[-1])
        if f > 0:
            out[pair] = round(100.0 * (l / f - 1.0), 3)
    return out


def parse_backtest_zip(zip_path: Path, *, datadir: Optional[Path] = None) -> dict:
    zip_path = Path(zip_path)
    if not zip_path.is_file():
        raise FileNotFoundError(f"no such results file: {zip_path}")

    stats: dict = {}
    wallet_df: Optional[pd.DataFrame] = None
    config: dict = {}

    with zipfile.ZipFile(zip_path) as zf:
        names = zf.namelist()
        for name in names:
            if name.endswith(".json") and name.startswith("backtest-result-") and "_config" not in name:
                stats = json.loads(zf.read(name).decode("utf-8"))
            elif name.endswith("_config.json"):
                config = json.loads(zf.read(name).decode("utf-8"))
            elif "_wallet.feather" in name:
                wallet_df = pd.read_feather(BytesIO(zf.read(name)))

    strategy_names = list(stats.get("strategy", {}).keys())
    if not strategy_names:
        raise ValueError("results zip contains no strategy stats")
    sname = strategy_names[0]
    s = stats["strategy"][sname]

    start_iso = s.get("backtest_start", "")
    end_iso = s.get("backtest_end", "")
    pairs = s.get("pairlist") or []

    dash = {
        "source": str(zip_path),
        "strategy": sname,
        "backtest_start": start_iso,
        "backtest_end": end_iso,
        "backtest_days": s.get("backtest_days"),
        "timeframe": s.get("timeframe"),
        "pairlist": pairs,
        "starting_balance": _f(s.get("starting_balance")),
        "final_balance": _f(s.get("final_balance")),
        "cards": {},
        "equity_curve": [],
        "drawdown": [],
        "daily": [],
        "per_pair": [],
        "exit_reasons": [],
        "monthly": [],
        "yearly": [],
        "buy_hold": {},
        "trades_count": int(s.get("total_trades") or 0),
        "config": {
            "exchange": (config.get("exchange") or {}).get("name"),
            "stake_currency": config.get("stake_currency"),
            "timerange": config.get("timerange"),
        },
    }

    # ---------------------------------------------------------------- cards
    total_trades = dash["trades_count"]
    winrate = s.get("winrate")
    win_rate = 100.0 * _f(winrate) if winrate is not None else 0.0
    if not win_rate and total_trades:
        trades = s.get("trades") or []
        wins = sum(1 for t in trades if _f(t.get("profit_ratio")) > 0)
        win_rate = 100.0 * wins / total_trades

    wallet_stats = s.get("wallet_stats") or {}
    dd = wallet_stats.get("max_drawdown_account", s.get("max_drawdown_account"))
    dash["cards"] = {
        "total_profit_abs": _f(s.get("profit_total_abs")),
        "return_pct": 100.0 * _f(s.get("profit_total")),
        "max_drawdown_pct": 100.0 * _f(dd),
        "trades": total_trades,
        "win_rate_pct": win_rate,
        "profit_factor": _f(s.get("profit_factor")),
        "sharpe": _f(s.get("sharpe")),
        "sortino": _f(s.get("sortino")),
        "cagr_pct": 100.0 * _f(s.get("cagr")),
        "trades_per_day": _f(s.get("trades_per_day")),
        "best_pair": (s.get("best_pair") or {}).get("key"),
        "worst_pair": (s.get("worst_pair") or {}).get("key"),
        "market_change_pct": 100.0 * _f(s.get("market_change")),
        "avg_stake": _f(s.get("avg_stake_amount")),
        "total_volume": _f(s.get("total_volume")),
    }

    # ------------------------------------------------------ equity & drawdown
    if wallet_df is not None and len(wallet_df):
        w = wallet_df.copy()
        if "date" not in w.columns and "index" in w.columns:
            w = w.rename(columns={"index": "date"})
        w["date"] = pd.to_datetime(w["date"], utc=True)
        if "rate" in w.columns and "balance" in w.columns:
            w["value"] = w["rate"].astype(float) * w["balance"].astype(float)
        else:
            w["value"] = w.get("total", w.get("total_quote", 0)).astype(float)
        w = w.sort_values("date")
        step = max(1, len(w) // 1500)
        w = w.iloc[::step]
        dash["equity_curve"] = [
            {"t": int(r.date.timestamp() * 1000), "v": round(float(r.value), 2)}
            for r in w.itertuples()
        ]
        values = w["value"].to_numpy(dtype=float)
        peak = np.maximum.accumulate(values)
        dd_series = np.where(peak > 0, (peak - values) / peak, 0.0)
        dash["drawdown"] = [
            {"t": int(r.date.timestamp() * 1000), "v": round(-100.0 * d, 3)}
            for r, d in zip(w.itertuples(), dd_series)
        ]

    # ------------------------------------------------------------- daily
    for row in s.get("daily_profit") or []:
        if isinstance(row, dict):
            dash["daily"].append({"date": row.get("date"), "profit_abs": round(_f(row.get("profit_abs")), 2)})
        elif isinstance(row, (list, tuple)) and len(row) >= 2:
            dash["daily"].append({"date": row[0], "profit_abs": round(_f(row[1]), 2)})

    # ------------------------------------------------------------ per pair
    for row in s.get("results_per_pair", []) or []:
        # freqtrade appends an aggregate 'TOTAL' row; it is not a pair
        if str(row.get("key", "")).upper() == "TOTAL" or "/" not in str(row.get("key", "")):
            continue
        dash["per_pair"].append({
            "pair": row.get("key"),
            "trades": int(row.get("trades") or 0),
            "profit_abs": _f(row.get("profit_total_abs")),
            "profit_pct": 100.0 * _f(row.get("profit_total")),
            "win_rate_pct": 100.0 * _f(row.get("winrate")),
            "avg_trade_pct": _f(row.get("profit_mean_pct")),
            "avg_duration_s": row.get("duration_avg"),
            "max_drawdown_pct": 100.0 * _f(row.get("max_drawdown_account")),
            "sharpe": _f(row.get("sharpe")),
            "profit_factor": _f(row.get("profit_factor")),
        })

    # ----------------------------------------------------------- exit reasons
    for row in s.get("exit_reason_summary", []) or []:
        dash["exit_reasons"].append({
            "reason": row.get("key"),
            "trades": int(row.get("trades") or 0),
            "profit_abs": _f(row.get("profit_total_abs")),
            "profit_pct": 100.0 * _f(row.get("profit_total")),
            "avg_duration_s": row.get("duration_avg"),
        })

    # ---------------------------------------------------------- periodic
    periodic = s.get("periodic_breakdown") or {}

    def _period(rows: list) -> list:
        out = []
        for row in rows or []:
            out.append({
                "period": str(row.get("date", "")),
                "trades": int(row.get("trades") or 0),
                "wins": int(row.get("wins") or 0),
                "losses": int(row.get("losses") or 0),
                "profit_abs": round(_f(row.get("profit_abs")), 2),
                "profit_factor": _f(row.get("profit_factor")),
            })
        return out

    dash["monthly"] = _period(periodic.get("month"))
    dash["yearly"] = _period(periodic.get("year"))

    # ------------------------------------------------------------- buy & hold
    bh_per_pair: dict[str, float] = {}
    if datadir is not None:
        bh_per_pair = _buy_hold_from_data(
            datadir, (config.get("exchange") or {}).get("name") or "nobitex",
            pairs, s.get("timeframe") or "5m", start_iso, end_iso,
        )
    avg_bh = round(float(np.mean(list(bh_per_pair.values()))), 3) if bh_per_pair else None
    dash["buy_hold"] = {
        "strategy_return_pct": dash["cards"]["return_pct"],
        "market_change_pct": dash["cards"]["market_change_pct"],
        "equal_weight_buy_hold_pct": avg_bh,
        "per_pair": bh_per_pair,
    }

    # keep a sample of trades for drill-down (can be large)
    trades = s.get("trades") or []
    dash["trades_sample"] = [
        {
            "pair": t.get("pair"),
            "side": t.get("side"),
            "open_date": t.get("open_date"),
            "close_date": t.get("close_date"),
            "profit_pct": round(100.0 * _f(t.get("profit_ratio")), 4),
            "profit_abs": round(_f(t.get("profit_abs")), 4),
            "exit_reason": t.get("exit_reason"),
            "enter_tag": t.get("enter_tag"),
        }
        for t in trades[:500]
    ]
    return dash


def save_dashboard(dash: dict, results_dir: Path) -> Path:
    results_dir.mkdir(parents=True, exist_ok=True)
    src = Path(dash["source"])
    path = results_dir / (src.stem + ".dashboard.json")
    path.write_text(json.dumps(dash, indent=1, default=str), encoding="utf-8")
    return path


def load_latest_dashboard(exportdir: Path, results_dir: Path,
                          datadir: Optional[Path] = None) -> Optional[dict]:
    from .backtest import latest_results_zip

    zip_path = latest_results_zip(exportdir)
    if zip_path is None:
        return None
    dash_path = results_dir / (zip_path.stem + ".dashboard.json")
    if dash_path.is_file():
        try:
            return json.loads(dash_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            pass
    dash = parse_backtest_zip(zip_path, datadir=datadir)
    save_dashboard(dash, results_dir)
    return dash
