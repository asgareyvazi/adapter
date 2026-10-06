"""Freqtrade runtime discovery, probing and binding.

The adapter must run backtests inside the user's SELECTED Freqtrade
environment (``<repo>/.venv``), not whichever Freqtrade happens to be
installed in the Python that started the CLI/GUI.

Design (documented in README "Runtime architecture"):

* ``resolve_runtime(repo)`` validates the selected Freqtrade repository and
  locates ``.venv``'s Python interpreter (``.venv/Scripts/python.exe`` on
  Windows, ``.venv/bin/python`` on POSIX; ``venv/`` accepted as fallback).
* ``probe_runtime(python)`` runs a tiny stdlib probe with that interpreter
  and reports Python/Freqtrade/CCXT versions + Freqtrade module path.
* The backtest is ALWAYS executed as a subprocess with the selected venv's
  interpreter (see ``jobs.make_backtest_job`` / ``cli.cmd_backtest``), so
  ccxt Nobitex registration and the Freqtrade engine live in ONE process
  that is the selected runtime.
* ``reexec_into_runtime()`` (used by the CLI) transparently re-launches the
  whole CLI with the selected venv's interpreter when different, so a single
  ``python -m nobitex_adapter backtest --repo X`` works from ANY Python.

This module is stdlib-only: it must import cleanly from any Python 3.10+.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

# candidate venv interpreter paths, in priority order (POSIX + Windows)
_VENV_DIRS = (".venv", "venv")
_VENV_PYTHON_CANDIDATES = (
    Path("Scripts") / "python.exe",  # Windows venv
    Path("Scripts") / "python",
    Path("bin") / "python",
    Path("bin") / "python3",
)


class AdapterRuntimeError(RuntimeError):
    """Actionable runtime error (shown to the user, no traceback)."""


@dataclass
class RuntimeInfo:
    """A resolved Freqtrade runtime (selected repo + its venv interpreter)."""

    repo: Path
    venv_dir: Optional[Path] = None
    python: Optional[Path] = None
    python_version: Optional[str] = None
    freqtrade_version: Optional[str] = None
    freqtrade_module: Optional[str] = None
    ccxt_version: Optional[str] = None
    freqtrade_error: Optional[str] = None
    ok: bool = False
    error: Optional[str] = None
    probe: dict = field(default_factory=dict)

    @property
    def summary(self) -> str:
        if not self.ok:
            return f"UNAVAILABLE: {self.error}"
        parts = [f"python={self.python}", f"py={self.python_version}"]
        if self.freqtrade_version:
            parts.append(f"freqtrade={self.freqtrade_version}")
        if self.ccxt_version:
            parts.append(f"ccxt={self.ccxt_version}")
        return " ".join(parts)


def candidate_python_paths(repo: Path) -> list[Path]:
    """All plausible venv interpreter paths under `repo` (deterministic)."""
    out: list[Path] = []
    for venv in _VENV_DIRS:
        for rel in _VENV_PYTHON_CANDIDATES:
            out.append(repo / venv / rel)
    return out


def find_venv_python(repo: Path) -> Optional[Path]:
    """Locate the selected repo's venv interpreter, or None."""
    for p in candidate_python_paths(repo):
        if p.is_file():
            return p
    return None


_PROBE_CODE = r"""
import json, sys
out = {
    "python": sys.executable,
    "python_version": sys.version.split()[0],
    "freqtrade": None,
    "freqtrade_module": None,
    "freqtrade_error": None,
    "ccxt": None,
}
try:
    import freqtrade
    out["freqtrade"] = getattr(freqtrade, "__version__", None)
    out["freqtrade_module"] = getattr(freqtrade, "__file__", None)
except Exception as e:  # noqa: BLE001
    out["freqtrade_error"] = f"{type(e).__name__}: {e}"
try:
    import ccxt
    out["ccxt"] = getattr(ccxt, "__version__", None)
except Exception:  # noqa: BLE001
    pass
print("NOBITEX_ADAPTER_PROBE " + json.dumps(out))
"""


def probe_runtime(python: Path, cwd: Optional[Path] = None, timeout: float = 120.0) -> dict:
    """Run the version probe with `python` (cwd defaults to its venv repo).

    Raises AdapterRuntimeError when the interpreter cannot run the probe.
    """
    env = dict(os.environ)
    try:
        proc = subprocess.run(
            [str(python), "-c", _PROBE_CODE],
            capture_output=True, text=True, timeout=timeout,
            cwd=str(cwd) if cwd else None, env=env,
        )
    except subprocess.TimeoutExpired:
        raise AdapterRuntimeError(
            f"Freqtrade runtime probe timed out after {timeout:.0f}s: {python}"
        ) from None
    except OSError as exc:
        raise AdapterRuntimeError(
            f"cannot execute selected Python interpreter {python}: {exc}"
        ) from exc
    marker = "NOBITEX_ADAPTER_PROBE "
    for line in (proc.stdout or "").splitlines():
        if line.startswith(marker):
            try:
                return json.loads(line[len(marker):])
            except json.JSONDecodeError:
                break
    detail = (proc.stderr or proc.stdout or "").strip().splitlines()
    tail = " | ".join(detail[-3:]) if detail else f"exit={proc.returncode}"
    raise AdapterRuntimeError(f"runtime probe failed on {python}: {tail[:400]}")


def resolve_runtime(repo: Path, *, require_freqtrade: bool = True) -> RuntimeInfo:
    """Validate a selected Freqtrade repository and probe its venv.

    Raises AdapterRuntimeError with an actionable message when:
      * the repo path does not exist
      * no .venv interpreter is found
      * Freqtrade is not installed in the venv (when require_freqtrade)
    """
    repo = Path(repo).expanduser()
    if not repo.is_dir():
        raise AdapterRuntimeError(f"Selected Freqtrade repository does not exist: {repo}")
    py = find_venv_python(repo)
    if py is None:
        raise AdapterRuntimeError(
            f"Selected .venv does not exist: expected {repo / '.venv'}/"
            "(Scripts/python.exe | bin/python). Is this a Freqtrade clone with a "
            "virtual environment?"
        )
    info = RuntimeInfo(repo=repo, venv_dir=py.parent, python=py)
    try:
        probe = probe_runtime(py, cwd=repo)
    except AdapterRuntimeError as exc:
        info.error = str(exc)
        return info
    info.probe = probe
    info.python_version = probe.get("python_version")
    info.ccxt_version = probe.get("ccxt")
    info.freqtrade_version = probe.get("freqtrade")
    info.freqtrade_module = probe.get("freqtrade_module")
    info.freqtrade_error = probe.get("freqtrade_error")
    if require_freqtrade and not info.freqtrade_version:
        info.error = (
            f"Freqtrade is not installed in selected .venv ({info.venv_dir}): "
            f"{info.freqtrade_error or 'unknown import error'}. "
            "Run `pip install -e .` inside that Freqtrade repository/venv."
        )
        raise AdapterRuntimeError(info.error)
    info.ok = True
    return info


def adapter_package_root() -> Path:
    """Parent directory of the nobitex_adapter package (for PYTHONPATH)."""
    return Path(__file__).resolve().parent.parent


def build_runtime_env(repo: Path, extra_pythonpath: Optional[Path] = None) -> dict:
    """Environment for a subprocess that must import the adapter + run Freqtrade.

    PYTHONPATH always includes the adapter package root so `python -m
    nobitex_adapter` works in the selected venv regardless of whether the
    adapter is pip-installed there.
    """
    env = dict(os.environ)
    parts = [str(adapter_package_root())]
    if extra_pythonpath is not None:
        parts.append(str(extra_pythonpath))
    if env.get("PYTHONPATH"):
        parts.append(env["PYTHONPATH"])
    env["PYTHONPATH"] = os.pathsep.join(dict.fromkeys(parts))
    return env


def _current_freqtrade_module() -> Optional[str]:
    """Resolved __file__ of freqtrade in the CURRENT interpreter (or None)."""
    try:
        import freqtrade

        return str(Path(freqtrade.__file__).resolve())
    except Exception:  # noqa: BLE001 - no freqtrade here
        return None


def _same_freqtrade_environment(info: "RuntimeInfo") -> bool:
    """True when the current interpreter already runs the selected venv's
    Freqtrade (i.e. no re-exec needed).

    NOTE: we deliberately do NOT compare `sys.executable` to the venv python —
    a venv's bin/python is a SYMLINK to the base interpreter, so the
    resolved paths would match even for completely different environments.
    The environment identity is the site-packages, i.e. the resolved
    freqtrade module path.
    """
    current = _current_freqtrade_module()
    if current is None:
        return False  # this interpreter has no Freqtrade at all
    if not info.freqtrade_module:
        return False
    try:
        return current == str(Path(info.freqtrade_module).resolve())
    except OSError:
        return False


def reexec_into_runtime(repo: Path) -> Optional[int]:
    """Re-launch the current CLI with the selected venv's interpreter.

    Returns a process exit code when a re-exec happened; None when the
    current interpreter already IS the selected runtime (nothing to do).
    """
    try:
        info = resolve_runtime(repo, require_freqtrade=True)
    except AdapterRuntimeError as exc:
        # The selected repo has no usable .venv. If THIS interpreter can run
        # Freqtrade we may continue (with a warning); otherwise fail clearly.
        if _current_freqtrade_module() is not None:
            print(f"[runtime] NOTE: {exc}")
            print("[runtime] continuing with the current interpreter "
                  f"({sys.executable}).")
            return None
        print(f"[runtime] ERROR: {exc}")
        print("[runtime] this interpreter also has no Freqtrade, so the "
              "backtest cannot run.\n"
              "[runtime] point --repo at a Freqtrade clone that contains "
              "a working .venv (see docs/README).")
        sys.exit(2)
    if _same_freqtrade_environment(info):
        return None
    env = build_runtime_env(repo)
    print(f"[runtime] re-executing with selected Freqtrade runtime: {info.python}")
    print(f"[runtime] {info.summary}")
    proc = subprocess.run(
        [str(info.python), "-m", "nobitex_adapter"] + sys.argv[1:],
        env=env,
        cwd=os.getcwd(),
    )
    sys.exit(proc.returncode)


def diagnose(repo: Optional[Path]) -> dict:
    """Full runtime diagnostic (CLI logs + GUI /api/runtime + job results).

    `repo` = selected Freqtrade repo (None -> adapter's own root).
    """
    from . import __version__

    host: dict = {
        "python": sys.executable,
        "python_version": sys.version.split()[0],
        "freqtrade": None,
        "freqtrade_module": None,
        "ccxt": None,
    }
    try:
        import freqtrade

        host["freqtrade"] = getattr(freqtrade, "__version__", None)
        host["freqtrade_module"] = getattr(freqtrade, "__file__", None)
    except Exception as exc:  # noqa: BLE001 - host may legitimately lack freqtrade
        host["freqtrade_error"] = f"{type(exc).__name__}: {exc}"
    try:
        import ccxt

        host["ccxt"] = getattr(ccxt, "__version__", None)
    except Exception:  # noqa: BLE001
        pass

    selected: dict
    if repo is not None:
        try:
            info = resolve_runtime(Path(repo), require_freqtrade=False)
            selected = {
                "ok": info.ok,
                "error": info.error,
                "repo": str(info.repo),
                "venv": str(info.venv_dir) if info.venv_dir else None,
                "python": str(info.python) if info.python else None,
                "python_version": info.python_version,
                "freqtrade": info.freqtrade_version,
                "freqtrade_module": info.freqtrade_module,
                "ccxt": info.ccxt_version,
            }
        except AdapterRuntimeError as exc:
            selected = {"ok": False, "error": str(exc), "repo": str(repo)}
    else:
        selected = {"ok": False, "error": "no Freqtrade repository selected"}

    return {
        "adapter_version": __version__,
        "adapter_root": str(adapter_package_root()),
        "host": host,
        "selected": selected,
    }


def format_diagnostic(d: dict) -> str:
    """One compact block for CLI logs."""
    lines = ["runtime diagnostic"]
    h = d["host"]
    lines.append(f"  python      : {h['python']} ({h['python_version']})")
    lines.append(f"  freqtrade   : {h.get('freqtrade') or h.get('freqtrade_error', 'n/a')}")
    if h.get("freqtrade_module"):
        lines.append(f"  ft module   : {h['freqtrade_module']}")
    lines.append(f"  ccxt        : {h.get('ccxt') or 'n/a'}")
    lines.append(f"  adapter     : {d['adapter_version']} @ {d['adapter_root']}")
    s = d["selected"]
    if s.get("ok"):
        lines.append(f"  selected    : {s.get('python')}")
        lines.append(f"                freqtrade {s.get('freqtrade')} | ccxt {s.get('ccxt')}")
        if s.get("freqtrade_module"):
            lines.append(f"                ft module {s['freqtrade_module']}")
    else:
        lines.append(f"  selected    : {s.get('error')}")
    return "\n".join(lines)
