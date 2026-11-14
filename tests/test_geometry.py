"""切片几何检查：重层/缺层/层距/方向/标签缺失与 ZIP 完整性规则。"""
from __future__ import annotations

import zipfile

import pytest
from pydicom.uid import CTImageStorage, ExplicitVRLittleEndian

from app import geometry, storage
from app.config import Settings
from app.db import Database, set_db
from simulator.generate_data import build_ct_dataset

TS = str(ExplicitVRLittleEndian)
AXIAL_IOP = [1.0, 0.0, 0.0, 0.0, 1.0, 0.0]


@pytest.fixture()
def workspace(tmp_path, monkeypatch):
    data = tmp_path / "data"
    monkeypatch.setattr(Settings, "DATA_DIR", data)
    monkeypatch.setattr(Settings, "DB_PATH", data / "retrieval.db")
    monkeypatch.setattr(Settings, "BATCHES_DIR", data / "batches")
    monkeypatch.setattr(Settings, "ZIP_DIR", data / "zips")
    Settings.ensure_dirs()
    db = Database(Settings.DB_PATH)
    set_db(db)
    yield data, db
    db.close()


def _make_batch(db, batch_id, slices):
    """slices: list of dict(series, sop, z, iop?, drop_iop?, drop_ipp?)"""
    db.create_batch(batch_id, "1.2.3.9", "")
    db.transition_state(batch_id, ["PENDING"], "FINDING")
    db.set_manifest(batch_id, [
        (s["sop"], str(CTImageStorage), TS) for s in slices
    ])
    db.transition_state(batch_id, ["FINDING"], "MOVING")
    for s in slices:
        ds = build_ct_dataset(
            study_uid="1.2.3.9", series_uid=s["series"], sop_uid=s["sop"],
            patient_id="P1", patient_name="T^T", instance_number=1,
            instance_count=1,
        )
        ds.ImageOrientationPatient = s.get("iop", AXIAL_IOP)
        ds.ImagePositionPatient = [0.0, 0.0, float(s["z"])]
        if s.get("drop_iop"):
            del ds.ImageOrientationPatient
        if s.get("drop_ipp"):
            del ds.ImagePositionPatient
        path = storage.write_dataset(batch_id, ds, TS)
        assert db.register_instance(batch_id, s["sop"], str(path), TS)
    zip_path = storage.build_zip(batch_id)
    expected = {s["sop"] for s in slices}
    return zip_path, expected


def _series(series_uid, zs, sop_prefix="1.9.9"):
    return [
        {"series": series_uid, "sop": f"{sop_prefix}.{i}", "z": z}
        for i, z in enumerate(zs)
    ]


# ------------------------------------------------------------------ 正常判定
def test_uniform_series_is_usable(workspace):
    _, db = workspace
    zip_path, expected = _make_batch(db, "b-ok", _series("1.8.1", [0, 2, 4, 6, 8]))
    rep = geometry.inspect_zip_geometry(zip_path, 2.0, 0.5, expected)
    assert rep["conclusion"] == "usable" and rep["usable"]
    assert rep["errors"] == []
    assert len(rep["series"]) == 1
    s = rep["series"][0]
    assert s["status"] == "ok" and s["issues"] == []
    assert s["slice_count"] == 5
    assert s["normal"] == [0.0, 0.0, 1.0]
    assert s["spacings"] == [2.0, 2.0, 2.0, 2.0]


def test_multiple_series_grouped_by_series_uid(workspace):
    _, db = workspace
    slices = (_series("1.8.2", [0, 2, 4], "1.9.1")
              + _series("1.8.3", [10, 12], "1.9.2"))
    zip_path, expected = _make_batch(db, "b-multi", slices)
    rep = geometry.inspect_zip_geometry(zip_path, 2.0, 0.5, expected)
    assert rep["conclusion"] == "usable"
    assert rep["series_count"] == 2
    assert rep["slice_count"] == 5
    assert {s["series_instance_uid"] for s in rep["series"]} == {"1.8.2", "1.8.3"}


# ------------------------------------------------------------------ 异常判定
def test_missing_slice_detected(workspace):
    _, db = workspace
    # 缺 z=4 这一层：2 -> 6 间距 4.0 = 2×期望层距
    zip_path, expected = _make_batch(db, "b-gap", _series("1.8.4", [0, 2, 6, 8]))
    rep = geometry.inspect_zip_geometry(zip_path, 2.0, 0.5, expected)
    assert rep["conclusion"] == "unusable" and not rep["usable"]
    s = rep["series"][0]
    assert s["status"] == "anomalous"
    kinds = [i["kind"] for i in s["issues"]]
    assert kinds == ["missing_slices"]
    assert "缺 1 层" in s["issues"][0]["detail"]


def test_duplicate_slice_detected(workspace):
    _, db = workspace
    zip_path, expected = _make_batch(db, "b-dup", _series("1.8.5", [0, 2, 2, 4]))
    rep = geometry.inspect_zip_geometry(zip_path, 2.0, 0.5, expected)
    assert rep["conclusion"] == "unusable"
    kinds = [i["kind"] for i in rep["series"][0]["issues"]]
    assert "duplicate_slices" in kinds


def test_irregular_spacing_detected(workspace):
    _, db = workspace
    # 2 -> 3.1 层距 1.1，偏差 0.9 > 0.5 且非整倍缺层
    zip_path, expected = _make_batch(db, "b-irr", _series("1.8.6", [0, 2, 3.1, 6]))
    rep = geometry.inspect_zip_geometry(zip_path, 2.0, 0.5, expected)
    assert rep["conclusion"] == "unusable"
    kinds = [i["kind"] for i in rep["series"][0]["issues"]]
    assert "irregular_spacing" in kinds


def test_inconsistent_orientation_detected(workspace):
    _, db = workspace
    slices = _series("1.8.7", [0, 2, 4])
    # 第三片旋转到矢状面：法向与参考 (0,0,1) 正交
    slices[2]["iop"] = [1.0, 0.0, 0.0, 0.0, 0.0, 1.0]
    zip_path, expected = _make_batch(db, "b-orient", slices)
    rep = geometry.inspect_zip_geometry(zip_path, 2.0, 0.5, expected)
    assert rep["conclusion"] == "unusable"
    kinds = [i["kind"] for i in rep["series"][0]["issues"]]
    assert "inconsistent_orientation" in kinds


def test_reversed_orientation_is_normalized(workspace):
    """法向反向（IOP 行列互换）不算不一致，投影翻正后层距仍均匀。"""
    _, db = workspace
    slices = _series("1.8.8", [0, 2, 4])
    slices[1]["iop"] = [0.0, 1.0, 0.0, 1.0, 0.0, 0.0]  # 法向 (0,0,-1)
    zip_path, expected = _make_batch(db, "b-flip", slices)
    rep = geometry.inspect_zip_geometry(zip_path, 2.0, 0.5, expected)
    assert rep["conclusion"] == "usable", rep


# ------------------------------------------------------------------ 无法判定
def test_missing_geometry_tags_is_undetermined(workspace):
    _, db = workspace
    slices = _series("1.8.9", [0, 2, 4])
    slices[1]["drop_ipp"] = True
    zip_path, expected = _make_batch(db, "b-notags", slices)
    rep = geometry.inspect_zip_geometry(zip_path, 2.0, 0.5, expected)
    assert rep["conclusion"] == "undetermined" and not rep["usable"]
    s = rep["series"][0]
    assert s["status"] == "undetermined"
    assert s["issues"][0]["kind"] == "missing_geometry_tags"
    assert "无法判定" in s["reason"]


def test_single_slice_series_is_undetermined(workspace):
    _, db = workspace
    zip_path, expected = _make_batch(db, "b-single", _series("1.8.10", [0]))
    rep = geometry.inspect_zip_geometry(zip_path, 2.0, 0.5, expected)
    assert rep["conclusion"] == "undetermined" and not rep["usable"]
    assert rep["series"][0]["status"] == "undetermined"
    assert "少于 2 片" in rep["series"][0]["reason"]


def test_undetermined_series_blocks_usable_conclusion(workspace):
    """一个序列无法判定时，即使其它序列正常，整批也不得报合格。"""
    _, db = workspace
    slices = (_series("1.8.11", [0, 2, 4], "1.9.3")
              + _series("1.8.12", [0], "1.9.4"))
    zip_path, expected = _make_batch(db, "b-mixed", slices)
    rep = geometry.inspect_zip_geometry(zip_path, 2.0, 0.5, expected)
    assert rep["conclusion"] == "undetermined" and not rep["usable"]


# ------------------------------------------------------------------ ZIP 完整性
def test_missing_zip_never_passes(workspace):
    data, db = workspace
    rep = geometry.inspect_zip_geometry(
        data / "zips" / "nonexistent.zip", 2.0, 0.5, set()
    )
    assert rep["conclusion"] == "undetermined" and not rep["usable"]
    assert any("ZIP 文件缺失" in e for e in rep["errors"])


def test_corrupted_zip_never_passes(workspace, tmp_path):
    _, db = workspace
    bad = tmp_path / "bad.zip"
    bad.write_bytes(b"this is not a zip file at all")
    rep = geometry.inspect_zip_geometry(bad, 2.0, 0.5, set())
    assert rep["conclusion"] == "undetermined" and not rep["usable"]
    assert any("损坏" in e for e in rep["errors"])


def test_corrupted_entry_never_passes(workspace):
    """ZIP 中混入了读不出的条目：不得用已读切片给出合格结论。"""
    data, db = workspace
    zip_path, expected = _make_batch(db, "b-ok2", _series("1.8.13", [0, 2, 4]))
    with zipfile.ZipFile(zip_path, "a") as zf:
        zf.writestr("dicom/garbage.dcm", b"\x00\x01not-a-dicom")
    rep = geometry.inspect_zip_geometry(zip_path, 2.0, 0.5, expected)
    assert rep["conclusion"] == "undetermined" and not rep["usable"]
    assert any("读取失败" in e for e in rep["errors"])


def test_replaced_zip_mismatching_manifest_never_passes(workspace):
    """ZIP 被整体替换（SOP 集合与登记清单不符）→ 不得报合格。"""
    _, db = workspace
    zip_path, _ = _make_batch(db, "b-victim", _series("1.8.14", [0, 2, 4]))
    wrong_expected = {"1.9.9.0", "1.9.9.1", "1.9.9.99"}  # 与 ZIP 内容不符
    rep = geometry.inspect_zip_geometry(zip_path, 2.0, 0.5, wrong_expected)
    assert rep["conclusion"] == "undetermined" and not rep["usable"]
    assert any("不一致" in e for e in rep["errors"])


def test_zip_from_other_batch_detected_as_replaced(workspace):
    """把另一批的 ZIP 覆盖到本批路径：内容合法但清单对不上。"""
    _, db = workspace
    zip_a, expected_a = _make_batch(db, "b-a", _series("1.8.15", [0, 2, 4]))
    zip_b, _ = _make_batch(db, "b-b", _series("1.8.16", [0, 2, 4], "1.9.8"))
    zip_a.write_bytes(zip_b.read_bytes())  # 模拟 ZIP 被替换
    rep = geometry.inspect_zip_geometry(zip_a, 2.0, 0.5, expected_a)
    assert rep["conclusion"] == "undetermined" and not rep["usable"]
    assert any("不一致" in e for e in rep["errors"])
