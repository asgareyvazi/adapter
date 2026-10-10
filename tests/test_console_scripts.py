"""Unit tests: installed console-script entry points (pyproject [project.scripts]).

Regression coverage for the reported defect where script targets pointed at
cmd_* (which require an `args` namespace) and raised TypeError on launch.
"""
from __future__ import annotations

import importlib
import inspect
import re
from pathlib import Path

import pytest

from nobitex_adapter import cli

pytestmark = pytest.mark.unit

EXPECTED_SCRIPTS = {
    "nobitex-markets": "markets_main",
    "nobitex-download": "download_main",
    "nobitex-validate": "validate_main",
    "nobitex-backtest": "backtest_main",
    "nobitex-ui": "ui_main",
    "nobitex-mock": "mock_main",
    "nobitex-probe": "probe_main",
    "nobitex-depth": "depth_main",
    "nobitex-doctor": "doctor_main",
    "nobitex-compare": "compare_main",
}


def _script_targets() -> dict[str, str]:
    text = (Path(__file__).resolve().parent.parent / "pyproject.toml").read_text(
        encoding="utf-8"
    )
    body = text.split("[project.scripts]", 1)[1].split("[", 1)[0]
    out: dict[str, str] = {}
    for m in re.finditer(r'^([\w-]+)\s*=\s*"([^"]+)"', body, re.M):
        out[m.group(1)] = m.group(2)
    return out


def test_all_declared_scripts_resolve_to_zero_arg_callables():
    targets = _script_targets()
    assert set(targets) == set(EXPECTED_SCRIPTS), targets
    for name, dotted in targets.items():
        mod_name, _, attr = dotted.partition(":")
        assert attr == EXPECTED_SCRIPTS[name]
        fn = getattr(importlib.import_module(mod_name), attr)
        assert callable(fn), name
        # console_scripts invoke target() with no arguments: every parameter
        # must therefore have a default (regression: cmd_* took `args`)
        for p in inspect.signature(fn).parameters.values():
            assert p.default is not inspect.Parameter.empty, (name, p.name)


def _capture_main(monkeypatch):
    seen: dict = {}

    def fake_main(argv=None):
        seen["argv"] = argv
        return 7

    monkeypatch.setattr(cli, "main", fake_main)
    return seen


@pytest.mark.parametrize(
    "wrapper,command",
    [(getattr(cli, f), c) for f, c in [
        ("markets_main", "markets"), ("download_main", "download"),
        ("validate_main", "validate"), ("backtest_main", "backtest"),
        ("ui_main", "ui"), ("mock_main", "mock"), ("probe_main", "probe"),
        ("depth_main", "depth"), ("doctor_main", "doctor"),
        ("compare_main", "compare"),
    ]],
)
def test_wrappers_route_to_main_with_command(monkeypatch, wrapper, command):
    seen = _capture_main(monkeypatch)
    assert wrapper([]) == 7
    assert seen["argv"] == [command]


def test_global_opts_hoisted_before_command(monkeypatch):
    seen = _capture_main(monkeypatch)
    cli.download_main(["--pairs", "BTC/USDT", "--repo", "/r", "--timeframes", "5m",
                       "--start", "2024-01-01", "-v"])
    assert seen["argv"] == ["--repo", "/r", "-v", "download", "--pairs", "BTC/USDT",
                            "--timeframes", "5m", "--start", "2024-01-01"]


def test_equals_forms_and_exchange_hoisted(monkeypatch):
    seen = _capture_main(monkeypatch)
    cli.backtest_main(["--exchange=azbit", "--strategy", "S", "--repo=/r e p o"])
    assert seen["argv"] == ["--exchange=azbit", "--repo=/r e p o", "backtest",
                            "--strategy", "S"]


def test_help_and_unknown_flags_not_hoisted(monkeypatch):
    seen = _capture_main(monkeypatch)
    cli.download_main(["--help"])
    assert seen["argv"] == ["download", "--help"]  # subcommand help, not top-level
    cli.download_main(["--bogus", "x"])
    assert seen["argv"] == ["download", "--bogus", "x"]  # argparse reports it


def test_double_dash_stops_hoisting(monkeypatch):
    seen = _capture_main(monkeypatch)
    cli.download_main(["--", "--repo", "X"])
    assert seen["argv"] == ["download", "--", "--repo", "X"]


def test_missing_global_value_left_for_argparse(monkeypatch):
    seen = _capture_main(monkeypatch)
    cli.download_main(["--repo"])
    assert seen["argv"] == ["download", "--repo"]


def test_wrapper_version_exits_zero(capsys):
    with pytest.raises(SystemExit) as exc:
        cli.doctor_main(["--version"])
    assert exc.value.code == 0
    assert "0.1.0" in capsys.readouterr().out


def test_wrapper_help_exits_zero(capsys):
    with pytest.raises(SystemExit) as exc:
        cli.markets_main(["--help"])
    assert exc.value.code == 0
    assert "usage" in capsys.readouterr().out


def test_wrapper_argparse_error_exits_two():
    with pytest.raises(SystemExit) as exc:
        cli.download_main([])  # missing required --pairs/--timeframes/--start
    assert exc.value.code == 2
