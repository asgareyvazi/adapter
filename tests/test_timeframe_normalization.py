"""Regression tests for the canonical timeframe normalization boundary.

The production incident this protects against: on a real Windows/PowerShell
run, `--timeframes 5m,15m,1h,4h,1d` reached a code path that iterated a
string, so `1d` became `['1', 'd']` and the download died with
`TimeframeError: invalid timeframe '1'` after 5m/15m returned zero candles.

The rule under test: a timeframe STRING is never char-iterated; every input
form (single str, comma str, space str, list, tuple, set, nested) normalizes
to a canonical list, and invalid items raise a clear, actionable error.
"""

from __future__ import annotations

import pytest

from nobitex_adapter.timeframes import (
    SUPPORTED_TIMEFRAMES,
    TimeframeError,
    normalize_timeframes,
)


# ------------------------------------------------------------- happy forms
@pytest.mark.unit
@pytest.mark.parametrize(
    "value, want",
    [
        # single timeframe string
        ("5m", ["5m"]),
        ("1d", ["1d"]),  # CRITICAL: must never become ['1', 'd']
        ("15m", ["15m"]),
        ("1h", ["1h"]),
        ("4h", ["4h"]),
        # comma-separated string
        ("5m,15m,1h", ["5m", "15m", "1h"]),
        ("5m,15m,1h,4h,1d", ["5m", "15m", "1h", "4h", "1d"]),
        # semicolon-separated
        ("5m;15m;1h", ["5m", "15m", "1h"]),
        # space-separated (shells may space-join a user's comma list,
        # e.g. PowerShell's comma-array expansion)
        ("5m 15m 1h", ["5m", "15m", "1h"]),
        ("5m,15m 1h;4h", ["5m", "15m", "1h", "4h"]),
        # whitespace is trimmed
        (" 5m , 15m ,1h ", ["5m", "15m", "1h"]),
        # case-insensitive
        ("5M,1H", ["5m", "1h"]),
        # list / tuple / set
        (["5m", "15m"], ["5m", "15m"]),
        (["5m", "15m", "1h", "4h", "1d"], ["5m", "15m", "1h", "4h", "1d"]),
        (("5m", "15m"), ["5m", "15m"]),
        # nested comma string inside a list
        (["5m,15m", "1h"], ["5m", "15m", "1h"]),
        # dedupe, order preserved
        (["5m", "15m", "5m", "1h"], ["5m", "15m", "1h"]),
        ("5m,5m,15m", ["5m", "15m"]),
    ],
)
def test_normalize_happy(value, want):
    if isinstance(value, set):
        assert sorted(normalize_timeframes(value)) == sorted(want)
    else:
        assert normalize_timeframes(value) == want


@pytest.mark.unit
def test_set_input():
    got = normalize_timeframes({"5m", "15m"})
    assert sorted(got) == ["15m", "5m"]


@pytest.mark.unit
def test_all_five_x8_timeframes():
    """The five timeframes X8 actually requires, end to end through the
    normalization boundary (the user's failing command's --timeframes)."""
    got = normalize_timeframes("5m,15m,1h,4h,1d", param_name="--timeframes")
    assert got == ["5m", "15m", "1h", "4h", "1d"]
    # every one maps to a Nobitex resolution
    from nobitex_adapter.timeframes import to_nobitex_resolution

    for tf in got:
        to_nobitex_resolution(tf)  # must not raise


# ----------------------------------------------------------- the char-split
@pytest.mark.unit
def test_1d_never_char_split():
    """THE incident: '1d' must never become ['1', 'd']."""
    assert normalize_timeframes("1d") == ["1d"]
    assert normalize_timeframes(["5m", "1d"]) == ["5m", "1d"]
    assert normalize_timeframes("5m,15m,1h,4h,1d")[-1] == "1d"
    # and it cannot sneak in via a nested string either
    assert normalize_timeframes(["5m,15m,1h,4h,1d"]) == ["5m", "15m", "1h", "4h", "1d"]


@pytest.mark.unit
def test_no_string_is_char_iterated():
    # A 4-char timeframe that would obviously break under char iteration
    assert normalize_timeframes("15m") == ["15m"]
    assert normalize_timeframes("12h") == ["12h"]


# -------------------------------------------------------------- invalid
@pytest.mark.unit
@pytest.mark.parametrize("bad", ["1", "d", "m", "5", "1 ", " 1"])
def test_rejects_bare_token(bad):
    with pytest.raises(TimeframeError):
        normalize_timeframes(bad)


@pytest.mark.unit
@pytest.mark.parametrize("bad", ["2h", "90m", "5x", "1w", "0d", "100y"])
def test_rejects_unsupported_format_or_value(bad):
    """Well-formed but not a Nobitex resolution (2h, 90m) or not well-formed
    at all (5x, 1w, 0d, 100y) must all be rejected with a clear error."""
    with pytest.raises(TimeframeError):
        normalize_timeframes(bad)


@pytest.mark.unit
def test_rejects_non_string_item():
    with pytest.raises(TimeframeError):
        normalize_timeframes(["5m", 15])
    with pytest.raises(TimeframeError):
        normalize_timeframes(None)
    with pytest.raises(TimeframeError):
        normalize_timeframes(5)


@pytest.mark.unit
def test_rejects_empty():
    with pytest.raises(TimeframeError):
        normalize_timeframes("")
    with pytest.raises(TimeframeError):
        normalize_timeframes(",, ,")
    with pytest.raises(TimeframeError):
        normalize_timeframes([])


@pytest.mark.unit
def test_error_names_the_raw_value_and_is_actionable():
    """The user's exact broken input must produce an error that shows WHAT
    was received and how to fix it (no bare traceback)."""
    with pytest.raises(TimeframeError) as exc:
        normalize_timeframes(["5m", "15m", "1h", "4h", "1", "d"],
                             param_name="--timeframes")
    msg = str(exc.value)
    assert "'1'" in msg
    assert "['5m', '15m', '1h', '4h', '1', 'd']" in msg
    assert "5m" in msg and "1d" in msg
    # actionable hint about quoting (PowerShell incident)
    assert "--timeframes" in msg


@pytest.mark.unit
def test_supported_set_matches_documented_resolutions():
    # sanity: the supported set is exactly the documented Nobitex set
    assert set(SUPPORTED_TIMEFRAMES) == {
        "1m", "5m", "15m", "30m",
        "1h", "3h", "4h", "6h", "12h",
        "1d", "2d", "3d",
    }
