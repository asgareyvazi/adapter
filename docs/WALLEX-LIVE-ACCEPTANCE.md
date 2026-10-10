# Wallex Live Acceptance — Record (2026-10-10)

## Verdict: PARTIALLY ACCEPTED

The implementation is sound: the full local suite (469 passed / 0 failed),
Wallex unit + mock-integration tests (134 passed), compile check and CLI
smokes all pass on the frozen candidate. The three live checks were executed
exactly as documented and **failed loudly on network unavailability** — the
sandbox egress-filters `api.wallex.ir:443` (TLS EOF), identically to
`apiv2.nobitex.ir` and `data.azbit.com`, while PyPI remains reachable. No live
evidence exists; per mission rules no live acceptance is claimed. No code was
changed (nothing was rejected, nothing failed except the network), so no
regression test was added and X8 is untouched.

## 1. Frozen candidate

- HEAD SHA: `635e5104a0bebecec2a99fa188eee9ac676c5457` (initial commit; the
  sandbox was reset before this mission — the full implementation + prior
  reports survive in the working tree, uncommitted, and were verified
  file-by-file instead of via git history).
- Working tree at start: `M README.md`, untracked `.github/`, `.gitignore`,
  `docs/`, `nobitex_adapter/`, `pyproject.toml`, `tests/`, `user_data/`.
  No commit/push/branch operations performed; tree identical at mission end
  except this file and the `docs/WALLEX.md` matrix-status edit.
- X8: `sha256sum` = `f202e860ee5937a9a426ad01501d6afeedc6e4a24525cb490815994d85d21cd0`
  — matches `STRATEGY_SOURCE.txt` line 7 and the previously reported hash.
- ftenv rebuilt (wiped by the reset): CPython 3.11.2, freqtrade 2026.9,
  ccxt 4.5.85, pandas 3.0.6 — identical to the prior validation environment.
- Repo `user_data/` untouched: the live download/depth runs used
  `--repo /tmp/wallex-live`; a post-run `find` confirms zero feathers and
  zero `*wallex*` files under repo `user_data/`.

## 2. The three live checks (exact record)

Network baseline (all `urlopen`, 8 s timeout, no proxy env vars):
`pypi.org` → 200; `apiv2.nobitex.ir`, `data.azbit.com`, `api.wallex.ir`
(https) → `URLError ... TLS/SSL connection has been closed (EOF)`; plain
`http://api.wallex.ir` → `RemoteDisconnected`. Conclusion: host-level egress
filtering, not a Wallex outage (all three exchange hosts fail identically).

Environment for the checks: no `WALLEX_API_BASE`/`NOBITEX_API_BASE` override
(verified via `env` — the live tests' local-mock skip did not trigger).

**Check 1 — live test file.**
`/home/user/ftenv/bin/python -m pytest tests/test_wallex_live_api.py -m live -v`
→ **5 failed in 162.25 s**. Every test fails with `WallexError: request
failed after 5 retries` caused by `SSLError` (1/2/4/8/16 s backoff, all
retries logged). Loud failure, zero fabricated rows.

**Check 2 — real historical download.**
`python -m nobitex_adapter --exchange wallex --repo /tmp/wallex-live download
--pairs BTC/USDT --timeframes "5m,15m,1h,4h,1d" --start 2024-06-01
--end 2024-06-06` → **exit 1 in ~32 s** (first window exhausts 5 retries, then
`WallexError ... url: /v1/udf/history?symbol=BTCUSDT&resolution=5&
from=1716945000&to=1717095300 (Caused by SSLError ... EOF)` propagates and
aborts the run — same fail-loud behavior as the Nobitex path; no partial
data, no success claim, no feathers written).
(`--repo /tmp/wallex-live` is the sole deviation from the documented command,
required by the do-not-modify-user-data constraint.)

**Check 3 — real market depth.**
`python -m nobitex_adapter --exchange wallex --repo /tmp/wallex-live depth
--pair BTC/USDT --timeframes "5m,15m,1h,4h,1d"` → **exit 0 in ~317 s** with
all 10 probes (wide + recent × 5 TFs, resolutions 5/15/60/240/D) failing on
`SSLError`; honest output `COMMON_EARLIEST = - (no timeframe returned any
history)` + per-probe failure notes. (Exit 0 is by design: depth reports
probe failures explicitly instead of raising.)

## 3. Resolution verdicts

Authoritative documentation (https://api-docs.wallex.ir/, all 7 chunks
re-read 2026-10-10) establishes exactly one value: **`resolution=60`**
(the candles-section example). No other resolution, row cap, or rate limit
is documented anywhere in the reference.

| Freqtrade | Resolution | Status |
| --- | --- | --- |
| `1m` | `1` | UNVERIFIED |
| `5m` | `5` | UNVERIFIED |
| `15m` | `15` | UNVERIFIED |
| `30m` | `30` | UNVERIFIED |
| `1h` | `60` | DOCUMENTED example (not live-verified) |
| `3h` | `180` | UNVERIFIED |
| `4h` | `240` | UNVERIFIED |
| `6h` | `360` | UNVERIFIED |
| `12h` | `720` | UNVERIFIED |
| `1d` | `D` | UNVERIFIED |
| `2d` | `2D` | UNVERIFIED |
| `3d` | `3D` | UNVERIFIED |

Verified: none. Rejected: none. No mapping was changed (changing a mapping
without rejection evidence would itself be fabrication); the capability
matrix in `docs/WALLEX.md` now carries these statuses verbatim. Structure /
timestamp-unit / boundary / ordering / OHLCV semantics: unverified live
(mock-server behavior is deterministic but is NOT live evidence).

## 4. Historical download behavior

Exact period verified against the live API: **none** (no request succeeded).
Pagination, dedupe, ordering, gap and incomplete-candle behavior remain
verified against the mock + scripted fixtures only — no multi-year or
multi-range coverage is claimed.

## 5. Regression validation (after the doc-only change)

- Wallex unit + integration files: **134 passed** (14.77 s).
- Full suite: **469 passed, 14 deselected, 0 failed** (~130 s) — live tests
  still excluded by default (`addopts -m 'not live'`); deselected count
  unchanged (9 pre-existing + 5 Wallex live).
- `compileall -q nobitex_adapter tests`: OK.
- CLI smokes: `markets/download/compare --help` exit 0, `--version` →
  `nobitex 0.1.0`, `compare --list` on empty repo OK.
- No test weakened, none added spuriously, X8 unmodified.

## 6. Remaining limitations / to close on a networked machine

Run checks 1–3 verbatim (with `--repo` of choice); success criteria: check 1
5/5 pass (any `WallexAPIError` on a resolution → correct that ladder entry
in `timeframes.WALLEX_RESOLUTIONS` + add a regression test), check 2 exit 0
with 5 feathers, check 3 exit 0 with a plausible `COMMON_EARLIEST`. Then
upgrade the matrix above entry-by-entry from UNVERIFIED to VERIFIED with
observed row counts and timestamps.
