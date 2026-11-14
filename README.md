# DICOM CT 离线取数流水线

面向影像科研数据员的院内 PACS 取数工具：提交 `StudyInstanceUID`，经
**C-FIND 固定本批 SOP 清单 → C-MOVE 发起回传 → C-STORE 接收落盘 →
核对收齐 → 打包 ZIP 下载** 的完整流水线，支持多检查并行、慢回传交错、
重复投送去重、清单外实例拒收、保存失败判失败与重启恢复。

已完成批在交付离线分割前，可调用 **切片几何检查 API** 复核 ZIP 内非压缩 CT
能否可靠重建体数据：按 `SeriesInstanceUID` 分组，用 `ImageOrientationPatient`
确定切片法向、投影 `ImagePositionPatient`，逐序列报告重层、缺层、层距异常、
方向不一致与几何标签缺失；每个序列至少两片且全部无异常，整批才判可用。

* **API 框架**：FastAPI（自带 Swagger/OpenAPI）
* **DICOM 栈**：pynetdicom 3.x（C-STORE SCP + C-FIND/C-MOVE SCU）、pydicom
* **持久化**：SQLite（WAL，全量落在数据卷）
* **模拟 PACS**：pynetdicom 实现的真实 DICOM 网络对端，内置虚构合成 CT，
  可配置慢回传与重复投送；另带 rogue 投送器演示拒收，并可生成缺层等
  六类几何缺陷检查供几何检查 API 演示

---

## 1. 快速开始（Docker Compose）

```bash
# 构建并启动 API + 模拟 PACS（首次会自动生成 8/6/5 个实例的三个虚构 CT 检查）
docker compose up --build -d

# Swagger 文档
open http://localhost:8000/docs

# 一键端到端演示：交错回传 / 重复投送 / 清单外拒收 / 压缩拒收 /
# 保存失败 / 失败批迟到拒收 / 重启恢复
docker compose run --rm demo
```

端口：

| 服务 | 端口 | 用途 |
|---|---|---|
| api | 8000 | REST API + Swagger |
| api | 11113 | DICOM C-STORE SCP（AE=`RETRIEVER`）|
| pacs | 11112 | 模拟 PACS（AE=`MYPACS`）|

数据卷：`api-data`（SQLite、DICOM 文件、ZIP）、`pacs-data`（虚构 CT）。

---

## 2. 业务规则与实现位置

| 规则 | 实现 |
|---|---|
| 只收**非压缩 CT** | SCP 只公布 Implicit/Explicit VR Little Endian 的 CT 存储上下文，压缩 TS 在协商阶段即被拒；`app/storage.py::validate_dataset` 二次校验 |
| 源端批内**不增删实例** | C-FIND（IMAGE 级）一次性固定清单写入 `instances` 表，`set_manifest` 只允许固定一次 |
| 回传**归入对应批** | C-STORE 按 `StudyInstanceUID` 经 `db.find_batch_for_study` 路由（按 rowid 取最新批） |
| **清单外实例不入包** | `db.is_manifest_uid` 校验，拒收并回 `0xC000`，事件 `STORE_REJECTED` |
| **重复实例不重复计数** | `(batch_id, sop_instance_uid)` 唯一约束 + `register_instance` 条件更新；重复直接回 SUCCESS，不写盘不计数，事件 `STORE_DUPLICATE` |
| **文件写完且登记成功才回 C-STORE 成功** | `handle_c_store` 严格顺序：临时文件 → fsync → rename → 目录 fsync → DB 登记；任一步失败回 `0xA700/0xC000`，登记失败会删除已写文件 |
| **C-MOVE 成功且清单收齐才可下载** | 下载接口仅允许 `COMPLETED` 且 zip_path 存在；MOVING/FAILED 返回 409 |
| 传输/保存失败、结束后缺件均**判失败** | C-MOVE 非 0x0000（含 0xA702/0xB000）失败；`missing_uids` 非空失败；写盘异常立即 `fail_batch` |
| **迟到回传不能恢复失败批** | 终态 FAILED 的批收到 C-STORE 一律拒收；DB 层 `transition_state` 禁止任何状态迁出终态 |
| 多检查**并行、慢回传互不阻塞** | 每批独立线程；C-STORE SCP 每关联一线程；C-MOVE 回传与其它批的 API/DICOM 处理无共享阻塞 |
| **重启恢复** | 启动时所有非终态批置 FAILED；COMPLETED 批保留可下载，ZIP 丢失自动按登记重建 |
| **几何检查 fail-closed** | 扫描中 ZIP 缺失/被替换（SHA-256 复核）/条目损坏即 502，`usable=false`，禁止用已读切片给合格结论；标签缺失或不足两片只给 `INDETERMINATE`，绝不报合格 |

### 批状态机

```
PENDING ──C-FIND──▶ FINDING ──清单固定+故障武装──▶ MOVING
                                          │
                  收齐+C-MOVE 成功 ────────┴──▶ COMPLETED（可下载，终态）
                  任何失败/缺件 ──────────────▶ FAILED（终态，迟到回传拒收）
重启时 PENDING/FINDING/MOVING ───────────────▶ FAILED
```

---

## 3. REST API

Swagger：`GET /docs`（OpenAPI JSON：`/openapi.json`）

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/batches` | 提交检查，body：`{"study_instance_uid": "...", "fault_inject": ""}`，`fault_inject` 可选 `save_failure` |
| GET | `/batches` | 列出全部批 |
| GET | `/batches/{id}` | 批状态与进度（`expected_count/received_count/state/downloadable`）|
| GET | `/batches/{id}/events` | 事件流水（接收、重复、拒收、状态迁移、故障）|
| GET | `/batches/{id}/instances` | 清单、已收、缺失 SOP 列表 |
| GET | `/batches/{id}/download` | 下载 ZIP（仅 COMPLETED），ZIP 内含 `dicom/` 与 `MANIFEST.csv` |
| POST | `/batches/{id}/geometry-check` | 切片几何检查（仅 COMPLETED），query 参数 `expected_spacing_mm`（>0）、`tolerance_mm`（≥0）|
| GET | `/health` | 健康检查 |
| GET | `/internal/pacs-studies` | （演示）C-FIND 列出模拟 PACS 检查 |
| POST | `/internal/suicide` | （演示）退出进程，由 compose 自动重启以演示恢复 |

手工调用示例：

```bash
BASE=http://localhost:8000
STUDY=$(curl -s $BASE/internal/pacs-studies | jq -r '.[0].study_instance_uid')

BID=$(curl -s -X POST $BASE/batches -H 'Content-Type: application/json' \
  -d "{\"study_instance_uid\":\"$STUDY\"}" | jq -r .id)

watch "curl -s $BASE/batches/$BID | jq"
curl -s $BASE/batches/$BID/events | jq
curl -OJ $BASE/batches/$BID/download

# 保存失败演示
curl -s -X POST $BASE/batches -H 'Content-Type: application/json' \
  -d "{\"study_instance_uid\":\"$STUDY\",\"fault_inject\":\"save_failure\"}"
```

### 3.1 切片几何检查

仅 `COMPLETED` 批可检查；层距与容差为必填 query 参数，调用方必须显式给出
重建假设，服务端不做隐式猜测：

```bash
# 期望层距 2.0mm，容差 0.5mm
curl -s -X POST \
  "$BASE/batches/$BID/geometry-check?expected_spacing_mm=2.0&tolerance_mm=0.5" | jq
```

判定逻辑（实现见 `app/geometry.py`）：

1. 读取 ZIP 内全部非压缩 CT 条目，按 `SeriesInstanceUID` 分组；
2. 以首片 `ImageOrientationPatient`（IOP）的行、列方向叉积得到单位法向，
   同序列其余切片逐分量比对 IOP，不一致即 **方向不一致**；
3. 把每片 `ImagePositionPatient`（IPP）投影到法向并升序排列；
4. 相邻投影位置之差（层距）分类：
   * 位置重合（小于期望层距 1/4）→ **重层**；
   * 约为期望层距的整数倍（k≥2，倍数误差在容差内）→ **缺层**，
     估算缺失 k−1 层；该判断优先于单层容差，大容差不会把缺层吸收成合格；
   * 与期望层距偏差在容差内 → 正常；
   * 其余偏差 → **层距异常**；
5. IOP/IPP 缺失或无法解析 → **几何标签缺失**，该序列只能 `INDETERMINATE`；
6. 序列不足两片 → `INDETERMINATE`（无法判定，不得报合格）。

响应关键字段：

| 字段 | 含义 |
|---|---|
| `usable` | 整批可否用于体数据重建；仅扫描完整且每个序列 `VALID` 时为 `true` |
| `verdict` | `USABLE` / `NOT_USABLE`（明确几何异常）/ `INDETERMINATE`（标签缺失、不足两片或扫描不完整）|
| `scan_completed` | ZIP 是否被完整扫描；`false` 时 `usable` 必为 `false` |
| `scan_error` | 扫描错误码：`ZIP_MISSING` / `ZIP_REPLACED` / `ZIP_STRUCTURE_ERROR` / `CORRUPT_ENTRY` |
| `series[].status` | `VALID` / `INVALID` / `INDETERMINATE`，附法向、投影位置、层距与异常明细 |

**失败安全（fail-closed）**：扫描中 ZIP 缺失、被替换（扫描前后 SHA-256 摘要
或大小不一致）或任一条目 CRC/DICOM 解析损坏，接口返回 HTTP 502，
`scan_completed=false`、`usable=false`，绝不使用已读切片给出合格结论；
ZIP 恢复后重新检查即可得到正常结论。几何检查不改变批状态、不修改 ZIP，
原取数与下载接口行为完全不变；每次检查在批事件流水中留
`GEOMETRY_CHECK` / `GEOMETRY_CHECK_SCAN_ERROR` 记录。

---

## 4. 演示场景说明

模拟 PACS 支持环境变量（见 `docker-compose.yml`）：

* `MOVE_DELAY=1.0`：每个实例发送前停顿 1 秒，制造慢回传，便于观察多批交错
* `MOVE_DUPLICATE_EVERY=3`：每 3 个实例额外用一条新关联重发一次，模拟源端重试
  （接收端应回 SUCCESS 但不计数）
* `DEFECT_STUDIES=all`：额外生成 6 个带切片几何缺陷的虚构 CT 检查，
  供几何检查 API 演示；也可逗号指定子集（如 `missing,duplicate`），置空关闭

| 缺陷检查 | 几何特征 | 期望判定 |
|---|---|---|
| `missing` | 中间整层缺失，相邻层位置相差 2 倍层距 | `NOT_USABLE` / `MISSING_SLICE` |
| `duplicate` | 两片 IPP 完全相同（独立 SOP） | `NOT_USABLE` / `DUPLICATE_SLICE` |
| `spacing` | 某层距 3.2mm，非整倍且超容差 | `NOT_USABLE` / `SPACING_ANOMALY` |
| `orientation` | 某片 IOP 行方向翻转 | `NOT_USABLE` / `ORIENTATION_INCONSISTENT` |
| `no_tag` | 某片缺少 IOP/IPP | `INDETERMINATE` / `MISSING_GEOMETRY_TAG` |
| `single` | 全检查仅 1 片 | `INDETERMINATE` / `NOT_ENOUGH_SLICES` |

也可不经过 PACS，直接用生成器产出缺陷数据：

```bash
python -m simulator.generate_data --out /tmp/pacs --counts 8,6,5 --defects all
python -m simulator.generate_data --out /tmp/pacs --defects missing,orientation
```

rogue 投送器（演示容器/API 镜像内均可运行）：

```bash
# 清单外实例：期望 0xC000 拒收
python -m simulator.rogue_sender off-list   --study <StudyUID> --host api --port 11113
# 声明 JPEG 压缩：期望关联协商被拒
python -m simulator.rogue_sender compressed --study <StudyUID> --host api --port 11113
```

保存失败演示的实现是**真实故障**而非 mock：提交时把批目录 `chmod 555`，
非 root 容器用户写入必然 `PermissionError`，可观察到：
首个实例 C-STORE 回 `0xA700` → 批立即 FAILED → 后续实例全部以“已失败”拒收
→ C-MOVE 最终 `0xA702` → 下载返回 409。

---

## 5. 目录结构

```
app/
  config.py      环境变量配置（AE、端口、数据卷路径、超时）
  db.py          SQLite schema、状态机、清单/登记去重、重启恢复查询
  storage.py     非压缩 CT 校验、原子写盘+fsync、故障注入、ZIP 打包
  dicom.py       C-STORE SCP 处理器、C-FIND/C-MOVE SCU
  geometry.py    ZIP 切片几何检查：法向/投影、重层/缺层/层距/方向/标签缺失、
                 扫描完整性（缺失/替换/损坏 fail-closed）
  pipeline.py    每批后台线程编排与启动恢复
  main.py        FastAPI 路由、lifespan、Swagger（含 geometry-check）
simulator/
  generate_data.py  虚构合成 CT 生成器（确定性模体像素 + 六类几何缺陷检查）
  pacs.py           模拟 PACS（C-ECHO/C-FIND/C-MOVE，慢回传+重复投送）
  rogue_sender.py   清单外/压缩实例投送器
demo/
  run_demo.py       端到端演示编排（含几何检查演示段）
tests/
  test_db.py       状态机/去重/终态保护单元测试
  test_storage.py  校验/原子写/ZIP 单元测试
  test_geometry.py 几何检查单元测试（31 个场景，含 ZIP 替换/删除/CRC 损坏）
  test_e2e.py      真实 pynetdicom 网络栈端到端测试（含几何检查 API 7 个场景）
docker-compose.yml  Dockerfile  Dockerfile.pacs  entrypoint.sh
```

## 6. 本地测试（无需 Docker）

```bash
pip install -r requirements.txt pytest
python -m pytest tests/ -v
```

端到端测试会在临时目录启动完整的 PACS + API 真实进程对，覆盖：
正常收齐打包、并行交错、重复去重、清单外/压缩/未知 Study 拒收、
保存失败与迟到拒收、409 冲突、重启恢复，以及切片几何检查的
正常批可用、六类缺陷分类、参数/状态守卫、ZIP 缺失与条目损坏 fail-closed。

## 7. 接入真实 PACS 的注意事项

1. 在真实 PACS 上注册 AE Title `RETRIEVER` 及本服务主机/11113 端口作为
   C-MOVE 目标，并放通防火墙。
2. 用 `PACS_AET/PACS_HOST/PACS_PORT` 指向真实源端；确认其 C-FIND 支持
   Study Root IMAGE 级查询并返回 `TransferSyntaxUID`。
3. 流水线只接受未压缩 little-endian CT；若源端只存压缩 TS，需在源端配置
   转码，或扩展 `app/dicom.py` 的上下文与 `storage.UNCOMPRESSED_TS` 策略。
4. 数据卷需保证持久化挂载与足够 inode/容量；SQLite 与 DICOM 文件均在
   `DATA_DIR` 下，备份该目录即可完整备份批状态与文件。
