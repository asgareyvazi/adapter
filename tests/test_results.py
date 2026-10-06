"""Unit tests: results-zip parsing into the dashboard payload.

Builds a synthetic zip matching Freqtrade 2026.x output (strategy stats JSON,
config JSON, wallet feather) and checks every dashboard section.
"""
from __future__ import annotations

import io
import json
import zipfile
from pathlib import Path

import pandas as pd
import pytest

from nobitex_adapter.results import (
    load_latest_dashboard,
    parse_backtest_zip,
    save_dashboard,
)

STEM = "backtest-result-20240101-20240201"

STATS = {
    "strategy": {
        "TestStrategy": {
            "timeframe": "5m",
            "backtest_start": "2024-01-01 00:00:00",
            "backtest_end": "2024-01-31 23:55:00",
            "backtest_days": 30,
            "pairlist": ["BTC/USDT", "ETH/USDT"],
            "starting_balance": 10_000.0,
            "final_balance": 10_500.0,
            "profit_total": 0.05,
            "profit_total_abs": 500.0,
            "total_trades": 10,
            "winrate": 0.8,
            "profit_factor": 2.5,
            "sharpe": 1.5,
            "sortino": 1.8,
            "cagr": 0.2,
            "trades_per_day": 0.33,
            "market_change": 0.3,
            "avg_stake_amount": 1250.0,
            "total_volume": 50_000.0,
            "max_drawdown_account": 0.02,
            "wallet_stats": {"max_drawdown_account": 0.021},
            "best_pair": {"key": "BTC/USDT", "profit_abs": 300.0},
            "worst_pair": {"key": "ETH/USDT", "profit_abs": 200.0},
            "daily_profit": [
                ["2024-01-05", 10.5],
                ["2024-01-06", -2.25],
            ],
            "results_per_pair": [
                {
                    "key": "BTC/USDT", "trades": 6, "profit_total_abs": 300.0,
                    "profit_total": 0.03, "winrate": 0.833, "profit_mean_pct": 1.2,
                    "duration_avg": "1:05:00", "max_drawdown_account": 0.01,
                    "sharpe": 1.1, "profit_factor": 2.0,
                },
                {
                    "key": "ETH/USDT", "trades": 4, "profit_total_abs": 200.0,
                    "profit_total": 0.02, "winrate": 0.75, "profit_mean_pct": 0.8,
                    "duration_avg": "2:10:00", "max_drawdown_account": 0.015,
                    "sharpe": 0.9, "profit_factor": 1.8,
                },
            ],
            "exit_reason_summary": [
                {"key": "roi", "trades": 7, "profit_total_abs": 400.0,
                 "profit_total": 0.04, "duration_avg": "1:00:00"},
                {"key": "stop_loss", "trades": 3, "profit_total_abs": -20.0,
                 "profit_total": -0.002, "duration_avg": "0:45:00"},
            ],
            "periodic_breakdown": {
                "day": [], "week": [],
                "month": [
                    {"date": "01/31/2024", "date_ts": 1706659200, "trades": 10,
                     "wins": 8, "losses": 2, "profit_abs": 500.0, "profit_factor": 2.5},
                ],
                "year": [], "weekday": [],
            },
            "trades": [
                {
                    "pair": "BTC/USDT", "side": "long",
                    "open_date": "2024-01-02 00:00:00", "close_date": "2024-01-02 03:00:00",
                    "profit_ratio": 0.012, "profit_abs": 12.0,
                    "exit_reason": "roi", "enter_tag": "tag_a",
                }
            ],
        }
    }
}

CONFIG = {
    "exchange": {"name": "nobitex"},
    "stake_currency": "USDT",
    "timerange": "20240101-20240201",
}


def _wallet_df() -> pd.DataFrame:
    dates = pd.date_range("2024-01-01 00:00:00", periods=10, freq="5min", tz="UTC")
    balance = [10_000 + i * 50 for i in range(10)]
    return pd.DataFrame({
        "index": list(range(10)),
        "date": dates,
        "currency": "USDT",
        "rate": [1.0] * 10,
        "balance": balance,
        "total_quote": balance,
    })


def _make_zip(tmp_path: Path) -> Path:
    zip_path = tmp_path / f"{STEM}.zip"
    with zipfile.ZipFile(zip_path, "w") as zf:
        zf.writestr(f"{STEM}.json", json.dumps(STATS))
        zf.writestr(f"{STEM}_config.json", json.dumps(CONFIG))
        zf.writestr(f"{STEM}_TestStrategy.py", "class TestStrategy: pass")
        buf = io.BytesIO()
        _wallet_df().to_feather(buf)
        zf.writestr(f"{STEM}_TestStrategy_wallet.feather", buf.getvalue())
    return zip_path


def test_parse_cards(tmp_path):
    dash = parse_backtest_zip(_make_zip(tmp_path))
    c = dash["cards"]
    assert c["total_profit_abs"] == 500.0
    assert c["return_pct"] == 5.0
    assert c["trades"] == 10
    assert c["win_rate_pct"] == 80.0
    assert c["profit_factor"] == 2.5
    assert c["sharpe"] == 1.5
    assert c["sortino"] == 1.8
    assert c["cagr_pct"] == 20.0
    assert c["max_drawdown_pct"] == pytest.approx(2.1)  # wallet_stats wins
    assert c["best_pair"] == "BTC/USDT"
    assert c["worst_pair"] == "ETH/USDT"
    assert c["market_change_pct"] == 30.0


def test_parse_top_level(tmp_path):
    dash = parse_backtest_zip(_make_zip(tmp_path))
    assert dash["strategy"] == "TestStrategy"
    assert dash["backtest_start"] == "2024-01-01 00:00:00"
    assert dash["backtest_end"] == "2024-01-31 23:55:00"
    assert dash["timeframe"] == "5m"
    assert dash["pairlist"] == ["BTC/USDT", "ETH/USDT"]
    assert dash["starting_balance"] == 10_000.0
    assert dash["final_balance"] == 10_500.0
    assert dash["config"]["exchange"] == "nobitex"
    assert dash["config"]["timerange"] == "20240101-20240201"


def test_parse_equity_and_drawdown(tmp_path):
    dash = parse_backtest_zip(_make_zip(tmp_path))
    assert len(dash["equity_curve"]) == 10
    eq = dash["equity_curve"]
    assert eq[0]["v"] == 10_000.0  # rate * balance
    assert eq[9]["v"] == 10_450.0
    # monotonic increasing here => no drawdown
    assert all(d["v"] >= 0 for d in dash["drawdown"])


def test_parse_daily_profit_pairs(tmp_path):
    dash = parse_backtest_zip(_make_zip(tmp_path))
    assert dash["daily"] == [
        {"date": "2024-01-05", "profit_abs": 10.5},
        {"date": "2024-01-06", "profit_abs": -2.25},
    ]


def test_parse_per_pair_and_exit_reasons(tmp_path):
    dash = parse_backtest_zip(_make_zip(tmp_path))
    assert len(dash["per_pair"]) == 2
    btc = dash["per_pair"][0]
    assert btc["pair"] == "BTC/USDT"
    assert btc["trades"] == 6
    assert btc["profit_pct"] == pytest.approx(3.0)
    assert btc["avg_trade_pct"] == 1.2
    assert btc["max_drawdown_pct"] == pytest.approx(1.0)
    assert len(dash["exit_reasons"]) == 2
    assert dash["exit_reasons"][0]["reason"] == "roi"


def test_parse_periodic(tmp_path):
    dash = parse_backtest_zip(_make_zip(tmp_path))
    assert len(dash["monthly"]) == 1
    m = dash["monthly"][0]
    assert m["period"] == "01/31/2024"
    assert m["trades"] == 10
    assert m["wins"] == 8
    assert m["losses"] == 2
    assert m["profit_abs"] == 500.0
    assert dash["yearly"] == []


def test_parse_trades_sample(tmp_path):
    dash = parse_backtest_zip(_make_zip(tmp_path))
    assert len(dash["trades_sample"]) == 1
    t = dash["trades_sample"][0]
    assert t["pair"] == "BTC/USDT"
    assert t["profit_pct"] == pytest.approx(1.2)
    assert t["exit_reason"] == "roi"


def test_buy_hold_from_data(tmp_path):
    """With datadir, per-pair buy&hold is computed from the stored feathers."""
    zip_path = _make_zip(tmp_path)
    # BTC +10% over the window, ETH -5% (data must cover the backtest window)
    dd = tmp_path / "data" / "nobitex"
    dd.mkdir(parents=True)
    n = 11_000
    dates = pd.date_range("2023-12-15 00:00:00", periods=n, freq="5min", tz="UTC")
    for pair, first, last in (("BTC_USDT-5m.feather", 100.0, 110.0),
                              ("ETH_USDT-5m.feather", 200.0, 190.0)):
        import numpy as np
        close = np.linspace(first, last, n)
        df = pd.DataFrame({
            "date": dates,
            "open": close, "high": close * 1.01,
            "low": close * 0.99, "close": close, "volume": [1.0] * n,
        })
        df.to_feather(dd / pair)

    dash = parse_backtest_zip(zip_path, datadir=tmp_path / "data")
    bh = dash["buy_hold"]
    assert set(bh["per_pair"]) == {"BTC/USDT", "ETH/USDT"}
    assert bh["per_pair"]["BTC/USDT"] > 0
    assert bh["per_pair"]["ETH/USDT"] < 0
    assert bh["equal_weight_buy_hold_pct"] is not None
    assert bh["strategy_return_pct"] == 5.0


def test_save_and_load_dashboard(tmp_path):
    zip_path = _make_zip(tmp_path)
    dash = parse_backtest_zip(zip_path)
    results_dir = tmp_path / "results"
    results_dir.mkdir()
    p = save_dashboard(dash, results_dir)
    assert p.is_file()
    assert p.name == f"{STEM}.dashboard.json"
    loaded = load_latest_dashboard(tmp_path, results_dir)
    assert loaded is not None
    assert loaded["strategy"] == "TestStrategy"


def test_missing_file_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        parse_backtest_zip(tmp_path / "nope.zip")
