"""已完成批 ZIP 的切片几何检查。

影像科研数据员把检查包交给离线分割前，需要确认每个序列都能可靠重建
体数据。本模块读取已完成批的 ZIP（只含非压缩 CT），按 SeriesInstanceUID
分组，用 ImageOrientationPatient 叉积求法向、把 ImagePositionPatient 投影到
法向上得到层位，逐序列报告：

* ``duplicate_slices``        重层（相邻层位差在容差内，同一位置出现多片）
* ``missing_slices``          缺层（相邻层位差约为期望层距的整数倍 ≥2）
* ``irregular_spacing``       层距异常（偏差超容差，但既非重层也非整倍缺层）
* ``inconsistent_orientation``方向不一致（法向与序列参考法向夹角超容差）
* ``missing_geometry_tags``   几何标签缺失（IOP/IPP 缺失或非法）

判定规则
========
* 序列切片 < 2 片或几何标签缺失 → 该序列 ``undetermined``（无法判定），
  不得报合格；
* 序列存在上述任一异常 → ``anomalous``；
* 全部序列 ``ok`` → 整批 ``usable``；
* ZIP 缺失、损坏、条目不可读、内容与批登记清单不符（可能被替换）→
  不得依据已读切片给出合格结论，整批一律 ``undetermined``。
"""
from __future__ import annotations

import io
import logging
import math
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pydicom
from pynetdicom.sop_class import CTImageStorage

from .storage import UNCOMPRESSED_TS

log = logging.getLogger("geometry")

# 法向量点积容差：|dot| 低于 1-该值 视为方向不一致
ORIENTATION_DOT_TOLERANCE = 1e-3

# 序列状态
STATUS_OK = "ok"
STATUS_ANOMALOUS = "anomalous"
STATUS_UNDETERMINED = "undetermined"

# 整批结论
CONCLUSION_USABLE = "usable"
CONCLUSION_UNUSABLE = "unusable"
CONCLUSION_UNDETERMINED = "undetermined"

# 问题类型
ISSUE_DUPLICATE = "duplicate_slices"
ISSUE_MISSING = "missing_slices"
ISSUE_IRREGULAR = "irregular_spacing"
ISSUE_ORIENTATION = "inconsistent_orientation"
ISSUE_MISSING_TAGS = "missing_geometry_tags"


@dataclass
class SliceGeom:
    """单片的定位信息（iop/ipp 为 None 表示标签缺失或非法）。"""

    sop_uid: str
    file_name: str
    iop: list[float] | None
    ipp: list[float] | None
    normal: list[float] | None = None   # 统一到参考朝向后的单位法向
    pos: float | None = None            # 在参考法向上的投影层位（mm）


# ------------------------------------------------------------------ 向量工具
def _cross(a: list[float], b: list[float]) -> list[float]:
    return [
        a[1] * b[2] - a[2] * b[1],
        a[2] * b[0] - a[0] * b[2],
        a[0] * b[1] - a[1] * b[0],
    ]


def _dot(a: list[float], b: list[float]) -> float:
    return sum(x * y for x, y in zip(a, b))


def _floats(value: Any, length: int) -> list[float] | None:
    """把 DICOM 多值元素转为定长 float 列表；缺失/非法返回 None。"""
    if value is None:
        return None
    try:
        out = [float(v) for v in value]
    except (TypeError, ValueError):
        return None
    if len(out) != length or any(not math.isfinite(v) for v in out):
        return None
    return out


# ------------------------------------------------------------------ 序列检查
def _series_report(
    series_uid: str,
    slices: list[SliceGeom],
    expected_spacing: float,
    tolerance: float,
) -> dict[str, Any]:
    report: dict[str, Any] = {
        "series_instance_uid": series_uid,
        "slice_count": len(slices),
        "status": STATUS_OK,
        "reason": "",
        "issues": [],
        "normal": None,
        "spacings": None,
    }

    # 1) 几何标签缺失 → 无法判定（不得报合格）
    missing = [
        s for s in slices
        if s.iop is None or s.ipp is None
    ]
    if missing:
        report["status"] = STATUS_UNDETERMINED
        report["reason"] = (
            f"{len(missing)} 片缺失 ImageOrientationPatient/ImagePositionPatient "
            "或标签非法，无法判定几何"
        )
        report["issues"].append({
            "kind": ISSUE_MISSING_TAGS,
            "detail": report["reason"],
            "sop_instance_uids": [s.sop_uid for s in missing],
            "positions": [],
        })
        return report

    # 2) 不足两片 → 无法判定层距
    if len(slices) < 2:
        report["status"] = STATUS_UNDETERMINED
        report["reason"] = (
            f"序列仅 {len(slices)} 片（少于 2 片），无法判定层距"
        )
        return report

    # 3) 法向：IOP 行/列方向叉积；零向量视为标签非法
    normals: list[list[float]] = []
    for s in slices:
        n = _cross(s.iop[:3], s.iop[3:])
        length = math.sqrt(_dot(n, n))
        if length == 0.0:
            report["status"] = STATUS_UNDETERMINED
            report["reason"] = (
                f"实例 {s.sop_uid} 的 ImageOrientationPatient 为零向量，"
                "无法确定法向"
            )
            report["issues"].append({
                "kind": ISSUE_MISSING_TAGS,
                "detail": report["reason"],
                "sop_instance_uids": [s.sop_uid],
                "positions": [],
            })
            return report
        normals.append([v / length for v in n])

    # 4) 方向一致性：以首片为参考，反向的翻正，夹角超差的报不一致
    ref = normals[0]
    inconsistent: list[str] = []
    for s, n in zip(slices, normals):
        d = _dot(ref, n)
        if d < 0:
            n = [-v for v in n]
            d = -d
        if d < 1.0 - ORIENTATION_DOT_TOLERANCE:
            inconsistent.append(s.sop_uid)
        s.normal = n
    if inconsistent:
        report["status"] = STATUS_ANOMALOUS
        report["issues"].append({
            "kind": ISSUE_ORIENTATION,
            "detail": (
                f"{len(inconsistent)} 片法向与序列参考方向不一致，"
                "体数据无法按同一坐标系重建"
            ),
            "sop_instance_uids": inconsistent,
            "positions": [],
        })
        return report
    report["normal"] = [round(v, 6) for v in ref]

    # 5) 投影层位并排序，逐相邻对分类
    for s in slices:
        s.pos = _dot(s.ipp, s.normal)
    ordered = sorted(slices, key=lambda s: s.pos)

    spacings: list[float] = []
    for a, b in zip(ordered, ordered[1:]):
        gap = b.pos - a.pos
        spacings.append(round(gap, 6))

        if gap <= tolerance:
            # 同一位置（容差内）出现两片 → 重层
            report["issues"].append({
                "kind": ISSUE_DUPLICATE,
                "detail": (
                    f"层位 {a.pos:.3f}mm 与 {b.pos:.3f}mm 间距 {gap:.3f}mm "
                    f"不超过容差 {tolerance}mm，判为重层"
                ),
                "sop_instance_uids": [a.sop_uid, b.sop_uid],
                "positions": [round(a.pos, 6), round(b.pos, 6)],
            })
            continue

        ratio = gap / expected_spacing
        k = round(ratio)
        if k >= 2 and abs(gap - k * expected_spacing) <= tolerance:
            # 间距恰为期望层距的 k 倍 → 缺 k-1 层
            report["issues"].append({
                "kind": ISSUE_MISSING,
                "detail": (
                    f"层位 {a.pos:.3f}mm 到 {b.pos:.3f}mm 间距 {gap:.3f}mm "
                    f"≈ {k}×期望层距，估计缺 {k - 1} 层"
                ),
                "sop_instance_uids": [a.sop_uid, b.sop_uid],
                "positions": [round(a.pos, 6), round(b.pos, 6)],
            })
        elif abs(gap - expected_spacing) > tolerance:
            # 既非整倍缺层，也超出期望±容差 → 层距异常
            report["issues"].append({
                "kind": ISSUE_IRREGULAR,
                "detail": (
                    f"层位 {a.pos:.3f}mm 到 {b.pos:.3f}mm 层距 {gap:.3f}mm "
                    f"偏离期望 {expected_spacing}mm 超过容差 {tolerance}mm"
                ),
                "sop_instance_uids": [a.sop_uid, b.sop_uid],
                "positions": [round(a.pos, 6), round(b.pos, 6)],
            })
    report["spacings"] = spacings

    if report["issues"]:
        report["status"] = STATUS_ANOMALOUS
    return report


# ------------------------------------------------------------------ ZIP 检查
def inspect_zip_geometry(
    zip_path: Path | None,
    expected_spacing: float,
    tolerance: float,
    expected_sop_uids: set[str] | None = None,
) -> dict[str, Any]:
    """检查已完成批 ZIP 的切片几何，返回可 JSON 序列化的报告 dict。

    ``expected_sop_uids`` 为批登记的已收 SOP 集合；ZIP 内容与其不符时
    视为“ZIP 被替换/损坏”，结论一律 undetermined，不得报合格。
    """
    errors: list[str] = []
    series_map: dict[str, list[SliceGeom]] = {}

    if zip_path is None or not Path(zip_path).exists():
        errors.append(f"ZIP 文件缺失：{zip_path}（数据卷可能已损坏）")
    else:
        try:
            with zipfile.ZipFile(zip_path) as zf:
                names = sorted(
                    n for n in zf.namelist()
                    if n.startswith("dicom/") and not n.endswith("/")
                )
                if not names:
                    errors.append("ZIP 内没有 dicom/ 条目（ZIP 可能被替换）")
                for name in names:
                    try:
                        # read() 会校验 CRC，损坏条目在此抛 BadZipFile
                        raw = zf.read(name)
                        ds = pydicom.dcmread(
                            io.BytesIO(raw), stop_before_pixels=True
                        )
                    except Exception as exc:
                        errors.append(f"ZIP 条目 {name} 读取失败：{exc}")
                        continue
                    file_meta = getattr(ds, "file_meta", None)
                    ts = str(getattr(file_meta, "TransferSyntaxUID", "") or "")
                    if ts not in UNCOMPRESSED_TS:
                        errors.append(
                            f"条目 {name} 传输语法 {ts or '缺失'} 非未压缩，"
                            "ZIP 可能被替换"
                        )
                        continue
                    if str(getattr(ds, "SOPClassUID", "") or "") != str(CTImageStorage):
                        errors.append(
                            f"条目 {name} 不是 CT Image Storage，ZIP 可能被替换"
                        )
                        continue
                    sop = str(getattr(ds, "SOPInstanceUID", "") or "")
                    series = str(getattr(ds, "SeriesInstanceUID", "") or "")
                    if not sop or not series:
                        errors.append(
                            f"条目 {name} 缺少 SOPInstanceUID/SeriesInstanceUID"
                        )
                        continue
                    series_map.setdefault(series, []).append(SliceGeom(
                        sop_uid=sop,
                        file_name=name,
                        iop=_floats(
                            getattr(ds, "ImageOrientationPatient", None), 6
                        ),
                        ipp=_floats(
                            getattr(ds, "ImagePositionPatient", None), 3
                        ),
                    ))
        except (zipfile.BadZipFile, OSError) as exc:
            errors.append(f"ZIP 无法打开或已损坏：{exc}")

    # 清单一致性：ZIP 被整体替换时 SOP 集合必然对不上登记清单
    if expected_sop_uids is not None and not errors:
        found = {s.sop_uid for slices in series_map.values() for s in slices}
        if found != set(expected_sop_uids):
            extra = sorted(found - set(expected_sop_uids))[:3]
            missing = sorted(set(expected_sop_uids) - found)[:3]
            errors.append(
                "ZIP 内容与批登记清单不一致（可能被替换）："
                f"多出 {extra or '无'}，缺少 {missing or '无'}"
            )

    series_reports = [
        _series_report(uid, slices, expected_spacing, tolerance)
        for uid, slices in sorted(series_map.items())
    ]

    if errors:
        conclusion = CONCLUSION_UNDETERMINED
    elif not series_reports:
        errors.append("ZIP 内未读到任何有效 CT 切片，无法判定")
        conclusion = CONCLUSION_UNDETERMINED
    elif any(r["status"] == STATUS_UNDETERMINED for r in series_reports):
        conclusion = CONCLUSION_UNDETERMINED
    elif any(r["status"] == STATUS_ANOMALOUS for r in series_reports):
        conclusion = CONCLUSION_UNUSABLE
    else:
        conclusion = CONCLUSION_USABLE

    return {
        "conclusion": conclusion,
        "usable": conclusion == CONCLUSION_USABLE,
        "errors": errors,
        "series_count": len(series_reports),
        "slice_count": sum(r["slice_count"] for r in series_reports),
        "series": series_reports,
    }
