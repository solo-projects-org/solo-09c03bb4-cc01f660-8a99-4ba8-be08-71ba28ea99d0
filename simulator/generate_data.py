"""生成完全虚构的合成 CT 数据集（无任何真实患者信息）。

每个实例都是合法的 DICOM CT Image Storage 对象，未压缩传输语法，
可被 pydicom/pynetdicom 直接读写与网络传输。

除正常检查外，还可生成带切片几何缺陷的检查，供几何检查 API 演示：

* ``missing``     缺层（中间整层缺失，相邻层位置相差 2 倍层距）
* ``duplicate``   重层（两片 ImagePositionPatient 完全相同）
* ``spacing``     层距异常（某一层距既不等于期望层距，也不构成整倍缺层）
* ``orientation`` 方向不一致（某片 ImageOrientationPatient 被翻转）
* ``no_tag``      几何标签缺失（某片缺少 IOP/IPP，无法判定，不得报合格）
* ``single``      仅一片（不足两片，无法判定，不得报合格）
"""
from __future__ import annotations

import argparse
import math
from pathlib import Path

import pydicom
from pydicom.dataset import Dataset, FileMetaDataset
from pydicom.uid import (
    CTImageStorage,
    ExplicitVRLittleEndian,
    generate_uid,
)

# 固定 UID 前缀，保证演示环境内 UID 稳定可读
UID_PREFIX = "1.2.826.0.1.3680043.8.498."
PATIENT_NAMES = {
    1: "PHANTOM^Alpha",
    2: "PHANTOM^Bravo",
    3: "PHANTOM^Charlie",
}

# 缺陷检查使用 8xxx 序列号段，避免与正常检查（1xxx/2xxx/3xxx）冲突
DEFECT_BASE_SERIAL = 8000
# 全部可演示的缺陷类型（顺序即演示顺序）
DEFECT_TYPES = ("missing", "duplicate", "spacing",
                "orientation", "no_tag", "single")
# 合成 CT 的标称层距（mm），几何检查请求按此值传参
NOMINAL_SLICE_SPACING = 2.0


def _make_uid(serial: int) -> str:
    return f"{UID_PREFIX}{serial}"


def build_ct_dataset(
    study_uid: str,
    series_uid: str,
    sop_uid: str,
    patient_id: str,
    patient_name: str,
    instance_number: int,
    instance_count: int,
    rows: int = 64,
    cols: int = 64,
) -> Dataset:
    """构造一个内存中的合成 CT Dataset（带 file_meta）。"""
    file_meta = FileMetaDataset()
    file_meta.MediaStorageSOPClassUID = str(CTImageStorage)
    file_meta.MediaStorageSOPInstanceUID = sop_uid
    file_meta.TransferSyntaxUID = str(ExplicitVRLittleEndian)
    file_meta.ImplementationClassUID = _make_uid(9900)
    file_meta.ImplementationVersionName = "SIMPACS_1"

    ds = Dataset()
    ds.file_meta = file_meta
    ds.preamble = b"\0" * 128

    ds.SOPClassUID = str(CTImageStorage)
    ds.SOPInstanceUID = sop_uid
    ds.StudyInstanceUID = study_uid
    ds.SeriesInstanceUID = series_uid

    ds.PatientName = patient_name
    ds.PatientID = patient_id
    ds.PatientBirthDate = "19700101"
    ds.PatientSex = "O"

    ds.Modality = "CT"
    ds.StudyDate = "20261101"
    ds.StudyTime = "090000"
    ds.ContentDate = "20261101"
    minute = (instance_number - 1) % 60
    ds.ContentTime = f"09{minute:02d}00"
    ds.AccessionNumber = f"SIM{patient_id}"
    ds.StudyID = f"S{patient_id}"
    ds.StudyDescription = f"Synthetic CT demo study {patient_id}"
    ds.SeriesNumber = 1
    ds.SeriesDescription = "Synthetic axial CT"
    ds.InstanceNumber = instance_number

    ds.ImageType = ["ORIGINAL", "PRIMARY", "AXIAL"]
    ds.PhotometricInterpretation = "MONOCHROME2"
    ds.SamplesPerPixel = 1
    ds.BitsAllocated = 16
    ds.BitsStored = 16
    ds.HighBit = 15
    ds.PixelRepresentation = 0
    ds.Rows = rows
    ds.Columns = cols
    ds.PixelSpacing = [1.0, 1.0]
    ds.SliceThickness = 2.0
    ds.SliceLocation = (instance_number - 1 - instance_count / 2) * 2.0
    ds.ImagePositionPatient = [0.0, 0.0, float(ds.SliceLocation)]
    ds.ImageOrientationPatient = [1.0, 0.0, 0.0, 0.0, 1.0, 0.0]
    ds.RescaleIntercept = -1024
    ds.RescaleSlope = 1
    ds.RescaleType = "HU"
    ds.DeviceSerialNumber = "SYNTH-001"
    ds.Manufacturer = "Synthetic PACS Demo"

    # 确定性的“模体”像素：径向渐变 + 层间变化，HU 值在常见 CT 范围内
    pixels = bytearray()
    cx = cy = (rows - 1) / 2.0
    for r in range(rows):
        for c in range(cols):
            dist = math.sqrt((r - cy) ** 2 + (c - cx) ** 2)
            hu = int(400 * math.exp(-(dist ** 2) / 200.0)
                     + 50 * math.sin(instance_number + dist / 4) - 200)
            hu = max(-1024, min(3071, hu))
            pixels += int(hu + 1024).to_bytes(2, "little", signed=False)
    ds.PixelData = bytes(pixels)
    return ds


def generate_study(
    out_dir: Path,
    study_index: int,
    instance_count: int,
) -> dict:
    """生成一个检查的全部实例文件，返回检查元数据。"""
    patient_id = f"P{study_index:03d}"
    study_uid = _make_uid(1000 + study_index)
    series_uid = _make_uid(2000 + study_index)
    study_dir = out_dir / study_uid
    study_dir.mkdir(parents=True, exist_ok=True)

    for i in range(1, instance_count + 1):
        sop_uid = _make_uid(3000 + study_index * 100 + i)
        ds = build_ct_dataset(
            study_uid=study_uid,
            series_uid=series_uid,
            sop_uid=sop_uid,
            patient_id=patient_id,
            patient_name=PATIENT_NAMES[study_index],
            instance_number=i,
            instance_count=instance_count,
        )
        ds.save_as(str(study_dir / f"{i:04d}.dcm"), enforce_file_format=True)

    return {
        "study_instance_uid": study_uid,
        "series_instance_uid": series_uid,
        "patient_id": patient_id,
        "patient_name": PATIENT_NAMES[study_index],
        "instances": instance_count,
        "directory": str(study_dir),
    }


def generate_defect_study(
    out_dir: Path,
    defect_index: int,
    defect_type: str,
    instance_count: int = 8,
) -> dict:
    """生成一个带指定切片几何缺陷的虚构 CT 检查。

    所有缺陷都落在几何标签上，像素数据仍为合法合成模体，
    使几何检查 API 能在真实 DICOM 网络流水中演示各类判定。
    """
    if defect_type not in DEFECT_TYPES:
        raise ValueError(f"未知缺陷类型：{defect_type}，支持 {DEFECT_TYPES}")

    serial = DEFECT_BASE_SERIAL + defect_index * 100
    patient_id = f"D{defect_index:03d}"
    study_uid = _make_uid(serial + 1)
    series_uid = _make_uid(serial + 2)
    study_dir = out_dir / study_uid
    study_dir.mkdir(parents=True, exist_ok=True)

    # single：只保留第 1 片；其余缺陷在完整 8 片基础上改造
    if defect_type == "single":
        positions = [0.0]
    elif defect_type == "missing":
        # 抽掉第 5 层（索引 4）：第 4、6 层位置相差 2 倍层距
        positions = [
            i * NOMINAL_SLICE_SPACING
            for i in range(instance_count) if i != 4
        ]
    else:
        positions = [
            i * NOMINAL_SLICE_SPACING for i in range(instance_count)
        ]

    for seq, z in enumerate(positions, start=1):
        sop_uid = _make_uid(serial + 100 + seq)
        ds = build_ct_dataset(
            study_uid=study_uid,
            series_uid=series_uid,
            sop_uid=sop_uid,
            patient_id=patient_id,
            patient_name=f"DEFECT^{defect_type.capitalize()}",
            instance_number=seq,
            instance_count=len(positions),
        )
        # build_ct_dataset 内部按 instance_number 重算位置，这里统一覆盖为
        # 缺陷计划中的 z 坐标，保证 IPP 与 SliceLocation 一致。
        ds.ImagePositionPatient = [0.0, 0.0, float(z)]
        ds.SliceLocation = float(z)

        if defect_type == "duplicate" and seq == 5:
            # 第 5 片与第 4 片位置完全相同（重层），但仍是独立 SOP
            dup_z = positions[seq - 2]
            ds.ImagePositionPatient = [0.0, 0.0, float(dup_z)]
            ds.SliceLocation = float(dup_z)
        elif defect_type == "spacing" and seq == 5:
            # 第 4→5 层距 3.2mm：与 2.0mm 偏差 1.2mm，且不是 2 的整数倍，
            # 在常用容差（如 0.5mm）下应报层距异常而非缺层
            ds.ImagePositionPatient = [
                0.0, 0.0,
                float(positions[seq - 2] + 3.2),
            ]
            ds.SliceLocation = float(ds.ImagePositionPatient[2])
        elif defect_type == "orientation" and seq == 5:
            # 行方向翻转：法向随之反向，同序列方向不一致
            ds.ImageOrientationPatient = [-1.0, 0.0, 0.0, 0.0, 1.0, 0.0]
        elif defect_type == "no_tag" and seq == 5:
            # 几何标签缺失：无法判定，不得报合格
            del ds.ImageOrientationPatient
            del ds.ImagePositionPatient
            if "SliceLocation" in ds:
                del ds.SliceLocation

        ds.save_as(str(study_dir / f"{seq:04d}.dcm"),
                   enforce_file_format=True)

    return {
        "study_instance_uid": study_uid,
        "series_instance_uid": series_uid,
        "patient_id": patient_id,
        "patient_name": f"DEFECT^{defect_type.capitalize()}",
        "instances": len(positions),
        "defect_type": defect_type,
        "directory": str(study_dir),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="生成虚构合成 CT 数据")
    parser.add_argument("--out", required=True, help="输出目录")
    parser.add_argument(
        "--counts", default="8,6,5",
        help="三个检查各自的实例数，逗号分隔（默认 8,6,5）",
    )
    parser.add_argument(
        "--defects", default="",
        help=("逗号分隔的缺陷检查类型，可选 "
              "missing,duplicate,spacing,orientation,no_tag,single；"
              "all 表示全部生成，留空不生成缺陷检查（默认留空）"),
    )
    args = parser.parse_args()

    out_dir = Path(args.out)
    counts = [int(x) for x in args.counts.split(",")]
    manifest = []
    for idx, count in enumerate(counts, start=1):
        meta = generate_study(out_dir, idx, count)
        manifest.append(meta)
        print(f"generated study {meta['study_instance_uid']} "
              f"({count} instances) -> {meta['directory']}")

    raw = args.defects.strip().lower()
    if raw:
        wanted = DEFECT_TYPES if raw == "all" else tuple(
            x.strip() for x in raw.split(",") if x.strip()
        )
        unknown = [x for x in wanted if x not in DEFECT_TYPES]
        if unknown:
            raise SystemExit(f"未知缺陷类型：{unknown}，支持 {DEFECT_TYPES}")
        for d_idx, defect_type in enumerate(wanted, start=1):
            meta = generate_defect_study(out_dir, d_idx, defect_type)
            manifest.append(meta)
            print(f"generated defect study [{defect_type}] "
                  f"{meta['study_instance_uid']} ({meta['instances']} "
                  f"instances) -> {meta['directory']}")

    import json
    manifest_path = out_dir / "studies.json"
    manifest_path.write_text(json.dumps(manifest, indent=2, ensure_ascii=False))
    print(f"manifest -> {manifest_path}")


if __name__ == "__main__":
    main()
