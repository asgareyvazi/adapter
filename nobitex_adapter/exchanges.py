"""Exchange identifiers shared without third-party imports.

``SUPPORTED_EXCHANGES`` is the SINGLE source of truth for every exchange id
the adapter knows (``providers`` registry, CLI ``--exchange`` choices, GUI
job validation). It lives in this stdlib-only module — NOT in
``providers/__init__`` — because ``cli.build_parser`` needs the ``choices=``
list at parser-build time while ``python -m nobitex_adapter doctor`` must
also run on a BARE interpreter without third-party packages installed
(``providers/__init__`` imports ``requests`` transitively, so importing it
from ``build_parser`` would break the bare-interpreter path — covered by
tests/test_e2e_runtime.py).
"""
from __future__ import annotations

SUPPORTED_EXCHANGES: tuple[str, ...] = ("nobitex", "azbit", "wallex")
