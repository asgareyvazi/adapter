"""Runtime registration of the Nobitex ccxt class + programmatic Freqtrade runs.

This is the (only) integration seam with Freqtrade. No Freqtrade source is
modified: ccxt's registry is designed to accept new exchange classes at
runtime, and Freqtrade instantiates exchanges via ``getattr(ccxt, name)``.
"""
from __future__ import annotations

import contextlib
import logging
import sys
from typing import Optional

from .ccxt_nobitex import register_ccxt

log = logging.getLogger("nobitex.ft")


def ensure_registered() -> None:
    register_ccxt()


@contextlib.contextmanager
def freqtrade_env():
    """Patch sys.modules-level things Freqtrade needs from a clean CLI entry.

    Kept as a seam so future compatibility shims (if Nobitex needs one for a
    given Freqtrade version) live in exactly one place.
    """
    ensure_registered()
    yield


def run_freqtrade(argv: list[str], *, config_path: Optional[str] = None) -> int:
    """Run Freqtrade's main() in-process.

    `argv` is the CLI tail, e.g.
        ["backtesting", "--config", str(cfg), "--strategy", "X8", "--export", "trades"]
    Returns a process-like exit code (0 = success).
    """
    ensure_registered()
    from freqtrade.main import main

    try:
        main(sysargv=argv)
    except SystemExit as e:
        code = e.code
        return int(code) if isinstance(code, int) else (0 if code in (None, 0) else 1)
    except KeyboardInterrupt:
        return 130
    return 0



