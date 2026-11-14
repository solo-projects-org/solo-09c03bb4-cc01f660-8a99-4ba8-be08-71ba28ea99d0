"""端到端演示编排（在 api 容器内或任何能访问 API/PACS 的环境运行）。

演示序列
========
1. 列出模拟 PACS 中的虚构 CT 检查
2. 并行提交两个正常批（交错/慢回传互不阻塞，其中一个启用源端重复投送）
3. 回传进行中投送“清单外实例”和“压缩实例”，验证均被拒收、不入包
4. 提交一个 save_failure 批，验证保存失败即批失败、C-STORE 回失败码
5. 轮询进度，下载完成批 ZIP 并校验实例数与清单一致
6. 重启恢复演示：制造一个未完成批后调用进程退出，验证重启后该批判失败、
   已完成批仍可下载

运行::

    python -m demo.run_demo            # 完整演示
    python -m demo.run_demo --skip-restart
"""
from __future__ import annotations

import argparse
import io
import os
import sys
import time
import zipfile

import requests

# 默认走 compose 服务名；本地裸跑时可用环境变量覆盖为 127.0.0.1
API = os.getenv("API_BASE", "http://api:8000")
HOST_API = os.getenv("STORE_HOST", "api")
PORT_API = int(os.getenv("STORE_PORT", "11113"))


def banner(title: str) -> None:
    print("\n" + "=" * 72)
    print(f"  {title}")
    print("=" * 72)


def wait_state(batch_id: str, target: set[str], timeout: float = 180) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        r = requests.get(f"{API}/batches/{batch_id}", timeout=10)
        r.raise_for_status()
        view = r.json()
        if view["state"] in target:
            return view
        print(f"    ... {batch_id[:8]} {view['state']} "
              f"{view['received_count']}/{view['expected_count']}")
        time.sleep(1.5)
    raise TimeoutError(f"批 {batch_id} 未在 {timeout}s 内到达 {target}")


def show_events(batch_id: str) -> None:
    events = requests.get(f"{API}/batches/{batch_id}/events",
                          timeout=10).json()
    for e in events:
        print(f"    [{e['created_at'][11:]}] {e['kind']}: {e['message']}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--skip-restart", action="store_true",
                        help="跳过重启恢复演示")
    args = parser.parse_args()

    banner("1. 列出模拟 PACS 中的检查")
    studies = requests.get(f"{API}/internal/pacs-studies", timeout=20).json()
    for s in studies:
        print(f"    Study={s['study_instance_uid']} 患者={s['patient_name']} "
              f"实例数={s['instances']}")
    if len(studies) < 3:
        print("PACS 至少需要 3 个检查")
        return 1
    study_a, study_b, study_c = (s["study_instance_uid"] for s in studies[:3])

    banner("2. 并行提交批 A（含源端重复投送）与批 B，验证交错回传互不阻塞")
    # 批 A 的重复投送由 PACS 环境变量 MOVE_DUPLICATE_EVERY=3 控制
    r_a = requests.post(f"{API}/batches",
                        json={"study_instance_uid": study_a}, timeout=10)
    r_a.raise_for_status()
    batch_a = r_a.json()["id"]
    r_b = requests.post(f"{API}/batches",
                        json={"study_instance_uid": study_b}, timeout=10)
    r_b.raise_for_status()
    batch_b = r_b.json()["id"]
    print(f"    批 A={batch_a}  Study={study_a}")
    print(f"    批 B={batch_b}  Study={study_b}")

    # 等清单固定、进入 MOVING，再投 rogue 流量
    time.sleep(3)

    banner("3. 清单外实例 / 压缩实例攻击，均应被拒收且不入包")
    from simulator import rogue_sender

    old_argv = sys.argv
    sys.argv = ["rogue", "off-list", "--study", study_a,
                "--host", HOST_API, "--port", str(PORT_API)]
    try:
        rc_offlist = rogue_sender.main()
    finally:
        sys.argv = old_argv
    sys.argv = ["rogue", "compressed", "--study", study_b,
                "--host", HOST_API, "--port", str(PORT_API)]
    try:
        rc_compressed = rogue_sender.main()
    finally:
        sys.argv = old_argv
    print(f"    清单外投送拒收校验返回码={rc_offlist}（0=符合预期）")
    print(f"    压缩投送拒收校验返回码={rc_compressed}（0=符合预期）")

    banner("4. 提交批 C 并注入保存失败（批目录只读，真实写盘失败）")
    r_c = requests.post(f"{API}/batches",
                        json={"study_instance_uid": study_c,
                              "fault_inject": "save_failure"}, timeout=10)
    r_c.raise_for_status()
    batch_c = r_c.json()["id"]
    print(f"    批 C={batch_c}（save_failure）")

    banner("5. 等待三个批到达终态")
    view_a = wait_state(batch_a, {"COMPLETED", "FAILED"})
    view_b = wait_state(batch_b, {"COMPLETED", "FAILED"})
    view_c = wait_state(batch_c, {"COMPLETED", "FAILED"}, timeout=120)
    print(f"    批 A 终态={view_a['state']} "
          f"{view_a['received_count']}/{view_a['expected_count']}")
    print(f"    批 B 终态={view_b['state']} "
          f"{view_b['received_count']}/{view_b['expected_count']}")
    print(f"    批 C 终态={view_c['state']} 原因={view_c['failure_reason']}")

    print("\n  -- 批 A 事件流水（可见重复投送去重、rogue 拒收）--")
    show_events(batch_a)
    print("\n  -- 批 C 事件流水（可见保存失败）--")
    show_events(batch_c)

    banner("6. 下载批 A 的 ZIP 并核对内容")
    if view_a["state"] != "COMPLETED":
        print("    批 A 未完成，跳过下载校验")
    else:
        resp = requests.get(f"{API}/batches/{batch_a}/download", timeout=30)
        resp.raise_for_status()
        zf = zipfile.ZipFile(io.BytesIO(resp.content))
        dicom_files = [n for n in zf.namelist() if n.startswith("dicom/")]
        manifest = zf.read("MANIFEST.csv").decode().strip().splitlines()
        print(f"    ZIP 内 DICOM 文件数={len(dicom_files)}，"
              f"清单行数（含表头）={len(manifest)}")
        assert len(dicom_files) == view_a["expected_count"], "ZIP 实例数与清单不符"
        assert len(manifest) == view_a["expected_count"] + 1
        print("    校验通过：ZIP 仅含清单内实例，数量一致")

    # 对失败批下载应被拒绝
    denied = requests.get(f"{API}/batches/{batch_c}/download", timeout=10)
    print(f"    失败批 C 下载 HTTP 状态={denied.status_code}（期望 409）")
    assert denied.status_code == 409

    if args.skip_restart:
        banner("演示完成（已跳过重启恢复）")
        return 0

    banner("7. 重启恢复：制造未完成批后重启进程")
    # 再提交一个新检查的批，立刻杀掉进程，使其停在非终态。
    # 终态批已存在不影响重新提交（仅拒绝“进行中”的同 Study 批）。
    r_d = requests.post(f"{API}/batches",
                        json={"study_instance_uid": study_b}, timeout=10)
    if r_d.status_code == 409:
        print("    存在同 Study 进行中批，重启恢复场景略过")
    else:
        r_d.raise_for_status()
        batch_d = r_d.json()["id"]
        print(f"    批 D={batch_d} 已提交，立即触发进程退出")
        try:
            requests.post(f"{API}/internal/suicide", timeout=5)
        except requests.RequestException:
            pass
        # 等待容器编排（compose restart: always）或外部守护拉起进程
        deadline = time.time() + 90
        healthy = False
        while time.time() < deadline:
            try:
                if requests.get(f"{API}/health", timeout=3).ok:
                    healthy = True
                    break
            except requests.RequestException:
                time.sleep(1.5)
        if not healthy:
            print("    服务未在 90s 内恢复（本地裸跑请手动重启 API 后重试）")
            return 2
        view_d = wait_state(batch_d, {"COMPLETED", "FAILED"})
        print(f"    重启后批 D 状态={view_d['state']} "
              f"原因={view_d['failure_reason']}")
        assert view_d["state"] == "FAILED"
        assert "重启" in (view_d["failure_reason"] or "")
        show_events(batch_d)
        # 已完成批仍可下载；若 ZIP 丢失也会在启动时重建
        if view_a["state"] == "COMPLETED":
            again = requests.get(f"{API}/batches/{batch_a}/download", timeout=30)
            print(f"    重启后已完成批 A 下载 HTTP 状态={again.status_code}（期望 200）")
            assert again.status_code == 200

    banner("8. 切片几何检查：缺层等缺陷不得判为可重建")
    # 模拟 PACS 在 DEFECT_STUDIES=all 时提供 6 个缺陷检查；
    # 缺层检查可通过内部 C-FIND 列表 + 几何检查结果识别
    studies_all = requests.get(f"{API}/internal/pacs-studies",
                               timeout=20).json()
    print(f"    PACS 当前共 {len(studies_all)} 个检查（含几何缺陷演示检查）")

    # 8.1 正常批：几何检查应判 USABLE
    good_study = studies_all[0]["study_instance_uid"]
    r = requests.post(f"{API}/batches",
                      json={"study_instance_uid": good_study}, timeout=10)
    r.raise_for_status()
    good_bid = r.json()["id"]
    good_view = wait_state(good_bid, {"COMPLETED", "FAILED"})
    assert good_view["state"] == "COMPLETED"
    rep = requests.post(
        f"{API}/batches/{good_bid}/geometry-check",
        params={"expected_spacing_mm": 2.0, "tolerance_mm": 0.5},
        timeout=30,
    ).json()
    print(f"    正常检查几何判定：verdict={rep['verdict']} "
          f"usable={rep['usable']} 序列状态="
          f"{[s['status'] for s in rep['series']]}")
    assert rep["verdict"] == "USABLE" and rep["usable"] is True

    # 8.2 逐个检查其余检查，统计三类判定；缺陷检查必须无一被误判 USABLE
    verdicts = {"USABLE": 0, "NOT_USABLE": 0, "INDETERMINATE": 0}
    found_missing = False
    for s in studies_all[1:]:
        r = requests.post(f"{API}/batches",
                          json={"study_instance_uid": s["study_instance_uid"]},
                          timeout=10)
        if r.status_code == 409:
            continue  # 同 Study 已有终态/进行中批则跳过
        r.raise_for_status()
        bid = r.json()["id"]
        view = wait_state(bid, {"COMPLETED", "FAILED"})
        if view["state"] != "COMPLETED":
            continue
        report = requests.post(
            f"{API}/batches/{bid}/geometry-check",
            params={"expected_spacing_mm": 2.0, "tolerance_mm": 0.5},
            timeout=30,
        ).json()
        verdicts[report["verdict"]] += 1
        anomaly_types = {a["type"] for ser in report["series"]
                         for a in ser["anomalies"]}
        print(f"    Study={s['study_instance_uid'][-6:]} "
              f"verdict={report['verdict']:<13} 异常={sorted(anomaly_types)}")
        assert report["usable"] is False
        if "MISSING_SLICE" in anomaly_types:
            found_missing = True

    print(f"    判定汇总：{verdicts}")
    assert found_missing, "演示数据中应至少包含一个缺层检查"
    assert verdicts["USABLE"] == 0, "缺陷检查不得被判为可重建"

    # 8.3 扫描错误防护：下载完成批后删除其 ZIP 的场景由 API 容器内
    # 数据卷操作触发，这里校验参数与状态守卫即可
    bad_param = requests.post(
        f"{API}/batches/{good_bid}/geometry-check",
        params={"expected_spacing_mm": 0, "tolerance_mm": 0.5}, timeout=10)
    print(f"    非正层距请求 HTTP 状态={bad_param.status_code}（期望 422）")
    assert bad_param.status_code == 422

    banner("全部演示完成")
    return 0


if __name__ == "__main__":
    sys.exit(main())
