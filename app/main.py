"""FastAPI 入口：批管理 REST API + Swagger 文档 + DICOM 服务生命周期。"""
from __future__ import annotations

import logging
import os
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import BackgroundTasks, FastAPI, HTTPException, Query
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from . import dicom, geometry, storage
from .config import settings
from .db import (
    STATE_COMPLETED,
    Database,
    set_db,
)
from .pipeline import Pipeline, recover_on_startup

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(name)s | %(message)s",
)
log = logging.getLogger("api")

# 进程级单例（lifespan 中初始化）
db: Database | None = None
pipeline: Pipeline | None = None
_scp_server = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global db, pipeline, _scp_server
    settings.ensure_dirs()
    db = Database(settings.DB_PATH)
    set_db(db)
    recover_on_startup(db)
    _scp_server = dicom.start_store_scp(db)
    pipeline = Pipeline(db)
    log.info("API 就绪，数据卷=%s", settings.DATA_DIR)
    try:
        yield
    finally:
        if _scp_server is not None:
            _scp_server.shutdown()
        if db is not None:
            db.close()


app = FastAPI(
    title="DICOM CT 离线取数流水线",
    version="1.0.0",
    lifespan=lifespan,
    description=(
        "提交 StudyInstanceUID → C-FIND 固定 SOP 清单 → C-MOVE 回传 → "
        "C-STORE 接收非压缩 CT → 核对收齐后打包 ZIP 下载。\n\n"
        "规则：清单外实例不入包、重复实例不重复计数、保存失败即批失败、"
        "失败批迟到回传不可恢复、重启后未完成批判失败。"
    ),
)


# ------------------------------------------------------------------ schemas
class BatchCreate(BaseModel):
    study_instance_uid: str = Field(
        ..., description="源端检查的 StudyInstanceUID",
        examples=["1.2.840.10008.1.2.99.1"],
    )
    fault_inject: str = Field(
        default="",
        description="可选故障注入：save_failure=保存失败演示；留空为正常流程",
    )


class BatchView(BaseModel):
    id: str
    study_instance_uid: str
    state: str
    expected_count: int
    received_count: int
    failure_reason: str | None
    zip_path: str | None
    fault_inject: str
    created_at: str
    updated_at: str
    downloadable: bool


def _view(row: dict) -> BatchView:
    return BatchView(
        id=row["id"],
        study_instance_uid=row["study_instance_uid"],
        state=row["state"],
        expected_count=row["expected_count"],
        received_count=row["received_count"],
        failure_reason=row["failure_reason"],
        zip_path=row["zip_path"],
        fault_inject=row["fault_inject"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        downloadable=row["state"] == STATE_COMPLETED and bool(row["zip_path"]),
    )


class GeometryAnomaly(BaseModel):
    type: str = Field(..., description="异常类型代码：MISSING_GEOMETRY_TAG / "
                      "ORIENTATION_INCONSISTENT / DUPLICATE_SLICE / "
                      "MISSING_SLICE / SPACING_ANOMALY / NOT_ENOUGH_SLICES")
    message: str
    entry_name: str | None = None
    sop_instance_uid: str | None = None
    detail: dict = Field(default_factory=dict)


class SeriesGeometry(BaseModel):
    series_instance_uid: str
    slice_count: int = Field(..., description="该序列在 ZIP 内的切片数")
    status: str = Field(..., description="VALID=至少两片且无异常；"
                        "INVALID=发现明确几何异常；"
                        "INDETERMINATE=标签缺失/不足两片，无法判定，不得视为合格")
    normal: list[float] | None = Field(
        None, description="由 ImageOrientationPatient 行/列方向叉积得到的单位法向")
    positions: list[float] = Field(
        default_factory=list,
        description="各切片 ImagePositionPatient 在法向上的投影（升序，mm）")
    slice_spacings: list[float] = Field(
        default_factory=list, description="相邻切片投影位置之差（mm）")
    min_spacing: float | None = None
    max_spacing: float | None = None
    mean_spacing: float | None = None
    anomalies: list[GeometryAnomaly] = Field(default_factory=list)


class GeometryReport(BaseModel):
    batch_id: str
    state: str
    expected_spacing_mm: float
    tolerance_mm: float
    scan_completed: bool = Field(
        ..., description="ZIP 是否被完整扫描；false 时 usable 必为 false，"
        "且不得用已读切片给出合格结论")
    usable: bool = Field(
        ..., description="整批是否可用于体数据重建：仅当扫描完整、每个序列"
        "至少两片且全部无异常时才为 true")
    verdict: str = Field(
        ..., description="USABLE=可重建；NOT_USABLE=存在明确异常；"
        "INDETERMINATE=标签缺失/不足两片/扫描不完整，无法判定")
    reason: str | None = Field(None, description="不可用/无法判定的总体说明")
    scan_error: dict | None = Field(
        None, description="扫描错误（ZIP 缺失/被替换/条目损坏）的代码与说明")
    series: list[SeriesGeometry] = Field(default_factory=list)
    series_count: int = 0


# ------------------------------------------------------------------ routes
@app.post("/batches", response_model=BatchView, status_code=201,
          tags=["batches"], summary="提交检查，启动取数批")
def create_batch(body: BatchCreate) -> BatchView:
    if body.fault_inject not in ("", storage.FAULT_SAVE):
        raise HTTPException(400, "fault_inject 只支持 '' 或 'save_failure'")
    uid = body.study_instance_uid.strip()
    if not uid:
        raise HTTPException(400, "study_instance_uid 不能为空")

    if db.has_active_batch_for_study(uid):
        raise HTTPException(409, "该 Study 已有进行中的批，请等待其结束")

    batch_id = uuid.uuid4().hex
    db.create_batch(batch_id, uid, body.fault_inject)
    pipeline.submit(batch_id)
    return _view(db.get_batch(batch_id))


@app.get("/batches", response_model=list[BatchView],
         tags=["batches"], summary="列出全部批")
def list_batches() -> list[BatchView]:
    return [_view(r) for r in db.list_batches()]


@app.get("/batches/{batch_id}", response_model=BatchView,
         tags=["batches"], summary="查询批状态与进度")
def get_batch(batch_id: str) -> BatchView:
    row = db.get_batch(batch_id)
    if row is None:
        raise HTTPException(404, "批不存在")
    return _view(row)


@app.get("/batches/{batch_id}/events",
         tags=["batches"], summary="查询批事件流水（接收/重复/拒收/状态迁移）")
def get_events(batch_id: str) -> list[dict]:
    if db.get_batch(batch_id) is None:
        raise HTTPException(404, "批不存在")
    return db.list_events(batch_id)


@app.get("/batches/{batch_id}/instances",
         tags=["batches"], summary="查询批清单及每个 SOP 的接收情况")
def get_instances(batch_id: str) -> dict:
    row = db.get_batch(batch_id)
    if row is None:
        raise HTTPException(404, "批不存在")
    files = db.received_files(batch_id)
    received = {sop: path for sop, path in files}
    return {
        "batch_id": batch_id,
        "state": row["state"],
        "manifest": sorted(db.manifest_uids(batch_id)),
        "received": sorted(received),
        "missing": db.missing_uids(batch_id),
    }


@app.get("/batches/{batch_id}/download",
         tags=["batches"], summary="下载 ZIP（仅 COMPLETED 批可下载）")
def download_zip(batch_id: str) -> FileResponse:
    row = db.get_batch(batch_id)
    if row is None:
        raise HTTPException(404, "批不存在")
    if row["state"] != STATE_COMPLETED or not row["zip_path"]:
        raise HTTPException(
            409,
            f"批状态 {row['state']} 不可下载；仅 C-MOVE 成功且清单收齐的批可下载",
        )
    path = Path(row["zip_path"])
    if not path.exists():
        raise HTTPException(410, "ZIP 文件缺失（数据卷可能已损坏）")
    return FileResponse(
        path, media_type="application/zip",
        filename=f"{batch_id}.zip",
    )


@app.post("/batches/{batch_id}/geometry-check",
          response_model=GeometryReport, tags=["batches"],
          summary="对已完成批 ZIP 做切片几何检查（重建体数据前的可靠性判定）",
          responses={
              404: {"description": "批不存在"},
              409: {"description": "批未完成，尚无 ZIP 可检查"},
              422: {"description": "层距/容差参数非法"},
              502: {"description": "扫描不完整（ZIP 缺失/被替换/条目损坏），"
                                  "usable=false，不得据此判定合格"},
          })
def geometry_check(
    batch_id: str,
    expected_spacing_mm: float = Query(
        ..., alias="expected_spacing_mm", gt=0,
        description="期望层距（mm），必须为正数",
    ),
    tolerance_mm: float = Query(
        ..., alias="tolerance_mm", ge=0,
        description="层距允许的绝对偏差（mm），非负",
    ),
) -> GeometryReport:
    """读取已完成批 ZIP 内的非压缩 CT，按 SeriesInstanceUID 分组检查切片几何。

    检查项：重层、缺层、层距异常、方向不一致、几何标签缺失。
    每个序列至少两片且全部无异常，整批才判为可用；标签缺失或不足两片的序列
    只能给 INDETERMINATE，绝不能报合格。扫描中 ZIP 缺失、被替换或条目损坏时
    返回 502 且 ``scan_completed=false``，不得用已读切片给出合格结论。
    """
    row = db.get_batch(batch_id)
    if row is None:
        raise HTTPException(404, "批不存在")
    if row["state"] != STATE_COMPLETED or not row["zip_path"]:
        raise HTTPException(
            409,
            f"批状态 {row['state']} 不可做几何检查；仅 COMPLETED 批存在 ZIP",
        )

    base = GeometryReport(
        batch_id=batch_id,
        state=row["state"],
        expected_spacing_mm=expected_spacing_mm,
        tolerance_mm=tolerance_mm,
        scan_completed=False,
        usable=False,
        verdict="INDETERMINATE",
    )

    try:
        reports = geometry.check_zip_geometry(
            row["zip_path"], expected_spacing_mm, tolerance_mm
        )
    except geometry.ScanError as exc:
        # 扫描不完整：绝不允许用已读切片给出合格结论
        base.scan_error = {"code": exc.code, "message": str(exc),
                           "entry": exc.entry}
        base.reason = f"ZIP 扫描不完整，无法判定：{exc}"
        db.add_event(
            "GEOMETRY_CHECK_SCAN_ERROR",
            f"几何检查扫描失败 code={exc.code}：{exc}",
            batch_id,
        )
        raise HTTPException(502, base.model_dump()) from exc

    base.scan_completed = True
    base.series = [SeriesGeometry(**r.to_dict()) for r in reports]
    base.series_count = len(reports)

    if not reports:
        base.verdict = "INDETERMINATE"
        base.reason = "ZIP 内没有任何可分组的 DICOM 序列，无法判定"
    elif all(r.status == geometry.SERIES_VALID for r in reports):
        base.verdict = "USABLE"
        base.usable = True
        base.reason = None
    elif any(r.status == geometry.SERIES_INDETERMINATE for r in reports):
        base.verdict = "INDETERMINATE"
        bad = [r.series_instance_uid for r in reports
               if r.status == geometry.SERIES_INDETERMINATE]
        base.reason = (f"序列 {bad} 几何标签缺失或切片不足，无法判定；"
                       "无法判定的序列不得视为合格")
    else:
        base.verdict = "NOT_USABLE"
        bad = [r.series_instance_uid for r in reports
               if r.status == geometry.SERIES_INVALID]
        base.reason = f"序列 {bad} 存在重层/缺层/层距异常/方向不一致"

    db.add_event(
        "GEOMETRY_CHECK",
        f"几何检查完成 verdict={base.verdict} 序列数={base.series_count} "
        f"期望层距={expected_spacing_mm}mm 容差={tolerance_mm}mm",
        batch_id,
    )
    return base


@app.get("/health", tags=["system"], summary="健康检查")
def health() -> dict:
    return {"status": "ok", "store_port": settings.STORE_PORT,
            "ae_title": settings.APP_AET}


# ------------------------------------------------------------------ debug
@app.get("/internal/pacs-studies", tags=["internal"],
         summary="（演示用）STUDY 级 C-FIND 列出模拟 PACS 中的检查")
def pacs_studies() -> list[dict]:
    from pydicom.dataset import Dataset
    from pydicom.uid import ExplicitVRLittleEndian, ImplicitVRLittleEndian
    from pynetdicom import AE
    from pynetdicom.presentation import build_context
    from pynetdicom.sop_class import StudyRootQueryRetrieveInformationModelFind

    ae = AE(ae_title=settings.APP_AET)
    ae.requested_contexts = [build_context(
        str(StudyRootQueryRetrieveInformationModelFind),
        [str(ExplicitVRLittleEndian), str(ImplicitVRLittleEndian)])]
    assoc = ae.associate(settings.PACS_HOST, settings.PACS_PORT,
                         ae_title=settings.PACS_AET)
    if not assoc.is_established:
        raise HTTPException(502, "无法连接模拟 PACS")
    try:
        q = Dataset()
        q.QueryRetrieveLevel = "STUDY"
        q.StudyInstanceUID = ""
        q.PatientID = ""
        q.PatientName = ""
        q.Modality = ""
        q.NumberOfStudyRelatedInstances = ""
        out = []
        for status, ident in assoc.send_c_find(
            q, str(StudyRootQueryRetrieveInformationModelFind)
        ):
            if ident is None:
                continue
            out.append({
                "study_instance_uid": str(ident.StudyInstanceUID),
                "patient_id": str(getattr(ident, "PatientID", "")),
                "patient_name": str(getattr(ident, "PatientName", "")),
                "modality": str(getattr(ident, "Modality", "")),
                "instances": int(getattr(ident, "NumberOfStudyRelatedInstances", 0) or 0),
            })
        return out
    finally:
        if assoc.is_alive:
            assoc.release()


@app.post("/internal/suicide", tags=["internal"],
          summary="（演示用）退出进程以演示重启恢复；compose 自动重启")
def suicide(tasks: BackgroundTasks) -> dict:
    def _die() -> None:
        os._exit(1)
    tasks.add_task(_die)
    return {"detail": "进程即将退出，等待 Docker 重启"}
