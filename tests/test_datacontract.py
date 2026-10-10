"""Unit tests: data-contract v1 fingerprints, sidecars, legacy compat."""
from __future__ import annotations

import hashlib
import json
import struct
from datetime import datetime, timezone

import pandas as pd
import pytest

from conftest import make_df
from nobitex_adapter.datacontract import (
    DATA_CONTRACT_VERSION,
    canonical_rows,
    contract_block,
    fingerprint_dataframe,
    fingerprint_file,
    is_legacy_sidecar,
    provenance,
    sidecar_contract_version,
)
from nobitex_adapter.downloader import DownloadRequest, Downloader
from nobitex_adapter.nobitex_client import Candle

pytestmark = pytest.mark.unit

START = int(datetime(2024, 6, 1, tzinfo=timezone.utc).timestamp())


def _candles(start_ts: int, n: int, interval: int = 300, price: float = 100.0):
    return [Candle(ts=start_ts + i * interval, open=price, high=price * 1.01,
                   low=price * 0.99, close=price * 1.005, volume=5.0)
            for i in range(n)]


class _FakeClient:
    def __init__(self, candles):
        self._candles = sorted(candles, key=lambda c: c.ts)

    def candles_page(self, symbol, resolution, start_ts, end_ts, page=1, **kw):
        from nobitex_adapter.nobitex_client import NobitexNoData

        sel = [c for c in self._candles if start_ts <= c.ts < end_ts]
        chunk = sel[(page - 1) * 500: page * 500]
        if not chunk:
            raise NobitexNoData("no data", code="NoData")
        return chunk


def _dl(tmp_path, candles):
    return Downloader(_FakeClient(candles), datadir=tmp_path / "data",
                      manifest_dir=tmp_path / "m", report_dir=tmp_path / "r")


def _req(**kw):
    base = dict(
        pairs=["BTC/USDT"], timeframes=["5m"],
        start=datetime(2024, 6, 1, tzinfo=timezone.utc),
        end=datetime(2024, 6, 1, 1, 0, tzinfo=timezone.utc),
        startup_candles={"5m": 0}, drop_incomplete_last=False,
        end_is_open=False,
    )
    base.update(kw)
    return DownloadRequest(**base)


# ------------------------------------------------------- fingerprint vectors
def test_known_answer_vector():
    """Independent struct-level recomputation matches the fingerprint."""
    df = make_df(1_000_000, 3, 300, base=100.0)
    fp = fingerprint_dataframe(df)
    h = hashlib.sha256()
    for i in range(3):
        # exact float expressions as stored (100.0*1.01 is not 101.0)
        h.update(struct.pack("<q5d", 1_000_000 + i * 300, 100.0,
                             100.0 * 1.01, 100.0 * 0.99, 100.0 * 1.005, 5.0))
    assert fp["sha256"] == f"sha256:{h.hexdigest()}"
    assert fp["rows"] == 3
    assert fp["first_ts"] == 1_000_000
    assert fp["last_ts"] == 1_000_600


def test_empty_frame_fingerprint():
    fp = fingerprint_dataframe(
        pd.DataFrame(columns=["date", "open", "high", "low", "close", "volume"]))
    assert fp["sha256"] == f"sha256:{hashlib.sha256(b'').hexdigest()}"
    assert fp["rows"] == 0
    assert fp["first_ts"] is None and fp["last_ts"] is None


def test_row_order_does_not_matter():
    a = fingerprint_dataframe(make_df(START, 50))
    b = fingerprint_dataframe(make_df(START, 50).sample(frac=1.0, random_state=7))
    assert a == b


def test_date_layouts_and_dtypes_do_not_matter():
    base = make_df(START, 20)
    naive = base.copy()
    naive["date"] = pd.to_datetime(naive["date"], unit="s")  # tz-naive
    aware = base.copy()
    aware["date"] = pd.to_datetime(aware["date"], unit="s", utc=True)  # tz-aware
    asint = base.copy()
    asint["volume"] = asint["volume"].astype("int64")  # lossless spelling
    expect = fingerprint_dataframe(base)
    assert fingerprint_dataframe(naive) == expect
    assert fingerprint_dataframe(aware) == expect
    assert fingerprint_dataframe(asint) == expect
    # ...while a LOSSY spelling (float32 rounding) genuinely changes values
    f32 = base.copy()
    for col in ("open", "high", "low", "close", "volume"):
        f32[col] = f32[col].astype("float32")
    assert fingerprint_dataframe(f32)["sha256"] != expect["sha256"]


def test_duplicate_timestamps_last_wins_like_downloader():
    df = make_df(START, 5)
    dup = pd.concat([df, df.iloc[[-1]].assign(close=999.0)], ignore_index=True)
    single = df.copy()
    single.loc[single.index[-1], "close"] = 999.0
    assert fingerprint_dataframe(dup) == fingerprint_dataframe(single)


def test_any_value_change_alters_fingerprint():
    base = fingerprint_dataframe(make_df(START, 20))
    for col, delta in (("open", 0.5), ("high", 0.5), ("low", 0.5),
                       ("close", 0.5), ("volume", 0.5)):
        mutated = make_df(START, 20)
        mutated.loc[7, col] += delta
        assert fingerprint_dataframe(mutated)["sha256"] != base["sha256"], col
    shifted = make_df(START, 20)
    shifted.loc[7, "date"] += 300
    assert fingerprint_dataframe(shifted)["sha256"] != base["sha256"]


def test_missing_columns_rejected():
    with pytest.raises(ValueError, match="'date'"):
        fingerprint_dataframe(make_df(START, 5).drop(columns=["date"]))
    with pytest.raises(ValueError, match="'volume'"):
        fingerprint_dataframe(make_df(START, 5).drop(columns=["volume"]))


def test_fingerprint_file_roundtrip(tmp_path):
    df = make_df(START, 20)
    df["date"] = pd.to_datetime(df["date"], unit="s", utc=True)
    path = tmp_path / "x.feather"
    df.to_feather(path)
    assert fingerprint_file(path) == fingerprint_dataframe(df)


# ------------------------------------------------------------------ sidecars
def test_contract_version_constant():
    assert DATA_CONTRACT_VERSION == 1


def test_provenance_block_shape():
    p = provenance("wallex", "BTC/USDT", "5m")
    assert p["exchange"] == "wallex"
    assert p["pair"] == "BTC/USDT" and p["timeframe"] == "5m"
    assert p["source"] == "download"
    assert p["adapter_version"] and p["written_at"]


def test_contract_block_shape():
    fp = {"sha256": "sha256:abc", "rows": 1, "first_ts": 1, "last_ts": 1}
    block = contract_block("nobitex", "BTC/USDT", "5m", fp)
    assert block["contract"] == 1
    assert block["fingerprint"] == fp
    assert block["provenance"]["exchange"] == "nobitex"


def test_sidecar_version_detection():
    assert sidecar_contract_version({}) == 0
    assert sidecar_contract_version({"covered": {}}) == 0  # legacy
    assert sidecar_contract_version({"contract": 1}) == 1
    assert sidecar_contract_version({"contract": "bogus"}) == 0
    assert is_legacy_sidecar({"covered": {}}) is True
    assert is_legacy_sidecar({"contract": 1}) is False


# ------------------------------------------------- downloader wiring + legacy
def test_download_writes_v1_manifest_and_report(tmp_path):
    candles = _candles(START - 3600, 40)  # cover the 1h window
    dl = _dl(tmp_path, candles)
    summary = dl.download(_req())
    assert summary.ok
    manifest = json.loads(
        (tmp_path / "m" / "nobitex" / "BTC_USDT-5m.json").read_text(encoding="utf-8"))
    assert manifest["contract"] == 1
    assert manifest["provenance"]["exchange"] == "nobitex"
    assert manifest["fingerprint"]["rows"] == summary.tasks[0].rows
    report = json.loads(
        (tmp_path / "r" / "BTC_USDT-5m.json").read_text(encoding="utf-8"))
    assert report["contract"] == 1
    assert report["fingerprint"] == manifest["fingerprint"]
    # the stamped fingerprint equals the stored feather content
    feather = tmp_path / "data" / "nobitex" / "BTC_USDT-5m.feather"
    assert fingerprint_file(feather) == manifest["fingerprint"]


def test_legacy_manifest_resumes_and_upgrades(tmp_path):
    """A v0 manifest (``covered`` only) still resumes; save upgrades to v1."""
    candles = _candles(START - 3600, 40)
    dl = _dl(tmp_path, candles)
    assert dl.download(_req()).ok
    manifest_path = tmp_path / "m" / "nobitex" / "BTC_USDT-5m.json"
    legacy = {"covered": json.loads(manifest_path.read_text(encoding="utf-8"))["covered"]}
    assert is_legacy_sidecar(legacy) is True
    manifest_path.write_text(json.dumps(legacy), encoding="utf-8")

    dl2 = _dl(tmp_path, candles)
    summary = dl2.download(_req())
    assert summary.ok
    assert summary.tasks[0].chunks_skipped > 0  # resume honored the v0 map
    upgraded = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert upgraded["contract"] == 1
    assert upgraded["fingerprint"]["rows"] == summary.tasks[0].rows
