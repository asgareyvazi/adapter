"""Tests: run registry + cross-run comparison (unit + CLI/GUI integration).

Backtest-result zips here are minimal freqtrade-shape fixtures (stats JSON
+ config JSON); parsing of REAL freqtrade output is covered by the e2e
tests. What is under test: registration, deterministic IDs, the warning
matrix, and the CLI/GUI surface.
"""
from __future__ import annotations

import json
import zipfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from nobitex_adapter.compare import (
    COMPARISON_SCHEMA_VERSION,
    RUN_SCHEMA_VERSION,
    compare_runs,
    config_sha256,
    list_runs,
    load_run,
    make_run_id,
    register_run,
)

pytestmark = pytest.mark.unit


def _spec(**kw):
    base = {
        "strategy": "TinyStrategy",
        "exchange": "nobitex",
        "pairs": ["BTC/USDT"],
        "timeframes": ["5m"],
        "start": "2024-06-01T00:00:00+00:00",
        "end": "2024-06-06T00:00:00+00:00",
        "stake_currency": "USDT",
        "initial_capital": 10000.0,
        "stake_amount": "unlimited",
        "max_open_trades": 8,
        "fee": 0.002,
    }
    base.update(kw)
    return base


def _stats(strategy="TinyStrategy", return_ratio=0.05, trades=10):
    return {
        "strategy": {
            strategy: {
                "backtest_start": "2024-06-01",
                "backtest_end": "2024-06-06",
                "backtest_days": 5,
                "timeframe": "5m",
                "pairlist": ["BTC/USDT"],
                "starting_balance": 10000.0,
                "final_balance": 10000.0 * (1 + return_ratio),
                "total_trades": trades,
                "profit_total": return_ratio,
                "profit_total_abs": 10000.0 * return_ratio,
                "winrate": 0.6,
                "profit_factor": 1.5,
                "sharpe": 1.1,
                "max_drawdown_account": 0.03,
                "market_change": 0.02,
                "results_per_pair": [],
                "exit_reason_summary": [],
            }
        }
    }


def _config(exchange="nobitex"):
    return {
        "exchange": {"name": exchange},
        "stake_currency": "USDT",
        "timerange": "20240601-20240606",
        "strategy_path": "/machine/specific",
        "datadir": "/machine/specific/data",
        "fee": 0.002,
    }


def _zip(path: Path, strategy="TinyStrategy", **stats_kw) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("backtest-result-2024-06-07_00-00-00.json",
                    json.dumps(_stats(strategy, **stats_kw)))
        zf.writestr("backtest-result-2024-06-07_00-00-00_config.json",
                    json.dumps(_config()))
    return path


def _register(results_dir: Path, name="a", *, spec_kw=None, stats_kw=None,
              status="ok", runtime=None, strategy_src="v1") -> dict:
    results_dir.mkdir(parents=True, exist_ok=True)
    strat = results_dir / f"strat-{name}.py"
    strat.write_text(f"# {strategy_src}\n", encoding="utf-8")
    zp = _zip(results_dir / f"bt-{name}.zip", **(stats_kw or {}))
    return register_run(
        results_dir, spec=_spec(**(spec_kw or {})), status=status,
        config=_config(), config_path=str(results_dir / f"c-{name}.json"),
        results_zip=str(zp), strategy_file=str(strat),
        runtime=runtime or {"freqtrade": "2026.9", "adapter_version": "0.1.0"},
        elapsed=3.0, error=None if status == "ok" else "boom",
        stamp=f"2024010{name}",
    )


def _codes(cmp):
    return {w["code"] for w in cmp["warnings"]}


# ------------------------------------------------------------- run registry
def test_schema_versions():
    assert RUN_SCHEMA_VERSION == 1
    assert COMPARISON_SCHEMA_VERSION == 1


def test_make_run_id_deterministic_and_safe(tmp_path):
    a = make_run_id(_spec(), stamp="20240101_000000")
    assert a == make_run_id(_spec(), stamp="20240101_000000")
    assert a != make_run_id(_spec(fee=0.005), stamp="20240101_000000")
    assert a.startswith("TinyStrategy-nobitex-20240101_000000-")
    weird = make_run_id(_spec(strategy="X8 v2 (alt)"), stamp="20240101_000000")
    assert "/" not in weird and " " not in weird and "(" not in weird


def test_config_sha_ignores_machine_paths():
    c1 = _config()
    c2 = _config()
    c2["strategy_path"] = "/elsewhere"
    c2["datadir"] = "/elsewhere/data"
    c2["exportdirectory"] = "/elsewhere/results"
    assert config_sha256(c1) == config_sha256(c2)
    c2["fee"] = 0.005
    assert config_sha256(c1) != config_sha256(c2)


def test_register_run_record_shape(tmp_path):
    rec = _register(tmp_path)
    assert rec["schema"] == 1
    assert rec["status"] == "ok"
    assert rec["spec"]["config_sha256"].startswith("sha256:")
    assert rec["strategy_sha256"].startswith("sha256:")
    assert rec["summary"]["cards"]["return_pct"] == pytest.approx(5.0)
    assert rec["summary"]["trades_count"] == 10
    assert (tmp_path / "runs" / f"{rec['run_id']}.run.json").is_file()
    assert load_run(tmp_path, rec["run_id"])["run_id"] == rec["run_id"]


def test_register_failed_run_keeps_error_no_summary(tmp_path):
    rec = _register(tmp_path, status="failed")
    assert rec["status"] == "failed"
    assert rec["error"] == "boom"
    assert rec["summary"] is None


def test_register_never_raises_on_garbage_zip(tmp_path):
    bad = tmp_path / "bad.zip"
    bad.write_bytes(b"not a zip")
    rec = register_run(tmp_path, spec=_spec(), status="ok", results_zip=str(bad))
    assert rec["summary"] is None
    assert any("parse failed" in n for n in rec["notes"])


def test_list_runs_newest_first_skips_corrupt(tmp_path):
    _register(tmp_path, name="a")
    _register(tmp_path, name="b")
    (tmp_path / "runs" / "junk.run.json").write_text("{oops", encoding="utf-8")
    runs = list_runs(tmp_path)
    assert [r["run_id"] for r in runs] == sorted(
        [r["run_id"] for r in runs], reverse=True)
    assert len(runs) == 2


def test_load_run_unknown_lists_known(tmp_path):
    rec = _register(tmp_path)
    with pytest.raises(ValueError) as exc:
        load_run(tmp_path, "nope")
    assert rec["run_id"] in str(exc.value)


# ------------------------------------------------------------ warning matrix
def test_identical_twins_have_no_warnings(tmp_path):
    a = _register(tmp_path, name="a")
    b = _register(tmp_path, name="b")
    cmp = compare_runs(tmp_path, [a["run_id"], b["run_id"]])
    assert cmp["schema"] == 1
    assert cmp["warnings"] == []
    assert [r["run_id"] for r in cmp["runs"]] == [a["run_id"], b["run_id"]]
    assert cmp["runs"][0]["return_pct"] == pytest.approx(5.0)


def test_single_run_warns(tmp_path):
    a = _register(tmp_path)
    cmp = compare_runs(tmp_path, [a["run_id"]])
    assert _codes(cmp) == {"SINGLE_RUN"}


def test_range_pairs_timeframes_warn_high(tmp_path):
    a = _register(tmp_path, name="a")
    b = _register(tmp_path, name="b", spec_kw={"end": "2024-06-10T00:00:00+00:00"})
    c = _register(tmp_path, name="c", spec_kw={"pairs": ["BTC/USDT", "ETH/USDT"]})
    d = _register(tmp_path, name="d", spec_kw={"timeframes": ["5m", "15m"]})
    assert "RANGE_DIFF" in _codes(compare_runs(tmp_path, [a["run_id"], b["run_id"]]))
    assert "PAIRS_DIFF" in _codes(compare_runs(tmp_path, [a["run_id"], c["run_id"]]))
    got = compare_runs(tmp_path, [a["run_id"], d["run_id"]])
    assert "TIMEFRAMES_DIFF" in _codes(got)


def test_exchange_capital_fee_stake_warn_medium(tmp_path):
    a = _register(tmp_path, name="a")
    b = _register(tmp_path, name="b", spec_kw={"exchange": "azbit"})
    c = _register(tmp_path, name="c", spec_kw={"initial_capital": 5000.0})
    d = _register(tmp_path, name="d", spec_kw={"fee": 0.005})
    e = _register(tmp_path, name="e", spec_kw={"max_open_trades": 3})
    assert "EXCHANGE_DIFF" in _codes(compare_runs(tmp_path, [a["run_id"], b["run_id"]]))
    assert "CAPITAL_DIFF" in _codes(compare_runs(tmp_path, [a["run_id"], c["run_id"]]))
    assert "FEE_DIFF" in _codes(compare_runs(tmp_path, [a["run_id"], d["run_id"]]))
    assert "STAKE_DIFF" in _codes(compare_runs(tmp_path, [a["run_id"], e["run_id"]]))


def test_config_diff_fires_on_advanced_override(tmp_path):
    a = _register(tmp_path, name="a")
    rec_b = _register(tmp_path, name="b")
    # same compared spec, different effective config (advanced override)
    hacked = dict(rec_b["spec"])
    hacked["config_sha256"] = "sha256:" + "0" * 64
    rec_b["spec"] = hacked
    (tmp_path / "runs" / f"{rec_b['run_id']}.run.json").write_text(
        json.dumps(rec_b), encoding="utf-8")
    codes = _codes(compare_runs(tmp_path, [a["run_id"], rec_b["run_id"]]))
    assert codes == {"CONFIG_DIFF"}


def test_data_fingerprint_change_warns(tmp_path):
    a = _register(tmp_path, name="a")
    rec_b = _register(tmp_path, name="b")
    rec_a = load_run(tmp_path, a["run_id"])
    rec_a["data_fingerprints"] = {
        "BTC/USDT 5m": {"sha256": "sha256:" + "2" * 64, "rows": 99,
                        "first_ts": 1, "last_ts": 2}}
    rec_b["data_fingerprints"] = {
        "BTC/USDT 5m": {"sha256": "sha256:" + "1" * 64, "rows": 99,
                        "first_ts": 1, "last_ts": 2}}
    for rec in (rec_a, rec_b):
        (tmp_path / "runs" / f"{rec['run_id']}.run.json").write_text(
            json.dumps(rec), encoding="utf-8")
    got = compare_runs(tmp_path, [a["run_id"], rec_b["run_id"]])
    assert "DATA_FP_DIFF" in _codes(got)


def test_runtime_and_strategy_src_warnings(tmp_path):
    a = _register(tmp_path, name="a")
    b = _register(tmp_path, name="b", runtime={"freqtrade": "2024.10",
                                               "adapter_version": "0.1.0"})
    c = _register(tmp_path, name="c", runtime={"freqtrade": "2026.9",
                                               "adapter_version": "0.2.0"})
    d = _register(tmp_path, name="d", strategy_src="v2-different")
    assert "FREQTRADE_DIFF" in _codes(compare_runs(tmp_path, [a["run_id"], b["run_id"]]))
    assert "ADAPTER_DIFF" in _codes(compare_runs(tmp_path, [a["run_id"], c["run_id"]]))
    assert "STRATEGY_SRC_DIFF" in _codes(compare_runs(tmp_path, [a["run_id"], d["run_id"]]))


def test_failed_run_warns_and_nulls_metrics(tmp_path):
    a = _register(tmp_path, name="a")
    b = _register(tmp_path, name="b", status="failed")
    got = compare_runs(tmp_path, [a["run_id"], b["run_id"]])
    assert "FAILED_RUN" in _codes(got)
    row = next(r for r in got["runs"] if r["run_id"] == b["run_id"])
    assert row["status"] == "failed" and row["return_pct"] is None


def test_compare_rejects_empty_and_unknown(tmp_path):
    _register(tmp_path)
    with pytest.raises(ValueError, match="no runs"):
        compare_runs(tmp_path, [])
    with pytest.raises(ValueError, match="unknown run"):
        compare_runs(tmp_path, ["missing"])


def test_run_ids_deduplicated_order_kept(tmp_path):
    a = _register(tmp_path, name="a")
    b = _register(tmp_path, name="b")
    got = compare_runs(tmp_path, [b["run_id"], a["run_id"], b["run_id"]])
    assert [r["run_id"] for r in got["runs"]] == [b["run_id"], a["run_id"]]


# ------------------------------------------------------- CLI + GUI integration
@pytest.mark.integration
def test_cli_compare_list_and_runs(capsys, tmp_path):
    from nobitex_adapter.cli import main
    from nobitex_adapter.configgen import default_user_data_layout

    results_dir = default_user_data_layout(tmp_path)["results_dir"]
    a = _register(results_dir, name="a")
    b = _register(results_dir, name="b", spec_kw={"fee": 0.005})
    rc = main(["--repo", str(tmp_path), "compare", "--list"])
    out = capsys.readouterr().out
    assert rc == 0, out
    assert a["run_id"] in out and b["run_id"] in out

    rc = main(["--repo", str(tmp_path), "compare",
               "--runs", f"{a['run_id']},{b['run_id']}"])
    out = capsys.readouterr().out
    assert rc == 0, out
    assert "FEE_DIFF" in out and "return%" in out

    rc = main(["--repo", str(tmp_path), "compare",
               "--runs", a["run_id"], "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert payload["schema"] == 1
    assert payload["runs"][0]["run_id"] == a["run_id"]

    rc = main(["--repo", str(tmp_path), "compare", "--runs", "missing"])
    assert rc == 2


@pytest.mark.integration
def test_gui_runs_endpoints(tmp_path):
    from nobitex_adapter.configgen import default_user_data_layout
    from nobitex_adapter.webui.app import create_app

    results_dir = default_user_data_layout(tmp_path)["results_dir"]
    a = _register(results_dir, name="a")
    b = _register(results_dir, name="b", spec_kw={"exchange": "azbit"})
    with TestClient(create_app(root=tmp_path)) as client:
        r = client.get("/api/runs")
        assert r.status_code == 200
        ids = {x["run_id"] for x in r.json()["runs"]}
        assert {a["run_id"], b["run_id"]} <= ids

        r = client.get(f"/api/runs/compare?ids={a['run_id']},{b['run_id']}")
        assert r.status_code == 200
        payload = r.json()
        assert payload["schema"] == 1
        assert "EXCHANGE_DIFF" in {w["code"] for w in payload["warnings"]}

        r = client.get("/api/runs/compare")
        assert r.status_code == 400
        r = client.get("/api/runs/compare?ids=missing")
        assert r.status_code == 400
