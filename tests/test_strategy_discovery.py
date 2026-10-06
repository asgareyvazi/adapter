"""Tests for deterministic strategy discovery (selected repo > bundled copy)."""

from __future__ import annotations

from pathlib import Path

import pytest

from nobitex_adapter.backtest import (
    BacktestError,
    bundled_strategies_dir,
    find_strategy,
)

HERE = Path(__file__).resolve().parent
ADAPTER_ROOT = HERE.parent


def _write_strategy(path: Path, name: str = "NostalgiaForInfinityX8") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f'class {name}:\n    timeframe = "5m"\n', encoding="utf-8"
    )
    return path


def test_find_flat_strategy(tmp_path):
    strategies = tmp_path / "strategies"
    f = _write_strategy(strategies / "NostalgiaForInfinityX8.py")
    path, note = find_strategy(strategies, "NostalgiaForInfinityX8")
    assert path == f
    assert note == ""


def test_find_nested_strategy_repo_layout(tmp_path):
    """Real-world layout: NFI cloned under user_data/strategies/."""
    strategies = tmp_path / "strategies"
    f = _write_strategy(
        strategies / "NostalgiaForInfinity" / "NostalgiaForInfinityX8.py"
    )
    path, note = find_strategy(strategies, "NostalgiaForInfinityX8")
    assert path == f
    assert note == ""


def test_flat_wins_over_nested_deterministically(tmp_path):
    strategies = tmp_path / "strategies"
    flat = _write_strategy(strategies / "NostalgiaForInfinityX8.py")
    _write_strategy(strategies / "NostalgiaForInfinity" / "NostalgiaForInfinityX8.py")
    path, note = find_strategy(strategies, "NostalgiaForInfinityX8")
    assert path == flat  # shallowest-first
    assert note == ""


def test_shallowest_wins_among_nested(tmp_path):
    strategies = tmp_path / "strategies"
    a = _write_strategy(strategies / "nfi" / "MyStrat.py", "MyStrat")
    _write_strategy(strategies / "nfi" / "deep" / "MyStrat.py", "MyStrat")
    path, _ = find_strategy(strategies, "MyStrat")
    assert path == a


def test_ignores_pycache_and_hidden(tmp_path):
    strategies = tmp_path / "strategies"
    _write_strategy(strategies / "__pycache__" / "NostalgiaForInfinityX8.py")
    _write_strategy(strategies / ".git" / "NostalgiaForInfinityX8.py")
    strategies.mkdir(parents=True, exist_ok=True)
    real = _write_strategy(strategies / "NostalgiaForInfinityX8.py")
    path, _ = find_strategy(strategies, "NostalgiaForInfinityX8")
    assert path == real


def test_bundled_fallback_with_note(tmp_path):
    empty = tmp_path / "strategies"
    empty.mkdir()
    bundled = bundled_strategies_dir()
    assert (bundled / "NostalgiaForInfinityX8.py").is_file(), \
        "adapter checkout must bundle the X8 strategy"
    path, note = find_strategy(empty, "NostalgiaForInfinityX8")
    assert path == bundled / "NostalgiaForInfinityX8.py"
    assert "bundled" in note


def test_missing_strategy_raises(tmp_path):
    empty = tmp_path / "strategies"
    empty.mkdir()
    with pytest.raises(BacktestError, match="not found"):
        find_strategy(empty, "DoesNotExist")


def test_bundled_dir_is_adapter_checkout():
    bundled = bundled_strategies_dir()
    assert bundled == ADAPTER_ROOT / "user_data" / "strategies"
