"""切片几何检查单元测试（无网络）。

覆盖
====
* 正常等距序列：VALID、法向与投影正确
* 缺层 / 重层 / 层距异常 / 方向不一致 / 标签缺失 / 单片
* 多序列分组：任一序列异常则整批不可用；无法判定不得报合格
* ZIP 缺失、条目损坏、扫描途中被替换：ScanError，不得给出合格结论
* 期望层距/容差边界
"""
from __future__ import annotations

import zipfile
from pathlib import Path

import pytest
import pydicom

from app import geometry
from app.geometry import (
    ANOM_DUPLICATE,
    ANOM_MISSING_SLICE,
    ANOM_MISSING_TAG,
    ANOM_ORIENTATION,
    ANOM_SPACING,
    ANOM_TOO_FEW,
    ERR_ENTRY_CORRUPT,
    ERR_ZIP_MISSING,
    ERR_ZIP_REPLACED,
    ScanError,
    SERIES_INDETERMINATE,
    SERIES_INVALID,
    SERIES_VALID,
)
from simulator.generate_data import (
    NOMINAL_SLICE_SPACING,
    build_ct_dataset,
    generate_defect_study,
    generate_study,
)

SPACING = NOMINAL_SLICE_SPACING  # 2.0mm
TOL = 0.5


# ------------------------------------------------------------ 构造辅助
def _slice_ds(series_uid: str, sop_uid: str, z: float,
              iop=(1.0, 0.0, 0.0, 0.0, 1.0, 0.0)):
    ds = build_ct_dataset(
        study_uid="1.2.3.9", series_uid=series_uid, sop_uid=sop_uid,
        patient_id="P1", patient_name="T^T", instance_number=1,
        instance_count=1,
    )
    ds.ImagePositionPatient = [0.0, 0.0, float(z)]
    ds.SliceLocation = float(z)
    ds.ImageOrientationPatient = list(iop)
    return ds


def _zip_from_datasets(path: Path, datasets: list[pydicom.Dataset]) -> Path:
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        for ds in datasets:
            buf = pydicom.filebase.DicomBytesIO()
            ds.save_as(buf, enforce_file_format=True)
            zf.writestr(f"dicom/{ds.SOPInstanceUID.replace('.', '_')}.dcm",
                        buf.getvalue())
        zf.writestr("MANIFEST.csv", "sop_instance_uid,file_name\n")
    return path


def _regular_series(series_uid="1.2.3.8", n=6, spacing=SPACING):
    return [
        _slice_ds(series_uid, f"1.2.4.{i}", i * spacing)
        for i in range(n)
    ]


@pytest.fixture()
def zip_path(tmp_path):
    def _build(datasets, name="batch.zip"):
        return _zip_from_datasets(tmp_path / name, datasets)
    return _build


def _anomaly_types(report):
    return {a.type for a in report.anomalies}


# ============================================================== 正常序列
def test_regular_series_is_valid(zip_path):
    path = zip_path(_regular_series())
    reports = geometry.check_zip_geometry(path, SPACING, TOL)
    assert len(reports) == 1
    r = reports[0]
    assert r.status == SERIES_VALID
    assert r.slice_count == 6
    # 轴位 IOP 的法向应为 +Z
    assert r.normal == pytest.approx([0.0, 0.0, 1.0], abs=1e-6)
    assert r.positions == pytest.approx([i * SPACING for i in range(6)])
    assert r.slice_spacings == pytest.approx([SPACING] * 5)
    assert r.min_spacing == r.max_spacing == r.mean_spacing == SPACING
    assert r.anomalies == []


def test_sagittal_orientation_normal(zip_path):
    # 矢状位：行方向=+Y? 这里构造行=+Z、列=+Y，法向应为 -X
    iop = (0.0, 0.0, 1.0, 0.0, 1.0, 0.0)
    dss = [
        _slice_ds("1.2.3.8", f"1.2.4.{i}", 0.0, iop=iop)
        for i in range(3)
    ]
    for i, ds in enumerate(dss):
        ds.ImagePositionPatient = [float(i * SPACING), 0.0, 0.0]
    reports = geometry.check_zip_geometry(zip_path(dss), SPACING, TOL)
    # 行×列 = Z×Y = -X
    assert reports[0].normal == pytest.approx([-1.0, 0.0, 0.0], abs=1e-6)
    assert reports[0].status == SERIES_VALID


def test_spacing_within_tolerance_is_valid(zip_path):
    dss = _regular_series()
    # 制造 0.3mm 偏差，容差 0.5 -> 仍合格
    dss[3].ImagePositionPatient = [0.0, 0.0, 3 * SPACING + 0.3]
    reports = geometry.check_zip_geometry(zip_path(dss), SPACING, TOL)
    assert reports[0].status == SERIES_VALID


# ============================================================== 缺层
def test_missing_slice_detected(zip_path):
    dss = [ds for i, ds in enumerate(_regular_series(n=8)) if i != 4]
    reports = geometry.check_zip_geometry(zip_path(dss), SPACING, TOL)
    r = reports[0]
    assert r.status == SERIES_INVALID
    assert ANOM_MISSING_SLICE in _anomaly_types(r)
    anom = next(a for a in r.anomalies if a.type == ANOM_MISSING_SLICE)
    assert anom.detail["missing_count_estimate"] == 1
    assert anom.detail["gap_mm"] == pytest.approx(2 * SPACING)


def test_generated_missing_study(tmp_path):
    meta = generate_defect_study(tmp_path, 1, "missing")
    dss = [pydicom.dcmread(str(p))
           for p in sorted(Path(meta["directory"]).glob("*.dcm"))]
    reports = geometry.check_zip_geometry(
        _zip_from_datasets(tmp_path / "m.zip", dss), SPACING, TOL)
    assert reports[0].status == SERIES_INVALID
    assert ANOM_MISSING_SLICE in _anomaly_types(reports[0])


# ============================================================== 重层
def test_duplicate_slice_detected(zip_path):
    dss = _regular_series()
    dss[4].ImagePositionPatient = [0.0, 0.0, 3 * SPACING]  # 与第 4 片重合
    reports = geometry.check_zip_geometry(zip_path(dss), SPACING, TOL)
    r = reports[0]
    assert r.status == SERIES_INVALID
    assert ANOM_DUPLICATE in _anomaly_types(r)


def test_generated_duplicate_study(tmp_path):
    meta = generate_defect_study(tmp_path, 2, "duplicate")
    dss = [pydicom.dcmread(str(p))
           for p in sorted(Path(meta["directory"]).glob("*.dcm"))]
    reports = geometry.check_zip_geometry(
        _zip_from_datasets(tmp_path / "d.zip", dss), SPACING, TOL)
    assert reports[0].status == SERIES_INVALID
    assert ANOM_DUPLICATE in _anomaly_types(reports[0])


# ============================================================== 层距异常
def test_spacing_anomaly_detected(zip_path):
    dss = _regular_series()
    # 3.2mm 层距：偏差 1.2mm 超容差，且 3.2/2.0=1.6 非整数倍 -> 层距异常
    dss[4].ImagePositionPatient = [0.0, 0.0, 3 * SPACING + 1.2]
    reports = geometry.check_zip_geometry(zip_path(dss), SPACING, TOL)
    r = reports[0]
    assert r.status == SERIES_INVALID
    assert ANOM_SPACING in _anomaly_types(r)
    assert ANOM_MISSING_SLICE not in _anomaly_types(r)


def test_large_tolerance_absorbs_spacing_anomaly(zip_path):
    dss = _regular_series()
    dss[4].ImagePositionPatient = [0.0, 0.0, 3 * SPACING + 1.2]
    # 容差放大到 1.5mm：同样的偏差变为合格
    reports = geometry.check_zip_geometry(zip_path(dss), SPACING, 1.5)
    assert reports[0].status == SERIES_VALID


def test_large_tolerance_must_not_absorb_missing_slice(zip_path):
    # 缺层（4.0mm = 2× 期望层距）即使在容差 2.0mm 下也必须报缺层：
    # 跨两个标称层位是客观事实，不能被大容差静默吸收为合格
    dss = [ds for i, ds in enumerate(_regular_series(n=8)) if i != 4]
    reports = geometry.check_zip_geometry(zip_path(dss), SPACING, 2.0)
    r = reports[0]
    assert r.status == SERIES_INVALID
    assert ANOM_MISSING_SLICE in _anomaly_types(r)


def test_generated_spacing_study(tmp_path):
    meta = generate_defect_study(tmp_path, 3, "spacing")
    dss = [pydicom.dcmread(str(p))
           for p in sorted(Path(meta["directory"]).glob("*.dcm"))]
    reports = geometry.check_zip_geometry(
        _zip_from_datasets(tmp_path / "s.zip", dss), SPACING, TOL)
    assert reports[0].status == SERIES_INVALID
    assert ANOM_SPACING in _anomaly_types(reports[0])


# ============================================================== 方向不一致
def test_orientation_inconsistent_detected(zip_path):
    dss = _regular_series()
    dss[4].ImageOrientationPatient = [-1.0, 0.0, 0.0, 0.0, 1.0, 0.0]
    reports = geometry.check_zip_geometry(zip_path(dss), SPACING, TOL)
    r = reports[0]
    assert r.status == SERIES_INVALID
    assert ANOM_ORIENTATION in _anomaly_types(r)


def test_generated_orientation_study(tmp_path):
    meta = generate_defect_study(tmp_path, 4, "orientation")
    dss = [pydicom.dcmread(str(p))
           for p in sorted(Path(meta["directory"]).glob("*.dcm"))]
    reports = geometry.check_zip_geometry(
        _zip_from_datasets(tmp_path / "o.zip", dss), SPACING, TOL)
    assert reports[0].status == SERIES_INVALID
    assert ANOM_ORIENTATION in _anomaly_types(reports[0])


# ============================================================== 标签缺失
def test_missing_geometry_tag_is_indeterminate(zip_path):
    dss = _regular_series()
    del dss[4].ImageOrientationPatient
    reports = geometry.check_zip_geometry(zip_path(dss), SPACING, TOL)
    r = reports[0]
    assert r.status == SERIES_INDETERMINATE
    assert ANOM_MISSING_TAG in _anomaly_types(r)
    # 无法判定时不产出位置/法向，避免被误用为合格数据
    assert r.normal is None and r.positions == []


def test_missing_ipp_is_indeterminate(zip_path):
    dss = _regular_series()
    del dss[2].ImagePositionPatient
    reports = geometry.check_zip_geometry(zip_path(dss), SPACING, TOL)
    assert reports[0].status == SERIES_INDETERMINATE
    assert ANOM_MISSING_TAG in _anomaly_types(reports[0])


def test_malformed_geometry_tag_is_indeterminate(zip_path):
    dss = _regular_series()
    dss[3].ImageOrientationPatient = [1.0, 0.0]  # 分量不足
    reports = geometry.check_zip_geometry(zip_path(dss), SPACING, TOL)
    assert reports[0].status == SERIES_INDETERMINATE
    assert ANOM_MISSING_TAG in _anomaly_types(reports[0])


def test_generated_no_tag_study(tmp_path):
    meta = generate_defect_study(tmp_path, 5, "no_tag")
    dss = [pydicom.dcmread(str(p))
           for p in sorted(Path(meta["directory"]).glob("*.dcm"))]
    reports = geometry.check_zip_geometry(
        _zip_from_datasets(tmp_path / "nt.zip", dss), SPACING, TOL)
    assert reports[0].status == SERIES_INDETERMINATE
    assert ANOM_MISSING_TAG in _anomaly_types(reports[0])


# ============================================================== 不足两片
def test_single_slice_is_indeterminate(zip_path):
    reports = geometry.check_zip_geometry(
        zip_path(_regular_series(n=1)), SPACING, TOL)
    r = reports[0]
    assert r.status == SERIES_INDETERMINATE
    assert ANOM_TOO_FEW in _anomaly_types(r)


def test_generated_single_study(tmp_path):
    meta = generate_defect_study(tmp_path, 6, "single")
    dss = [pydicom.dcmread(str(p))
           for p in sorted(Path(meta["directory"]).glob("*.dcm"))]
    assert len(dss) == 1
    reports = geometry.check_zip_geometry(
        _zip_from_datasets(tmp_path / "single.zip", dss), SPACING, TOL)
    assert reports[0].status == SERIES_INDETERMINATE
    assert ANOM_TOO_FEW in _anomaly_types(reports[0])


# ============================================================== 多序列分组
def test_multiple_series_grouped_and_all_valid(zip_path):
    dss_a = _regular_series("1.2.3.8")
    dss_b = _regular_series("1.2.3.7")
    for i, ds in enumerate(dss_b):
        ds.SOPInstanceUID = f"1.2.5.{i}"  # 保证跨系列 SOP 唯一
    reports = geometry.check_zip_geometry(
        zip_path(dss_a + dss_b), SPACING, TOL)
    assert len(reports) == 2
    assert {r.series_instance_uid for r in reports} == {"1.2.3.8", "1.2.3.7"}
    assert all(r.status == SERIES_VALID for r in reports)


def test_one_bad_series_marks_batch_invalid(zip_path):
    good = _regular_series("1.2.3.8")
    bad = _regular_series("1.2.3.7")
    for i, ds in enumerate(bad):
        ds.SOPInstanceUID = f"1.2.5.{i}"
    del bad[3].ImagePositionPatient  # 坏序列无法判定
    reports = geometry.check_zip_geometry(
        zip_path(good + bad), SPACING, TOL)
    statuses = {r.series_instance_uid: r.status for r in reports}
    assert statuses["1.2.3.8"] == SERIES_VALID
    assert statuses["1.2.3.7"] == SERIES_INDETERMINATE
    # 契约：存在无法判定序列时，整批绝不能判合格（由 API 层聚合，
    # 这里验证至少没有任何报告被误判为 VALID 的坏序列）
    assert not any(
        r.status == SERIES_VALID and ANOM_MISSING_TAG in _anomaly_types(r)
        for r in reports
    )


def test_generated_normal_study_valid(tmp_path):
    meta = generate_study(tmp_path, 1, 6)
    dss = [pydicom.dcmread(str(p))
           for p in sorted(Path(meta["directory"]).glob("*.dcm"))]
    reports = geometry.check_zip_geometry(
        _zip_from_datasets(tmp_path / "ok.zip", dss), SPACING, TOL)
    assert len(reports) == 1
    assert reports[0].status == SERIES_VALID
    assert reports[0].slice_count == 6


# ============================================================== 扫描失败
def test_missing_zip_raises_scan_error(tmp_path):
    with pytest.raises(ScanError) as exc:
        geometry.check_zip_geometry(tmp_path / "nope.zip", SPACING, TOL)
    assert exc.value.code == ERR_ZIP_MISSING


def test_corrupt_dicom_entry_raises_scan_error(tmp_path):
    path = tmp_path / "corrupt.zip"
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("dicom/0001.dcm", b"not a dicom file at all")
        zf.writestr("MANIFEST.csv", "x\n")
    with pytest.raises(ScanError) as exc:
        geometry.check_zip_geometry(path, SPACING, TOL)
    assert exc.value.code == ERR_ENTRY_CORRUPT
    assert exc.value.entry == "dicom/0001.dcm"


def test_broken_zip_structure_raises_scan_error(tmp_path):
    path = tmp_path / "broken.zip"
    path.write_bytes(b"PK\x03\x04 broken content")
    with pytest.raises(ScanError):
        geometry.check_zip_geometry(path, SPACING, TOL)


def test_compressed_stream_tamper_raises_scan_error(tmp_path):
    """直接翻转 ZIP 压缩流字节：解压/CRC 失败必须归为 CORRUPT_ENTRY。"""
    import struct

    path = _zip_from_datasets(tmp_path / "tamper.zip", _regular_series(n=2))
    blob = bytearray(path.read_bytes())
    idx = blob.find(b"PK\x03\x04")
    name_len = struct.unpack("<H", blob[idx + 26:idx + 28])[0]
    extra_len = struct.unpack("<H", blob[idx + 28:idx + 30])[0]
    data_start = idx + 30 + name_len + extra_len
    blob[data_start + 5] ^= 0xFF
    path.write_bytes(blob)

    with pytest.raises(ScanError) as exc:
        geometry.check_zip_geometry(path, SPACING, TOL)
    assert exc.value.code == ERR_ENTRY_CORRUPT


def test_zip_replaced_during_scan(tmp_path, monkeypatch):
    """扫描途中替换 ZIP：摘要复核必须失败，禁止用已读切片给结论。"""
    path = _zip_from_datasets(tmp_path / "swap.zip", _regular_series())
    real_zipfile = geometry.zipfile.ZipFile
    swapping = {"armed": True}

    class _SwappingZipFile(real_zipfile):
        def close(self):
            super().close()
            if swapping["armed"]:
                swapping["armed"] = False  # 替换动作自身不得再触发副作用
                # 条目全部读完、关闭句柄后立刻替换为另一份 ZIP
                _zip_from_datasets(path, _regular_series(n=3))

    monkeypatch.setattr(geometry.zipfile, "ZipFile", _SwappingZipFile)
    with pytest.raises(ScanError) as exc:
        geometry.check_zip_geometry(path, SPACING, TOL)
    assert exc.value.code == ERR_ZIP_REPLACED


def test_zip_deleted_during_scan(tmp_path, monkeypatch):
    path = _zip_from_datasets(tmp_path / "del.zip", _regular_series())
    real_zipfile = geometry.zipfile.ZipFile
    deleting = {"armed": True}

    class _DeletingZipFile(real_zipfile):
        def close(self):
            super().close()
            if deleting["armed"]:
                deleting["armed"] = False
                # 模拟扫描结束前 ZIP 被移走
                path.unlink()

    monkeypatch.setattr(geometry.zipfile, "ZipFile", _DeletingZipFile)
    with pytest.raises(ScanError) as exc:
        geometry.check_zip_geometry(path, SPACING, TOL)
    assert exc.value.code == ERR_ZIP_MISSING


# ============================================================== 输入边界
def test_zero_tolerance_still_detects_small_deviation(zip_path):
    dss = _regular_series()
    dss[2].ImagePositionPatient = [0.0, 0.0, 2 * SPACING + 0.1]
    reports = geometry.check_zip_geometry(zip_path(dss), SPACING, 0.0)
    assert reports[0].status == SERIES_INVALID
    assert ANOM_SPACING in _anomaly_types(reports[0])


def test_reverse_order_slices_sorted_by_projection(zip_path):
    # ZIP 内顺序颠倒不应影响判定：投影后统一排序
    dss = list(reversed(_regular_series()))
    reports = geometry.check_zip_geometry(zip_path(dss), SPACING, TOL)
    r = reports[0]
    assert r.status == SERIES_VALID
    assert r.positions == sorted(r.positions)


def test_missing_series_uid_is_indeterminate(zip_path):
    dss = _regular_series()
    del dss[3].SeriesInstanceUID  # 无法可靠分组，不得判合格
    reports = geometry.check_zip_geometry(zip_path(dss), SPACING, TOL)
    unknown = [r for r in reports
               if r.series_instance_uid.startswith("__UNKNOWN_SERIES_")]
    assert len(unknown) == 1
    assert unknown[0].status == SERIES_INDETERMINATE
    assert ANOM_MISSING_TAG in _anomaly_types(unknown[0])
    # 其余 5 片仍按原序列分组；抽走一片形成 2 倍层距缺口 -> INVALID
    normal = [r for r in reports if not r.series_instance_uid.startswith("__")]
    assert len(normal) == 1
    assert normal[0].slice_count == 5
    assert normal[0].status == SERIES_INVALID
    assert ANOM_MISSING_SLICE in _anomaly_types(normal[0])
