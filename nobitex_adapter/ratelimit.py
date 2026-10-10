"""Shared per-endpoint rate limiter for the exchange HTTP clients.

The Nobitex/AZBit/Wallex clients previously each carried a verbatim copy of
this tiny token-bucket limiter; it now lives here. Only the LIMITER is
shared: the retry/backoff loops stay per-client because each exchange has a
different failure envelope (Nobitex ``backOff``/``status=failed``, AZBit
``Code``/``Message``, Wallex ``success``/``Retry-After``), and coupling
those would trade honest per-exchange semantics for illusory dedup.
"""
from __future__ import annotations

import threading
import time


class RateLimiter:
    """Tiny per-endpoint token-bucket limiter (thread-safe)."""

    def __init__(self, rps: dict[str, float]) -> None:
        self._rps = dict(rps)
        self._lock = threading.Lock()
        self._last: dict[str, float] = {}

    def wait(self, endpoint: str) -> None:
        rps = self._rps.get(endpoint, 5.0)
        if rps <= 0:
            return
        interval = 1.0 / rps
        with self._lock:
            now = time.monotonic()
            last = self._last.get(endpoint, 0.0)
            sleep_for = last + interval - now
            self._last[endpoint] = max(now, last) + interval
        if sleep_for > 0:
            time.sleep(sleep_for)
