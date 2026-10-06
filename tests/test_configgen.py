"""Unit tests: Freqtrade config generation (never touches the master config)."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from nobitex_adapter.configgen import (
    btc_informative_pair,
    build_backtest_config,
    detect_strategy_timeframes,
    required_timeframes,
    timerange_str,
    write_backtest_config,
)


def _dt(s: str) -> datetime:
    return datetime.strptime(s, "%Y-%m-%d %H:%M").replace(tzinfo=timezone.utc)


def test_timerange_date_only_midnight():
    assert timerange_str(_dt("2024-01-01 00:00"), _dt("2024-03-31 00:00")) == "20240101-20240331"


def test_timerange_with_time():
    assert timerange_str(_dt("2024-01-01 05:30"), _dt("2024-03-31 12:45")) == \
        "20240101-0530-20240331-1245"


def test_timerange_rejects_invalid_hour_form():
    out = timerange_str(_dt("2024-01-01 00:00"), _dt("2024-03-31 00:00"))
    assert "-" not in out.split("-")[0]  # no HHMM on the start side


def test_detect_strategy_timeframes_on_real_x8(tmp_path):
    (tmp_path / "S.py").write_text(
        'class S:\n    timeframe = "5m"\n'
        '    info_timeframes = ["15m", "1h", "4h", "1d"]\n'
        '    btc_info_timeframes = ["4h"]\n',
        encoding="utf-8",
    )
    det = detect_strategy_timeframes(tmp_path / "S.py")
    assert det == {"timeframe": "5m", "info_timeframes": ["15m", "1h", "4h", "1d"],
                   "btc_info_timeframes": ["4h"]}


def test_detect_strategy_timeframes_missing_attrs(tmp_path):
    (tmp_path / "S.py").write_text("class S: pass\n", encoding="utf-8")
    det = detect_strategy_timeframes(tmp_path / "S.py")
    assert det["timeframe"] is None
    assert det["info_timeframes"] == []


def test_detect_strategy_timeframes_nonexistent(tmp_path):
    det = detect_strategy_timeframes(tmp_path / "nope.py")
    assert det["timeframe"] is None


def test_btc_informative_pair():
    assert btc_informative_pair("USDT") == "BTC/USDT"
    assert btc_informative_pair("usdt") == "BTC/USDT"
    assert btc_informative_pair("USDC") == "BTC/USDC"
    assert btc_informative_pair("EUR") == "BTC/EUR"
    assert btc_informative_pair("TRY") == "BTC/TRY"
    assert btc_informative_pair("PLN") == "BTC/USDT"  # non-stable -> USDT
    # futures suffix (future-proofing; spot path unaffected)
    assert btc_informative_pair("USDT", is_futures=True) == "BTC/USDT:USDT"


def test_required_timeframes_sorted_and_complete():
    tfs = required_timeframes("5m", ["15m", "1h", "4h", "1d"], ["4h"])
    assert tfs == ["5m", "15m", "1h", "4h", "1d"]


def _build(tmp_path: Path) -> dict:
    return build_backtest_config(
        strategy="NostalgiaForInfinityX8",
        pairs=["BTC/USDT", "ETH/USDT"],
        base_timeframe="5m",
        start=_dt("2024-01-01 00:00"),
        end=_dt("2024-03-31 00:00"),
        user_data_dir=tmp_path / "user_data",
        datadir=tmp_path / "user_data" / "data",
        strategies_dir=tmp_path / "user_data" / "strategies",
    )


def test_config_core_fields(tmp_path):
    cfg = _build(tmp_path)
    assert cfg["strategy"] == "NostalgiaForInfinityX8"
    assert cfg["timeframe"] == "5m"
    assert cfg["max_open_trades"] == 8
    assert cfg["stake_currency"] == "USDT"
    assert cfg["stake_amount"] == "unlimited"
    assert cfg["initial_capital"] == 10_000.0
    assert cfg["dry_run_wallet"] == 10_000.0
    assert cfg["trading_mode"] == "spot"
    assert cfg["dry_run"] is True
    assert cfg["exchange"]["name"] == "nobitex"
    assert cfg["exchange"]["pair_whitelist"] == ["BTC/USDT", "ETH/USDT"]
    assert cfg["exchange"]["key"] == "" and cfg["exchange"]["secret"] == ""
    assert cfg["pairlists"] == [{"method": "StaticPairList"}]
    assert cfg["dataformat"] == "feather"
    assert cfg["timerange"] == "20240101-20240331"
    assert cfg["tradingFee"] == 0.002


def test_config_never_includes_api_server(tmp_path):
    """The REST API server must stay disabled in generated configs."""
    cfg = _build(tmp_path)
    assert "api_server" not in cfg
    assert "api_server" not in json.dumps(cfg)


def test_config_entry_exit_pricing_present(tmp_path):
    """Freqtrade validate_config reads these keys directly."""
    cfg = _build(tmp_path)
    assert cfg["entry_pricing"] == {"price_side": "same", "use_order_book": False, "order_book_top": 1}
    assert cfg["exit_pricing"] == {"price_side": "same", "use_order_book": False, "order_book_top": 1}


def test_config_block_bad_experiments_flag(tmp_path):
    """Market-data-only exchange: must not be rejected by Freqtrade's gate."""
    cfg = _build(tmp_path)
    assert cfg["experimental"]["block_bad_exchanges"] is False


def test_config_blacklist_and_custom_fee(tmp_path):
    cfg = build_backtest_config(
        strategy="S", pairs=["BTC/USDT"], base_timeframe="5m",
        start=_dt("2024-01-01 00:00"), end=_dt("2024-02-01 00:00"),
        user_data_dir=tmp_path, datadir=tmp_path, strategies_dir=tmp_path,
        fee=0.001, blacklist=["WBTC/USDT", "WETH/USDT"],
    )
    assert cfg["tradingFee"] == 0.001
    assert set(cfg["exchange"]["pair_blacklist"]) == {"WBTC/USDT", "WETH/USDT"}


def test_config_fee_none_omits_tradingfee(tmp_path):
    cfg = build_backtest_config(
        strategy="S", pairs=["BTC/USDT"], base_timeframe="5m",
        start=_dt("2024-01-01 00:00"), end=_dt("2024-02-01 00:00"),
        user_data_dir=tmp_path, datadir=tmp_path, strategies_dir=tmp_path,
        fee=None,
    )
    assert "tradingFee" not in cfg


def test_config_advanced_deep_merge(tmp_path):
    cfg = build_backtest_config(
        strategy="S", pairs=["BTC/USDT"], base_timeframe="5m",
        start=_dt("2024-01-01 00:00"), end=_dt("2024-02-01 00:00"),
        user_data_dir=tmp_path, datadir=tmp_path, strategies_dir=tmp_path,
        advanced={"experimental": {"block_bad_exchanges": False, "extra": 1},
                  "new_section": {"a": 1}},
    )
    assert cfg["experimental"]["extra"] == 1
    assert cfg["experimental"]["block_bad_exchanges"] is False  # not clobbered
    assert cfg["new_section"] == {"a": 1}


def test_write_backtest_config_unique_files(tmp_path):
    cfg = _build(tmp_path)
    p1 = write_backtest_config(cfg, tmp_path / "configs", tag="run1")
    p2 = write_backtest_config(cfg, tmp_path / "configs", tag="run1")
    assert p1.is_file() and p2.is_file()
    assert p1 != p2  # timestamped + random suffix -> never overwrites
    assert json.loads(p1.read_text(encoding="utf-8"))["strategy"] == "NostalgiaForInfinityX8"
