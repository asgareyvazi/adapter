"""Unit tests: strict OHLCV validation report (monotonicity, dups, OHLC,
volume, spacing, missing, tz/era, expected range)."""
from __future__ import annotations

import copy

import pytest

from conftest import make_df
from nobitex_adapter.validator import validate

BASE_TS = 1_700_000_000  # a plausible 2023 UTC epoch


def test_clean_data_passes():
    df = make_df(BASE_TS, 100, 300)
    out, rep = validate(df, "BTC/USDT", "5m")
    assert rep.status == "PASS"
    assert rep.rows == 100
    assert rep.duplicates == 0
    assert rep.invalid_ohlc == 0
    assert rep.invalid_volume == 0
    assert rep.missing_intervals == 0
    assert rep.candle_spacing_ok is True
    assert out is not df  # returned a working copy


def test_duplicate_timestamps_counted():
    df = make_df(BASE_TS, 100, 300)
    dup = copy.deepcopy(df)
    dup.loc[50] = dup.loc[49]  # duplicate the ts of row 49 onto row 50
    _, rep = validate(dup, "BTC/USDT", "5m")
    assert rep.duplicates >= 1
    assert rep.status == "FAIL"


def test_duplicate_repair_keeps_last():
    df = make_df(BASE_TS, 100, 300)
    dup = copy.deepcopy(df)
    dup.loc[50, "close"] = 999.0
    dup.loc[50, "date"] = dup.loc[49, "date"]  # duplicate ts, newer close
    out, rep = validate(dup, "BTC/USDT", "5m", repair=True)
    assert rep.duplicates >= 1
    assert len(out) < len(dup)
    # keep-last semantics: the 999.0 close survives
    assert out[out["close"] == 999.0].shape[0] == 1


def test_non_monotonic_detected():
    df = make_df(BASE_TS, 50, 300)
    bad = copy.deepcopy(df)
    bad.loc[10, "date"], bad.loc[11, "date"] = bad.loc[11, "date"], bad.loc[10, "date"]
    _, rep = validate(bad, "BTC/USDT", "5m")
    assert rep.non_monotonic >= 1
    assert rep.status == "FAIL"


def test_non_monotonic_repair_resorts():
    df = make_df(BASE_TS, 50, 300)
    bad = copy.deepcopy(df)
    bad.loc[10, "date"], bad.loc[11, "date"] = bad.loc[11, "date"], bad.loc[10, "date"]
    out, rep = validate(bad, "BTC/USDT", "5m", repair=True)
    assert rep.non_monotonic == 0  # after repair
    dates = out["date"].tolist()
    assert dates == sorted(dates)


def test_bad_ohlc_relation_high_below_low():
    df = make_df(BASE_TS, 20, 300)
    df.loc[3, "high"] = 1.0
    df.loc[3, "low"] = 500.0  # low > high
    _, rep = validate(df, "BTC/USDT", "5m")
    assert rep.invalid_ohlc >= 1
    assert rep.status == "FAIL"


def test_non_positive_price_rejected():
    df = make_df(BASE_TS, 20, 300)
    df.loc[4, "open"] = 0.0
    _, rep = validate(df, "BTC/USDT", "5m")
    assert rep.invalid_ohlc >= 1
    assert rep.status == "FAIL"


def test_negative_volume_rejected():
    df = make_df(BASE_TS, 20, 300)
    df.loc[2, "volume"] = -1.0
    _, rep = validate(df, "BTC/USDT", "5m")
    assert rep.invalid_volume >= 1
    assert rep.status == "FAIL"


def test_missing_column_fails():
    df = make_df(BASE_TS, 20, 300).drop(columns=["volume"])
    _, rep = validate(df, "BTC/USDT", "5m")
    assert rep.status == "FAIL"
    assert any("volume" in p for p in rep.problems)


def test_empty_dataframe_fails():
    df = make_df(BASE_TS, 0, 300)
    _, rep = validate(df, "BTC/USDT", "5m")
    assert rep.status == "FAIL"
    assert any("no rows" in p for p in rep.problems)


def test_gap_detected_as_missing_intervals():
    df = make_df(BASE_TS, 100, 300)
    # remove a 3-candle window in the middle -> one gap of 3 missing
    keep = list(range(0, 50)) + list(range(53, 100))
    df = df.iloc[keep].reset_index(drop=True)
    _, rep = validate(df, "BTC/USDT", "5m")
    assert rep.missing_intervals >= 1
    assert any("missing" in g for g in rep.gap_ranges)


def test_timezone_era_check():
    # timestamps before 2000 or after 2100 => tz/epoch problem
    df = make_df(10_000_000, 10, 300)  # 1970
    _, rep = validate(df, "BTC/USDT", "5m")
    assert rep.status == "FAIL"
    assert any("era" in p or "timezone" in p.lower() for p in rep.problems)


def test_expected_range_start_late_flagged():
    df = make_df(BASE_TS + 10 * 300, 100, 300)  # starts 10 candles late
    _, rep = validate(df, "BTC/USDT", "5m",
                      expected_start_ts=BASE_TS, expected_end_ts=BASE_TS + 200 * 300)
    assert any("starts" in p for p in rep.problems)


def test_expected_range_end_early_flagged():
    df = make_df(BASE_TS, 50, 300)  # ends 150 candles before requested end
    _, rep = validate(df, "BTC/USDT", "5m",
                      expected_start_ts=BASE_TS, expected_end_ts=BASE_TS + 200 * 300,
                      end_is_open=False)
    assert any("ends" in p for p in rep.problems)


def test_incomplete_last_candle_tolerated_when_open():
    # data ends 1 candle short but end_is_open=True -> no "ends early" problem
    df = make_df(BASE_TS, 100, 300)
    _, rep = validate(df, "BTC/USDT", "5m",
                      expected_start_ts=BASE_TS,
                      expected_end_ts=BASE_TS + 101 * 300,
                      end_is_open=True)
    assert not any("ends" in p for p in rep.problems)


def test_report_to_dict_and_render():
    df = make_df(BASE_TS, 100, 300)
    _, rep = validate(df, "BTC/USDT", "5m")
    d = rep.to_dict()
    assert d["pair"] == "BTC/USDT"
    assert d["timeframe"] == "5m"
    assert d["status"] == "PASS"
    text = rep.render()
    assert "BTC/USDT" in text
    assert "PASS" in text


def test_different_timeframe_interval_respected():
    # 1h data: 300s spacing would be a spacing violation
    df = make_df(BASE_TS, 20, 300)
    _, rep = validate(df, "BTC/USDT", "1h")
    assert rep.candle_spacing_ok is False
