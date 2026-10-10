"""E2E proof of the runtime-binding architecture.

Proves the core promise of the repair: when the adapter is launched by a
Python WITHOUT Freqtrade and `--repo` points at a Freqtrade clone whose
.venv HAS Freqtrade, the backtest transparently re-execs and runs on the
selected repo's venv (the user's real Freqtrade install) — with the exact
runtime recorded in the result.

Layout under test (mirrors the user's Windows machine):

    freqtrade repo/                      <- path with a SPACE on purpose
      .venv/  -> Freqtrade environment   <- different from the launcher
      user_data/
        data/...                         <- downloaded from the mock API
        strategies/
          NostalgiaForInfinity/          <- nested strategy-repo layout
            NostalgiaForInfinityX8.py
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

from nobitex_adapter.downloader import DownloadRequest, Downloader
from nobitex_adapter.nobitex_client import NobitexClient

pytestmark = pytest.mark.e2e

REPO_ROOT = Path(__file__).resolve().parent.parent
X8 = REPO_ROOT / "user_data" / "strategies" / "NostalgiaForInfinityX8.py"

DATA_START = datetime(2024, 6, 1, tzinfo=timezone.utc)
DATA_END = datetime(2024, 6, 8, tzinfo=timezone.utc)
BT_START = "2024-06-02"
BT_END = "2024-06-06"
PAIRS = ["BTC/USDT"]
X8_TFS = ["5m", "15m", "1h", "4h", "1d"]


# ------------------------------------------------------------------ fixtures
@pytest.fixture(scope="module")
def ft_venv_root() -> Path:
    """Prefix (venv root) of the test interpreter, which must have Freqtrade."""
    try:
        import freqtrade  # noqa: F401
    except Exception:
        pytest.skip("test interpreter has no Freqtrade")
    out = subprocess.run(
        [sys.executable, "-c", "import sys; print(sys.prefix)"],
        capture_output=True, text=True, timeout=120,
    )
    return Path(out.stdout.strip())


@pytest.fixture(scope="module")
def bare_python() -> str:
    """A Python interpreter that does NOT have Freqtrade (the 'launcher').

    The discriminator is the Freqtrade import itself (not the resolved
    executable path — a venv's python is a symlink to the base interpreter,
    so path comparison is meaningless for environment identity).
    """
    from nobitex_adapter import runtime as rt

    seen = set()
    for c in ("/usr/bin/python3", shutil.which("python3") or "",
              shutil.which("python") or ""):
        if not c or c in seen:
            continue
        seen.add(c)
        # Usability first: a path that exists but cannot run code (notably
        # invalid Windows Store aliases, which fail closed here) must never
        # be mistaken for a working Freqtrade-less launcher.
        if not rt.is_usable_python(c):
            continue
        try:
            r = subprocess.run([c, "-c", "import freqtrade"],
                               capture_output=True, timeout=120)
            if r.returncode != 0:
                return c
        except (OSError, subprocess.SubprocessError):
            continue
    pytest.skip("no Freqtrade-less python available to simulate the launcher")


@pytest.fixture(scope="module")
def fake_repo(tmp_path_factory, ft_venv_root, mock_server_url) -> Path:
    """Freqtrade clone (path with a space) with .venv + nested X8 + data."""
    repo = tmp_path_factory.mktemp("runt") / "freqtrade repo"
    (repo / "user_data").mkdir(parents=True)
    (repo / ".venv").symlink_to(ft_venv_root)

    nfi = repo / "user_data" / "strategies" / "NostalgiaForInfinity"
    nfi.mkdir(parents=True)
    assert X8.is_file()
    shutil.copy(X8, nfi / "NostalgiaForInfinityX8.py")

    client = NobitexClient(base_url=mock_server_url)
    try:
        summary = Downloader(
            client,
            datadir=repo / "user_data" / "data",
            manifest_dir=repo / "user_data" / "manifests",
            report_dir=repo / "user_data" / "reports",
        ).download(DownloadRequest(
            pairs=PAIRS, timeframes=X8_TFS, start=DATA_START, end=DATA_END,
            drop_incomplete_last=False, end_is_open=False,
        ))
    finally:
        client.close()
    assert summary.ok, {t.pair: (t.status, t.error) for t in summary.tasks}
    return repo


def _venv_python(repo: Path) -> Path:
    return repo / ".venv" / ("Scripts/python.exe" if os.name == "nt"
                             else "bin/python")


# --------------------------------------------------------------------- tests
def test_cli_reexec_runs_in_selected_venv(fake_repo, bare_python, mock_server_url,
                                          tmp_path):
    """Bare python (no Freqtrade) + --repo -> backtest on the repo's venv."""
    out = tmp_path / "out.json"
    env = dict(os.environ)
    env["NOBITEX_API_BASE"] = mock_server_url
    env["PYTHONPATH"] = str(REPO_ROOT)

    r = subprocess.run(
        [bare_python, "-m", "nobitex_adapter", "--repo", str(fake_repo),
         "backtest",
         "--strategy", "NostalgiaForInfinityX8",
         "--pairs", ",".join(PAIRS),
         "--start", BT_START, "--end", BT_END,
         "--capital", "10000", "--out", str(out)],
        env=env, cwd=str(REPO_ROOT),
        capture_output=True, text=True, timeout=900,
    )
    combined = (r.stdout or "") + (r.stderr or "")
    assert r.returncode == 0, combined[-4000:]

    # the launcher must have announced the re-exec
    assert "[runtime] re-executing" in combined

    res = json.loads(out.read_text(encoding="utf-8"))
    assert res["ok"] is True, res
    assert res["exit_code"] == 0
    rt = res["runtime"]
    # the runtime that RAN is the selected repo's venv, NOT the launcher
    assert Path(rt["python"]).resolve() == _venv_python(fake_repo).resolve()
    assert rt["freqtrade"], "backtest must have run under a real Freqtrade"
    assert rt["freqtrade_module"]
    assert rt["ccxt"]
    assert Path(res["results_zip"]).is_file()


def test_cli_doctor_reports_selected_runtime(fake_repo, bare_python):
    env = dict(os.environ)
    env["PYTHONPATH"] = str(REPO_ROOT)
    r = subprocess.run(
        [bare_python, "-m", "nobitex_adapter", "--repo", str(fake_repo), "doctor"],
        env=env, cwd=str(REPO_ROOT), capture_output=True, text=True, timeout=300,
    )
    assert r.returncode == 0, r.stdout + r.stderr
    text = r.stdout + r.stderr
    assert "selected" in text
    assert str(_venv_python(fake_repo)) in text
    assert "freqtrade" in text.lower()


def test_cli_clear_error_when_repo_has_no_venv(bare_python, tmp_path, mock_server_url):
    env = dict(os.environ)
    env["PYTHONPATH"] = str(REPO_ROOT)
    env["NOBITEX_API_BASE"] = mock_server_url
    repo = tmp_path / "emptyrepo"
    (repo / "user_data").mkdir(parents=True)
    r = subprocess.run(
        [bare_python, "-m", "nobitex_adapter", "--repo", str(repo), "backtest",
         "--strategy", "NostalgiaForInfinityX8", "--pairs", "BTC/USDT",
         "--start", BT_START, "--end", BT_END, "--out", str(tmp_path / "o.json")],
        env=env, cwd=str(REPO_ROOT), capture_output=True, text=True, timeout=300,
    )
    assert r.returncode == 2
    text = (r.stdout or "") + (r.stderr or "")
    assert "[runtime] ERROR" in text
    assert ".venv" in text
    # actionable, not a raw traceback
    assert "Traceback" not in text


def test_gui_repo_binding_runtime_and_backtest(fake_repo, mock_server_url, tmp_path):
    """GUI: /api/runtime, repo switching, and a backtest job bound to the repo."""
    from fastapi.testclient import TestClient

    from nobitex_adapter.webui.app import create_app

    old_base = os.environ.get("NOBITEX_API_BASE")
    os.environ["NOBITEX_API_BASE"] = mock_server_url
    try:
        app = create_app(root=fake_repo)
        c = TestClient(app)

        # runtime diagnostic for the selected repo
        rt = c.get("/api/runtime").json()
        assert rt["selected"]["ok"] is True
        assert Path(rt["selected"]["python"]).resolve() == \
            _venv_python(fake_repo).resolve()
        assert rt["selected"]["freqtrade"]

        st = c.get("/api/status").json()
        assert st["backtest_runtime"]["ok"] is True

        # nested-layout strategy is discoverable
        ss = c.get("/api/strategies").json()["strategies"]
        assert any(s["name"] == "NostalgiaForInfinityX8" for s in ss)

        # run a backtest JOB (subprocess CLI -> bound runtime)
        j = c.post("/api/jobs", json={
            "kind": "backtest",
            "params": {
                "strategy": "NostalgiaForInfinityX8",
                "pairs": ",".join(PAIRS), "start": BT_START, "end": BT_END,
                "capital": 10000,
            },
        }).json()
        job_id = j["job_id"]
        job = None
        for _ in range(300):
            job = c.get(f"/api/jobs/{job_id}").json()
            if job["status"] in ("done", "error", "cancelled"):
                break
            time.sleep(1)
        if job is not None and job["status"] != "done":
            log_lines = c.get(f"/api/jobs/{job_id}/log").json()
            assert job["status"] == "done", (
                job.get("status"), job.get("error"),
                (log_lines if isinstance(log_lines, list) else log_lines.get("log", []))[-20:]
            )
        res = job["result"]
        assert res["ok"] is True
        assert res["runtime"]["freqtrade"]
        assert Path(res["results_zip"]).is_file()

        # runtime repo switching at runtime
        second = tmp_path / "second repo"
        (second / "user_data").mkdir(parents=True)
        r2 = c.post("/api/repo", json={"repo": str(second)}).json()
        assert r2["root"] == str(second)
        assert c.get("/api/status").json()["repo"] == str(second)
        assert c.get("/api/runtime").json()["selected"]["repo"] == str(second)

        # invalid repo -> 400 with actionable message
        bad = c.post("/api/repo", json={"repo": str(tmp_path / "nope")})
        assert bad.status_code == 400
    finally:
        if old_base is None:
            os.environ.pop("NOBITEX_API_BASE", None)
        else:
            os.environ["NOBITEX_API_BASE"] = old_base
