#!/bin/sh
# 模拟 PACS 启动前准备：数据卷为空时生成虚构合成 CT，随后降权运行
set -e
DATA_DIR="${PACS_DATA_DIR:-/data/pacs}"

if [ "$(id -u)" = "0" ]; then
    mkdir -p "$DATA_DIR"
    chown -R pacsuser:pacsuser "$(dirname "$DATA_DIR")"
    exec gosu pacsuser "$0" "$@"
fi

if [ ! -f "$DATA_DIR/studies.json" ]; then
    echo "[pacs-init] 数据卷为空，生成虚构合成 CT ..."
    GAP_ARGS=""
    if [ "${GAP_STUDY_INSTANCES:-0}" != "0" ]; then
        GAP_ARGS="--gap-study ${GAP_STUDY_INSTANCES}"
        echo "[pacs-init] 附带缺层演示检查（标称 ${GAP_STUDY_INSTANCES} 层）"
    fi
    # shellcheck disable=SC2086
    python -m simulator.generate_data --out "$DATA_DIR" --counts "${STUDY_COUNTS:-8,6,5}" $GAP_ARGS
else
    echo "[pacs-init] 复用已有数据：$DATA_DIR/studies.json"
fi
exec python -m simulator.pacs
