"""Tests for the runtime-binding layer (nobitex_adapter/runtime.py).

These prove that selecting a Freqtrade repo deterministically selects its
.venv's Python/Freqtrade for backtests — without touching the repo itself.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from nobitex_adapter import runtime as rt

HERE = Path(__file__).resolve().parent
ADAPTER_ROOT = HERE.parent


# ------------------------------------------------------------------ fixtures
def _freqtrade_available(py: str) -> bool:
    try:
        r = subprocess.run([py, "-c", "import freqtrade"], capture_output=True,
                           timeout=120)
        return r.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


@pytest.fixture(scope="module")
def ft_python() -> str:
    """A Python that has Freqtrade (normally the test interpreter itself)."""
    if not _freqtrade_available(sys.executable):
        pytest.skip("no Freqtrade in the test interpreter")
    return sys.executable


@pytest.fixture(scope="module")
def fake_repo(tmp_path_factory, ft_python) -> Path:
    """A fake Freqtrade repo: .venv symlink -> the Freqtrade env's prefix.

    The path deliberately contains a SPACE to cover quoting on every layer.
    """
    out = subprocess.run(
        [ft_python, "-c", "import sys; print(sys.prefix)"],
        capture_output=True, text=True, timeout=120,
    )
    venv_root = Path(out.stdout.strip())
    repo = tmp_path_factory.mktemp("repos") / "freqtrade repo"
    (repo / "user_data").mkdir(parents=True)
    (repo / ".venv").symlink_to(venv_root)
    return repo


# ------------------------------------------------------------------ discovery
def test_find_venv_python_posix_layout(fake_repo):
    assert rt.find_venv_python(fake_repo) == fake_repo / ".venv" / "bin" / "python"


def test_find_venv_python_windows_layout(tmp_path):
    win = tmp_path / "winrepo"
    (win / ".venv" / "Scripts").mkdir(parents=True)
    exe = win / ".venv" / "Scripts" / "python.exe"
    exe.write_text("", encoding="utf-8")
    # must be found even on a POSIX host (pure path logic)
    assert rt.find_venv_python(win) == exe


def test_find_venv_python_none_when_missing(tmp_path):
    empty = tmp_path / "empty"
    empty.mkdir()
    assert rt.find_venv_python(empty) is None


def test_find_venv_python_ignores_dir_named_python(tmp_path):
    weird = tmp_path / "weird" / ".venv" / "bin"
    weird.mkdir(parents=True)
    (weird / "python").mkdir()  # a DIRECTORY, not an executable
    assert rt.find_venv_python(tmp_path / "weird") is None


# ------------------------------------------------------------------ probe
def test_probe_runtime_finds_freqtrade(fake_repo):
    probe = rt.probe_runtime(fake_repo / ".venv" / "bin" / "python")
    assert probe["freqtrade"]
    assert probe["ccxt"]
    assert probe["freqtrade_module"]
    assert not probe["freqtrade_error"]


def test_probe_runtime_bare_venv_has_no_freqtrade(tmp_path):
    bare = tmp_path / "barevenv"
    subprocess.run([sys.executable, "-m", "venv", "--without-pip", str(bare)],
                   check=True, timeout=300)
    py = bare / ("Scripts" if os.name == "nt" else "bin") / (
        "python.exe" if os.name == "nt" else "python"
    )
    probe = rt.probe_runtime(py)
    assert probe["freqtrade"] is None
    assert probe["freqtrade_error"]


# ------------------------------------------------------------------ resolve
def test_resolve_runtime_ok(fake_repo):
    info = rt.resolve_runtime(fake_repo)
    assert info.ok
    assert info.python == fake_repo / ".venv" / "bin" / "python"
    assert info.freqtrade_version
    assert info.ccxt_version
    assert info.freqtrade_module


def test_resolve_runtime_missing_repo(tmp_path):
    with pytest.raises(rt.AdapterRuntimeError, match="does not exist"):
        rt.resolve_runtime(tmp_path / "nope")


def test_resolve_runtime_repo_without_venv(tmp_path):
    bare = tmp_path / "bare"
    (bare / "user_data").mkdir(parents=True)
    with pytest.raises(rt.AdapterRuntimeError, match=r"\.venv"):
        rt.resolve_runtime(bare)


def test_resolve_runtime_venv_without_freqtrade(tmp_path):
    repo = tmp_path / "nofreqtrade"
    bare = tmp_path / "barevenv"
    subprocess.run([sys.executable, "-m", "venv", "--without-pip", str(bare)],
                   check=True, timeout=300)
    (repo / "user_data").mkdir(parents=True)
    (repo / ".venv").symlink_to(bare)
    with pytest.raises(rt.AdapterRuntimeError, match="Freqtrade"):
        rt.resolve_runtime(repo, require_freqtrade=True)


# ------------------------------------------------------------ environment id
def test_same_environment_true_for_self(fake_repo):
    info = rt.resolve_runtime(fake_repo)
    if _freqtrade_available(sys.executable):
        # tests run inside the very env the fake repo points at
        assert rt._same_freqtrade_environment(info) is True


def test_same_environment_false_when_no_freqtrade_here(monkeypatch):
    monkeypatch.setattr(rt, "_current_freqtrade_module", lambda: None)
    info = rt.RuntimeInfo(
        repo=Path("/x"), venv_dir=Path("/x/.venv"), python=Path("/x/.venv/bin/python"),
        python_version="3.11", freqtrade_version="2026.9",
        freqtrade_module="/x/.venv/lib/site-packages/freqtrade/__init__.py",
        ccxt_version="4.5.85", freqtrade_error=None, ok=True, error=None, probe={},
    )
    assert rt._same_freqtrade_environment(info) is False


# ------------------------------------------------------------------- re-exec
def test_reexec_noop_when_same_environment(fake_repo):
    """Running from inside the selected venv must NOT spawn a subprocess."""
    if not _freqtrade_available(sys.executable):
        pytest.skip("need Freqtrade in the test interpreter")
    assert rt.reexec_into_runtime(fake_repo) is None


def test_reexec_noop_when_repo_has_no_venv(tmp_path):
    bare = tmp_path / "bare"
    bare.mkdir()
    if not _freqtrade_available(sys.executable):
        pytest.skip("host must have Freqtrade to fall back")
    assert rt.reexec_into_runtime(bare) is None


def test_reexec_clear_error_when_nothing_has_freqtrade(tmp_path, monkeypatch, capsys):
    bare = tmp_path / "bare"
    bare.mkdir()
    monkeypatch.setattr(rt, "_current_freqtrade_module", lambda: None)
    with pytest.raises(SystemExit) as exc:
        rt.reexec_into_runtime(bare)
    assert exc.value.code == 2
    captured = capsys.readouterr()
    assert "ERROR" in captured.out + captured.err


# ------------------------------------------------------------------ environ
def test_build_runtime_env_sets_pythonpath(fake_repo):
    env = rt.build_runtime_env(fake_repo)
    pp = env.get("PYTHONPATH", "")
    assert str(ADAPTER_ROOT) in pp


def test_build_runtime_env_preserves_existing_pythonpath(fake_repo, monkeypatch):
    monkeypatch.setenv("PYTHONPATH", "/custom/path")
    env = rt.build_runtime_env(fake_repo)
    pp = env["PYTHONPATH"]
    assert "/custom/path" in pp and str(ADAPTER_ROOT) in pp


# ----------------------------------------------------------------- diagnose
def test_diagnose_structure(fake_repo):
    d = rt.diagnose(fake_repo)
    assert "host" in d and "selected" in d
    assert d["host"]["python"] == sys.executable
    assert d["selected"]["ok"] is True
    assert d["selected"]["python"] == str(fake_repo / ".venv" / "bin" / "python")
    assert d["selected"]["freqtrade"]
    text = rt.format_diagnostic(d)
    assert "freqtrade" in text.lower()
    assert d["selected"]["freqtrade"] in text
