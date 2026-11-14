"""ZIP 内 CT 切片几何检查（离线体数据重建前的可靠性判定）。

输入是已完成批打包出的 ZIP（其中只应含非压缩 CT）。本模块：

1. 读取 ZIP 内全部 DICOM 条目，按 ``SeriesInstanceUID`` 分组；
2. 用每片的 :data:`ImageOrientationPatient` 求行/列方向叉积得到切片法向；
3. 把 :data:`ImagePositionPatient` 投影到法向，得到每片沿扫描轴的位置；
4. 逐序列报告：
   * 重层（duplicate slice：投影位置重合）
   * 缺层（missing slice：相邻层位置约为期望层距整数倍且倍数 >= 2）
   * 层距异常（spacing anomaly：层距与期望层距偏差超出容差，且不构成整倍缺层）
   * 方向不一致（orientation inconsistent：同序列 IOP 不一致）
   * 几何标签缺失（缺少/无法解析 IOP 或 IPP）

判定规则（对外契约）
====================
* 每个序列 **至少两片且全部检查无异常**，整批才可判为 ``usable``；
* 标签缺失或不足两片的序列只能给出 ``INDETERMINATE``（无法判定），不得报合格；
* 扫描过程中 ZIP 缺失、被替换或任一条目损坏，抛 :class:`ScanError`，
  调用方只能给出“不可用/无法判定”的结论，**不得用已读切片给出合格结论**。

纯函数式实现，不接触网络与数据库，便于单元测试。
"""
from __future__ import annotations

import hashlib
import io
import logging
import math
import zipfile
import zlib
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pydicom
from pydicom.dataelem import DataElement

log = logging.getLogger("geometry")

# 几何标签
TAG_IOP = "ImageOrientationPatient"   # 6 个分量：行方向、列方向
TAG_IPP = "ImagePositionPatient"      # 3 个分量：切片左上角患者坐标

# 序列级判定
SERIES_VALID = "VALID"                  # 至少两片且无任何异常
SERIES_INVALID = "INVALID"              # 发现明确几何异常
SERIES_INDETERMINATE = "INDETERMINATE"  # 标签缺失 / 不足两片，无法判定

# 异常类型代码（对外稳定契约）
ANOM_MISSING_TAG = "MISSING_GEOMETRY_TAG"
ANOM_ORIENTATION = "ORIENTATION_INCONSISTENT"
ANOM_DUPLICATE = "DUPLICATE_SLICE"
ANOM_MISSING_SLICE = "MISSING_SLICE"
ANOM_SPACING = "SPACING_ANOMALY"
ANOM_TOO_FEW = "NOT_ENOUGH_SLICES"

# 扫描错误代码
ERR_ZIP_MISSING = "ZIP_MISSING"
ERR_ZIP_REPLACED = "ZIP_REPLACED"
ERR_ZIP_STRUCTURE = "ZIP_STRUCTURE_ERROR"
ERR_ENTRY_CORRUPT = "CORRUPT_ENTRY"

# 浮点比较阈值：两片投影位置差小于期望层距的该比例即视为重层
_DUP_EPS_RATIO = 0.25
# IOP 逐分量一致性阈值（DICOM 典型值为 0/±1，留足数值余量）
_ORIENT_EPS = 1e-4
# 法向模长低于该值说明行/列方向退化，无法确定法向
_NORMAL_MIN_NORM = 1e-6


class GeometryError(Exception):
    """几何检查相关错误基类。"""


class ScanError(GeometryError):
    """扫描未能完整读取 ZIP（缺失/被替换/结构损坏/条目损坏）。

    携带稳定的机器可读错误码，调用方必须按“不可用、无法判定”处理。
    """

    def __init__(self, code: str, message: str, *, entry: str | None = None):
        super().__init__(message)
        self.code = code
        self.entry = entry


# ============================================================ 数据结构
@dataclass
class SliceInfo:
    """单个 DICOM 条目的几何输入。"""

    entry_name: str
    sop_instance_uid: str
    series_instance_uid: str
    orientation: tuple[float, ...] | None      # IOP 6 分量，缺失为 None
    position: tuple[float, ...] | None         # IPP 3 分量，缺失为 None
    orientation_parse_error: str | None = None
    position_parse_error: str | None = None


@dataclass
class Anomaly:
    type: str
    message: str
    entry_name: str | None = None
    sop_instance_uid: str | None = None
    detail: dict[str, Any] = field(default_factory=dict)


@dataclass
class SeriesReport:
    series_instance_uid: str
    slice_count: int
    status: str
    normal: list[float] | None
    positions: list[float]
    slice_spacings: list[float]
    min_spacing: float | None
    max_spacing: float | None
    mean_spacing: float | None
    anomalies: list[Anomaly]

    def to_dict(self) -> dict[str, Any]:
        return {
            "series_instance_uid": self.series_instance_uid,
            "slice_count": self.slice_count,
            "status": self.status,
            "normal": self.normal,
            "positions": [round(p, 6) for p in self.positions],
            "slice_spacings": [round(s, 6) for s in self.slice_spacings],
            "min_spacing": None if self.min_spacing is None
            else round(self.min_spacing, 6),
            "max_spacing": None if self.max_spacing is None
            else round(self.max_spacing, 6),
            "mean_spacing": None if self.mean_spacing is None
            else round(self.mean_spacing, 6),
            "anomalies": [a.__dict__ for a in self.anomalies],
        }


# ============================================================ 向量运算
def _cross(a: tuple[float, ...], b: tuple[float, ...]) -> tuple[float, float, float]:
    return (
        a[1] * b[2] - a[2] * b[1],
        a[2] * b[0] - a[0] * b[2],
        a[0] * b[1] - a[1] * b[0],
    )


def _dot(a: tuple[float, ...], b: tuple[float, ...]) -> float:
    return sum(x * y for x, y in zip(a, b))


def _norm(a: tuple[float, ...]) -> float:
    return math.sqrt(sum(x * x for x in a))


def _normal_from_orientation(
    iop: tuple[float, ...],
) -> tuple[float, float, float] | None:
    """由 IOP 的行方向、列方向叉积得到单位法向。"""
    row = iop[0:3]
    col = iop[3:6]
    normal = _cross(row, col)
    n = _norm(normal)
    if n < _NORMAL_MIN_NORM:
        return None
    return (normal[0] / n, normal[1] / n, normal[2] / n)


def _as_float_tuple(value: Any, expected: int) -> tuple[float, ...]:
    """把 DICOM 多值元素转为定长浮点元组，非法时抛 ValueError。"""
    if isinstance(value, DataElement):
        value = value.value
    if value is None:
        raise ValueError("元素为空")
    # pydicom 的多值 VR 通常返回 MultiValue / list
    seq = list(value)
    if len(seq) != expected:
        raise ValueError(f"分量数应为 {expected}，实际 {len(seq)}")
    out = tuple(float(x) for x in seq)
    if any(math.isnan(x) or math.isinf(x) for x in out):
        raise ValueError("含 NaN/Inf，不是合法几何坐标")
    return out


# ============================================================ 单条解析
def parse_slice(entry_name: str, ds: pydicom.Dataset) -> SliceInfo:
    """从已解析的 DICOM 数据集中提取几何输入；标签缺失/非法不抛，记录在字段里。"""
    series_uid = str(getattr(ds, "SeriesInstanceUID", "") or "").strip()
    sop_uid = str(getattr(ds, "SOPInstanceUID", "") or "").strip()

    orientation: tuple[float, ...] | None = None
    position: tuple[float, ...] | None = None
    iop_err: str | None = None
    ipp_err: str | None = None

    if not getattr(ds, TAG_IOP, None):
        iop_err = "缺少 ImageOrientationPatient"
    else:
        try:
            orientation = _as_float_tuple(getattr(ds, TAG_IOP), 6)
        except (TypeError, ValueError) as exc:
            iop_err = f"ImageOrientationPatient 无法解析：{exc}"

    if not getattr(ds, TAG_IPP, None):
        ipp_err = "缺少 ImagePositionPatient"
    else:
        try:
            position = _as_float_tuple(getattr(ds, TAG_IPP), 3)
        except (TypeError, ValueError) as exc:
            ipp_err = f"ImagePositionPatient 无法解析：{exc}"

    return SliceInfo(
        entry_name=entry_name,
        sop_instance_uid=sop_uid,
        series_instance_uid=series_uid,
        orientation=orientation,
        position=position,
        orientation_parse_error=iop_err,
        position_parse_error=ipp_err,
    )


# ============================================================ ZIP 扫描
def _zip_digest(path: Path) -> str:
    """对 ZIP 字节做 SHA-256，用于发现扫描途中被替换。"""
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(8 * 1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def read_slices_from_zip(zip_path: str | Path) -> list[SliceInfo]:
    """完整读取 ZIP 内全部 DICOM 条目并解析几何标签。

    任何不完整情况都抛 :class:`ScanError`：

    * 文件不存在 -> ``ZIP_MISSING``
    * 扫描途中被替换（摘要变化）/大小变化 -> ``ZIP_REPLACED``
    * ZIP 结构损坏 -> ``ZIP_STRUCTURE_ERROR``
    * 单个 DICOM 条目无法解析 -> ``CORRUPT_ENTRY``
    """
    path = Path(zip_path)
    if not path.exists():
        raise ScanError(ERR_ZIP_MISSING, f"ZIP 文件缺失：{path}")

    digest_before = _zip_digest(path)
    size_before = path.stat().st_size
    slices: list[SliceInfo] = []

    try:
        zf = zipfile.ZipFile(path, "r")
    except zipfile.BadZipFile as exc:
        raise ScanError(ERR_ZIP_STRUCTURE, f"ZIP 结构损坏，无法打开：{exc}") from exc

    try:
        names = sorted(
            n for n in zf.namelist()
            if not n.endswith("/") and n != "MANIFEST.csv"
        )
        for name in names:
            try:
                # zf.read 会在解压同时校验 CRC，坏条目抛 BadZipFile；
                # 压缩流本身损坏可能抛 zlib.error，统一按条目损坏处理，
                # 无需先 testzip() 再读一遍（避免对大 ZIP 双重 I/O）
                raw = zf.read(name)
            except (zipfile.BadZipFile, OSError, RuntimeError, zlib.error) as exc:
                # RuntimeError：加密条目/压缩层损坏等
                raise ScanError(
                    ERR_ENTRY_CORRUPT,
                    f"条目无法读取：{name}：{exc}",
                    entry=name,
                ) from exc
            try:
                ds = pydicom.dcmread(io.BytesIO(raw))
            except Exception as exc:  # pydicom 对坏文件抛多种异常，统一按损坏处理
                raise ScanError(
                    ERR_ENTRY_CORRUPT,
                    f"条目不是合法 DICOM：{name}：{exc}",
                    entry=name,
                ) from exc
            slices.append(parse_slice(name, ds))
    except ScanError:
        raise
    except (zipfile.BadZipFile, OSError, zlib.error) as exc:
        raise ScanError(
            ERR_ZIP_STRUCTURE, f"读取 ZIP 过程中结构损坏：{exc}"
        ) from exc
    finally:
        zf.close()

    # 扫描结束后复核：摘要或大小变化说明文件在扫描途中被替换/截断
    if not path.exists():
        raise ScanError(ERR_ZIP_MISSING, "扫描结束时 ZIP 已消失（可能被替换）")
    if path.stat().st_size != size_before or _zip_digest(path) != digest_before:
        raise ScanError(
            ERR_ZIP_REPLACED,
            "ZIP 在扫描过程中被替换或修改，本次读取结果不可信",
        )
    return slices


# ============================================================ 序列判定
def check_series(
    series_uid: str,
    slices: list[SliceInfo],
    expected_spacing: float,
    tolerance: float,
) -> SeriesReport:
    """对单个序列做几何检查。

    ``expected_spacing`` 为期望层距（mm，正值），``tolerance`` 为允许的
    绝对偏差（mm，非负）。
    """
    anomalies: list[Anomaly] = []
    n = len(slices)

    # ---- 1) 几何标签缺失 / 无法解析：逐片登记，序列只能无法判定 ----
    bad_tag_sops: list[str] = []
    usable: list[SliceInfo] = []
    for sl in slices:
        problems = [p for p in (sl.orientation_parse_error,
                                sl.position_parse_error) if p]
        if problems:
            bad_tag_sops.append(sl.sop_instance_uid or sl.entry_name)
            anomalies.append(Anomaly(
                type=ANOM_MISSING_TAG,
                message="；".join(problems),
                entry_name=sl.entry_name,
                sop_instance_uid=sl.sop_instance_uid or None,
                detail={"series_instance_uid": series_uid},
            ))
        else:
            usable.append(sl)

    # ---- 2) 不足两片：无法判定，不得报合格 ----
    if n < 2:
        anomalies.append(Anomaly(
            type=ANOM_TOO_FEW,
            message=f"序列仅 {n} 片，至少需要 2 片才能判定几何一致性",
            detail={"series_instance_uid": series_uid, "slice_count": n},
        ))
        return SeriesReport(
            series_instance_uid=series_uid, slice_count=n,
            status=SERIES_INDETERMINATE, normal=None, positions=[],
            slice_spacings=[], min_spacing=None, max_spacing=None,
            mean_spacing=None, anomalies=anomalies,
        )

    if bad_tag_sops:
        # 有片可读也不能给合格：标签缺失即无法判定
        return SeriesReport(
            series_instance_uid=series_uid, slice_count=n,
            status=SERIES_INDETERMINATE, normal=None,
            positions=[], slice_spacings=[],
            min_spacing=None, max_spacing=None, mean_spacing=None,
            anomalies=anomalies,
        )

    # ---- 3) 方向一致性：以首片 IOP 为基准，逐片比较 ----
    ref = usable[0]
    ref_iop = ref.orientation
    assert ref_iop is not None
    normal = _normal_from_orientation(ref_iop)
    if normal is None:
        anomalies.append(Anomaly(
            type=ANOM_MISSING_TAG,
            message="ImageOrientationPatient 行/列方向退化，无法确定切片法向",
            entry_name=ref.entry_name,
            sop_instance_uid=ref.sop_instance_uid or None,
            detail={"series_instance_uid": series_uid},
        ))
        return SeriesReport(
            series_instance_uid=series_uid, slice_count=n,
            status=SERIES_INDETERMINATE, normal=None, positions=[],
            slice_spacings=[], min_spacing=None, max_spacing=None,
            mean_spacing=None, anomalies=anomalies,
        )

    orientation_bad = False
    for sl in usable[1:]:
        iop = sl.orientation
        assert iop is not None
        if any(abs(a - b) > _ORIENT_EPS for a, b in zip(iop, ref_iop)):
            orientation_bad = True
            anomalies.append(Anomaly(
                type=ANOM_ORIENTATION,
                message=("ImageOrientationPatient 与序列基准不一致："
                         f"got={list(iop)} reference={list(ref_iop)}"),
                entry_name=sl.entry_name,
                sop_instance_uid=sl.sop_instance_uid or None,
                detail={
                    "series_instance_uid": series_uid,
                    "actual": list(iop),
                    "reference": list(ref_iop),
                },
            ))

    # ---- 4) 投影 IPP 到法向并排序 ----
    projected: list[tuple[float, SliceInfo]] = []
    for sl in usable:
        pos = sl.position
        assert pos is not None
        projected.append((_dot(pos, normal), sl))
    projected.sort(key=lambda item: item[0])
    positions = [p for p, _ in projected]

    # ---- 5) 重层 / 缺层 / 层距异常 ----
    spacings: list[float] = []
    dup_eps = max(expected_spacing * _DUP_EPS_RATIO, 1e-9)
    for (p_prev, sl_prev), (p_cur, sl_cur) in zip(projected, projected[1:]):
        gap = p_cur - p_prev
        spacings.append(gap)

        if abs(gap) <= dup_eps:
            anomalies.append(Anomaly(
                type=ANOM_DUPLICATE,
                message=(f"重层：{sl_prev.sop_instance_uid} 与 "
                         f"{sl_cur.sop_instance_uid} 投影位置重合，"
                         f"层距={gap:.4f}mm"),
                entry_name=sl_cur.entry_name,
                sop_instance_uid=sl_cur.sop_instance_uid or None,
                detail={
                    "series_instance_uid": series_uid,
                    "previous_sop": sl_prev.sop_instance_uid,
                    "gap_mm": round(gap, 6),
                },
            ))
            continue

        # 先判整倍缺层：即使容差很大、把 k 倍层距也纳入“单层偏差”范围，
        # 两个切片位置之间跨过多个标称层位仍是客观事实，必须优先报缺层，
        # 避免大容差把缺层静默吸收成合格。
        k = int(round(gap / expected_spacing))
        if k >= 2 and abs(gap - k * expected_spacing) <= tolerance:
            anomalies.append(Anomaly(
                type=ANOM_MISSING_SLICE,
                message=(f"缺层：{sl_prev.sop_instance_uid} 与 "
                         f"{sl_cur.sop_instance_uid} 之间层距 {gap:.4f}mm "
                         f"≈ {k} × 期望层距 {expected_spacing}mm，"
                         f"疑似缺失 {k - 1} 层"),
                entry_name=sl_cur.entry_name,
                sop_instance_uid=sl_cur.sop_instance_uid or None,
                detail={
                    "series_instance_uid": series_uid,
                    "previous_sop": sl_prev.sop_instance_uid,
                    "gap_mm": round(gap, 6),
                    "expected_multiple": k,
                    "missing_count_estimate": k - 1,
                },
            ))
        elif abs(gap - expected_spacing) <= tolerance:
            continue  # 正常层距
        else:
            anomalies.append(Anomaly(
                type=ANOM_SPACING,
                message=(f"层距异常：{sl_prev.sop_instance_uid} 与 "
                         f"{sl_cur.sop_instance_uid} 之间层距 {gap:.4f}mm，"
                         f"偏离期望 {expected_spacing}mm 超出容差 {tolerance}mm"),
                entry_name=sl_cur.entry_name,
                sop_instance_uid=sl_cur.sop_instance_uid or None,
                detail={
                    "series_instance_uid": series_uid,
                    "previous_sop": sl_prev.sop_instance_uid,
                    "gap_mm": round(gap, 6),
                    "expected_spacing_mm": expected_spacing,
                    "tolerance_mm": tolerance,
                    "deviation_mm": round(gap - expected_spacing, 6),
                },
            ))

    if spacings:
        min_s = min(spacings)
        max_s = max(spacings)
        mean_s = sum(spacings) / len(spacings)
    else:
        min_s = max_s = mean_s = None

    # ---- 6) 序列总判定 ----
    if orientation_bad or any(
        a.type in (ANOM_DUPLICATE, ANOM_MISSING_SLICE, ANOM_SPACING)
        for a in anomalies
    ):
        status = SERIES_INVALID
    else:
        status = SERIES_VALID

    return SeriesReport(
        series_instance_uid=series_uid,
        slice_count=n,
        status=status,
        normal=[round(x, 6) for x in normal],
        positions=positions,
        slice_spacings=spacings,
        min_spacing=min_s,
        max_spacing=max_s,
        mean_spacing=mean_s,
        anomalies=anomalies,
    )


def check_zip_geometry(
    zip_path: str | Path,
    expected_spacing: float,
    tolerance: float,
) -> list[SeriesReport]:
    """读取 ZIP 并逐序列给出几何报告（按 SeriesInstanceUID 分组）。

    扫描不完整时抛 :class:`ScanError`，调用方不得据此给出合格结论。
    """
    slices = read_slices_from_zip(zip_path)

    grouped: dict[str, list[SliceInfo]] = defaultdict(list)
    unknown_series = 0
    unknown_uids: set[str] = set()
    for sl in slices:
        if not sl.series_instance_uid:
            # 缺少 SeriesInstanceUID 无法可靠分组：赋独立键并标记，
            # 该“序列”在 check_series 中必然走 INDETERMINATE，绝不可能误判合格
            sl.series_instance_uid = (
                f"__UNKNOWN_SERIES_{unknown_series}__"
            )
            unknown_uids.add(sl.series_instance_uid)
            unknown_series += 1
        grouped[sl.series_instance_uid].append(sl)

    reports = [
        check_series(uid, grouped[uid], expected_spacing, tolerance)
        for uid in sorted(grouped)
    ]
    for report in reports:
        if report.series_instance_uid in unknown_uids:
            report.anomalies.insert(0, Anomaly(
                type=ANOM_MISSING_TAG,
                message="缺少 SeriesInstanceUID，无法按序列可靠分组",
                detail={"series_instance_uid": report.series_instance_uid},
            ))
            report.status = SERIES_INDETERMINATE
            report.normal = None
            report.positions = []
            report.slice_spacings = []
            report.min_spacing = report.max_spacing = report.mean_spacing = None
    log.info(
        "几何检查完成：zip=%s 序列数=%d 状态=%s",
        zip_path, len(reports), [r.status for r in reports],
    )
    return reports
