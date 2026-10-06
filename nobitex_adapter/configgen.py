"""Generated run-configuration management.

The UI/CLI NEVER overwrites the user's master config. Every backtest gets its
own generated config under ``user_data/nobitex_gui/configs/``.

Secrets policy: generated configs contain NO secrets. (Milestone 1 does not
need any; a future dry-run milestone will read credentials from environment
variables or a local ``*.secret.json`` that is git-ignored.)
"""
from __future__ import annotations

import importlib.util
import json
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from .timeframes import X8_BASE_TIMEFRAME

# Pairs that must never be backtested (leveraged / wrapped tokens that
# distort strategy logic). Nobitex currently lists none of these, but the
# filter keeps future listings safe.
DEFAULT_BLACKLIST_PATTERNS: list[str] = [
    r".*BULL$",
    r".*BEAR$",
    r".*UP$",
    r".*DOWN$",
    r"WBTC/.*",
    r"WETH/.*",
]


def timerange_str(start: datetime, end: datetime) -> str:
    """Freqtrade timerange: 'YYYYMMDD' or 'YYYYMMDD-HHMM' (UTC)."""
    s = start.astimezone(timezone.utc)
    e = end.astimezone(timezone.utc)
    if (s.hour, s.minute) == (0, 0):
        return f"{s.strftime('%Y%m%d')}-{e.strftime('%Y%m%d')}"
    return f"{s.strftime('%Y%m%d-%H%M')}-{e.strftime('%Y%m%d-%H%M')}"


def detect_strategy_timeframes(strategy_file: Path) -> dict:
    """Read timeframe requirements straight from the strategy file source.

    Looks for class attributes: ``timeframe``, ``info_timeframes``,
    ``btc_info_timeframes``. No strategy code is executed; if the attributes
    are absent, empty results are returned (caller falls back to base TF).
    """
    out = {"timeframe": None, "info_timeframes": [], "btc_info_timeframes": [],
           "startup_candle_count": None}
    try:
        src = strategy_file.read_text(encoding="utf-8")
    except OSError:
        return out
    m = re.search(r"^\s*timeframe\s*=\s*[\"']([^\"']+)[\"']", src, re.M)
    if m:
        out["timeframe"] = m.group(1)
    m = re.search(r"^\s*info_timeframes\s*=\s*\[([^\]]*)\]", src, re.M)
    if m:
        out["info_timeframes"] = re.findall(r"[\"']([^\"']+)[\"']", m.group(1))
    m = re.search(r"^\s*btc_info_timeframes\s*=\s*\[([^\]]*)\]", src, re.M)
    if m:
        out["btc_info_timeframes"] = re.findall(r"[\"']([^\"']+)[\"']", m.group(1))
    # X8 declares it annotated: `startup_candle_count: int = 800`
    m = re.search(r"^\s*startup_candle_count\s*(?::\s*[\w\[\]]+\s*)?=\s*(\d+)",
                  src, re.M)
    if m:
        out["startup_candle_count"] = int(m.group(1))
    return out


def btc_informative_pair(stake_currency: str, is_futures: bool = False) -> str:
    stables = {"USDT", "BUSD", "USDC", "DAI", "TUSD", "FDUSD", "PAX", "USD", "EUR", "GBP", "TRY"}
    if stake_currency.upper() in stables:
        pair = f"BTC/{stake_currency.upper()}"
    else:
        pair = "BTC/USDT"
    if is_futures:
        pair += f":{stake_currency.upper()}"
    return pair


def required_timeframes(
    base_tf: str,
    info_tfs: list[str],
    btc_info_tfs: list[str],
    stake_currency: str = "USDT",
) -> list[str]:
    from .timeframes import parse_timeframe

    tfs = {base_tf, *info_tfs}
    if btc_info_tfs:
        tfs |= set(btc_info_tfs)
    return sorted(tfs, key=lambda t: parse_timeframe(t).seconds)


def build_backtest_config(
    *,
    strategy: str,
    pairs: list[str],
    base_timeframe: str,
    start: datetime,
    end: datetime,
    user_data_dir: Path,
    datadir: Path,
    strategies_dir: Path,
    stake_currency: str = "USDT",
    initial_capital: float = 10_000.0,
    stake_amount: object = "unlimited",
    max_open_trades: int = 8,
    fee: Optional[float] = 0.002,
    blacklist: Optional[list[str]] = None,
    advanced: Optional[dict] = None,
) -> dict:
    """Build the full Freqtrade backtest configuration dict."""
    config: dict = {
        "strategy": strategy,
        "strategy_path": str(strategies_dir),
        "timeframe": base_timeframe,
        "max_open_trades": int(max_open_trades),
        "stake_currency": stake_currency.upper(),
        "stake_amount": stake_amount,
        "dry_run": True,
        "trading_mode": "spot",
        "margin_mode": None,
        "initial_capital": float(initial_capital),
        "dry_run_wallet": float(initial_capital),
        "exchange": {
            "name": "nobitex",
            "key": "",
            "secret": "",
            "pair_whitelist": list(pairs),
            "pair_blacklist": list(blacklist or []),
        },
        "pairlists": [{"method": "StaticPairList"}],
        # Local (file) data: price fills come from candles, not the live order
        # book. Ticker-based pricing passes Freqtrade's exchange validation
        # (Nobitex exposes fetchTicker; no order book needed in backtests).
        "entry_pricing": {"price_side": "same", "use_order_book": False, "order_book_top": 1},
        "exit_pricing": {"price_side": "same", "use_order_book": False, "order_book_top": 1},
        "dataformat": "feather",
        "datadir": str(datadir),
        "user_data_dir": str(user_data_dir),
        "export": "trades",
        # Milestone 1: Nobitex exposes public market data only. This documented
        # Freqtrade option (experimental.block_bad_exchanges) tells Freqtrade
        # to warn (not fail) about the missing private API, which is exactly
        # correct for a backtest-only integration. Live/dry-run requires the
        # private API and remains blocked in the ccxt class itself.
        "experimental": {"block_bad_exchanges": False},
        # No api_server section at all: the REST API server stays disabled.
        "bot_name": "nobitex-adapter-bt",
        "internals": {"process_throttle_secs": 1},
        "timerange": timerange_str(start, end),
    }
    if fee is not None:
        config["tradingFee"] = float(fee)
    if advanced:
        _deep_update(config, advanced)
    return config


def write_backtest_config(config: dict, configs_dir: Path, tag: str = "run") -> Path:
    configs_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    short = uuid.uuid4().hex[:6]
    path = configs_dir / f"nobitex-{tag}-{stamp}-{short}.json"
    path.write_text(json.dumps(config, indent=2), encoding="utf-8")
    return path


def _deep_update(base: dict, extra: dict) -> None:
    for k, v in extra.items():
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            _deep_update(base[k], v)
        else:
            base[k] = v


def default_user_data_layout(repo_root: Path) -> dict:
    """Standard directory layout (all git-ignored except strategies)."""
    ud = repo_root / "user_data"
    gui = ud / "nobitex_gui"
    return {
        "user_data_dir": ud,
        "datadir": ud / "data",
        "strategies_dir": ud / "strategies",
        "configs_dir": gui / "configs",
        "results_dir": gui / "results",
        "jobs_dir": gui / "jobs",
        "logs_dir": gui / "logs",
        "manifests_dir": gui / "manifests",
        "reports_dir": gui / "reports",
    }


def default_repo_root() -> Path:
    """Repo root = parent of the nobitex_adapter package directory."""
    return Path(__file__).resolve().parent.parent
