#!/bin/bash
set -euo pipefail

########################################
# Stage B: clean Stage-A pseudo_v3 (ABC-130k)
# 用法: WORLD_SIZE=8 RANK=0..7 bash stage_b_clean.sh
# stats 分片：各 RANK --stats-phase pass1/pass2，RANK0 prepare_pass2/finalize。
# 需设置 STAGE_A_ROOT 指向 Stage A stage_a_aligned
# 可选: SAMPLE_SIZE / PILOT / OUTPUT_LAYOUT=aggregated_v3|pseudo_v3 / KEEP_SHARDS=1
########################################

cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")"  # package dir of this script (not the legacy Dataclean tree)
SAMPLE_SIZE=${SAMPLE_SIZE:-}
CONFIG=${CONFIG:-run_config_stage_b.yaml}
PILOT=${PILOT:-/mnt/pfs/datasets/Processed/abc130k_stageB}
STAGE_A_ROOT=${STAGE_A_ROOT:-/mnt/pfs/datasets/Processed/abc130k_stageA/stage_a_aligned}
MERGE_WORKERS=${MERGE_WORKERS:-64}
RELATIVE_STATS_WORKERS=${RELATIVE_STATS_WORKERS:-64}
OUTPUT_LAYOUT=${OUTPUT_LAYOUT:-aggregated_v3}
KEEP_SHARDS=${KEEP_SHARDS:-0}
PYTHON=${PYTHON:-python3}

RANK=${RANK:-0}
WORLD_SIZE=${WORLD_SIZE:-1}
# shellcheck source=_setup_pfs_tmp.sh
source /mnt/pfs/Data/liuweilin/Dataclean/datasets/abc_130k/_setup_pfs_tmp.sh

export PILOT
export STATS=$PILOT/stats_cache_b
export MERGED=$PILOT/stage_b_cleaned
export SYNC_DIR=$PILOT/.sync_b
export MERGE_WORKERS
export RELATIVE_STATS_WORKERS

if [[ "$OUTPUT_LAYOUT" != "aggregated_v3" && "$OUTPUT_LAYOUT" != "pseudo_v3" ]]; then
    echo "ERROR: OUTPUT_LAYOUT must be aggregated_v3 or pseudo_v3, got: $OUTPUT_LAYOUT" >&2
    exit 1
fi
if [[ ! -f "$STAGE_A_ROOT/meta/info.json" ]]; then
    echo "ERROR: Stage A root missing info.json: $STAGE_A_ROOT" >&2
    exit 1
fi

mkdir -p "$PILOT" "$STATS" "$SYNC_DIR" "$PILOT/logs"
if [[ -z "${ABC130K_RANK_LOG:-}" ]]; then
    export ABC130K_RANK_LOG="$PILOT/logs/rank_${RANK}.log"
    export PYTHONUNBUFFERED=1
    if command -v stdbuf >/dev/null 2>&1; then
        exec > >(stdbuf -oL -eL tee -a "$ABC130K_RANK_LOG") 2>&1
    else
        exec > >(tee -a "$ABC130K_RANK_LOG") 2>&1
    fi
fi

echo "========================================"
echo "Stage B Clean  host=$(hostname) RANK=$RANK/$WORLD_SIZE"
echo "STAGE_A_ROOT=$STAGE_A_ROOT CONFIG=$CONFIG"
echo "PILOT=$PILOT OUTPUT_LAYOUT=$OUTPUT_LAYOUT SAMPLE_SIZE=${SAMPLE_SIZE:-full}"
echo "LOG=$ABC130K_RANK_LOG"
echo "========================================"

SAMPLE_ARGS=()
if [[ -n "${SAMPLE_SIZE}" ]]; then
    SAMPLE_ARGS=(--sample-size "$SAMPLE_SIZE")
fi

COMMON_STATS=(
    --config "$CONFIG"
    --force-skip-gate
    --pipeline-mode clean
    --input-root "$STAGE_A_ROOT"
    --input-format pseudo_v3
    --stats-cache-dir "$STATS"
    --stats-only
    "${SAMPLE_ARGS[@]}"
)

T0=$(date +%s)

if [[ "$RANK" == "0" ]]; then
    rm -f "$SYNC_DIR"/stats_*.done "$SYNC_DIR"/shard_*.done "$SYNC_DIR/merge.done"
    rm -rf "$STATS/partials" "$STATS/bounds"
    mkdir -p "$STATS/partials" "$STATS/bounds"
    touch "$SYNC_DIR/stats_ready.done"
else
    while [[ ! -f "$SYNC_DIR/stats_ready.done" ]]; do sleep 2; done
fi

echo "[timing] stats_pass1 start $(date '+%Y-%m-%d %H:%M:%S') RANK=$RANK"
"$PYTHON" -u run.py \
    "${COMMON_STATS[@]}" \
    --output-dir "$PILOT/stats_pass1/shard_${RANK}" \
    --stats-phase pass1 \
    --shard-id "$RANK" \
    --num-shards "$WORLD_SIZE"
touch "$SYNC_DIR/stats_pass1_shard_${RANK}.done"

if [[ "$RANK" == "0" ]]; then
    for ((i = 0; i < WORLD_SIZE; i++)); do
        while [[ ! -f "$SYNC_DIR/stats_pass1_shard_${i}.done" ]]; do sleep 3; done
    done
    "$PYTHON" -u run.py \
        "${COMMON_STATS[@]}" \
        --output-dir "$PILOT/stats_prepare" \
        --stats-phase prepare_pass2 \
        --num-shards "$WORLD_SIZE"
    touch "$SYNC_DIR/stats_pass1.done"
else
    while [[ ! -f "$SYNC_DIR/stats_pass1.done" ]]; do sleep 3; done
fi

echo "[timing] stats_pass2 start $(date '+%Y-%m-%d %H:%M:%S') RANK=$RANK"
"$PYTHON" -u run.py \
    "${COMMON_STATS[@]}" \
    --output-dir "$PILOT/stats_pass2/shard_${RANK}" \
    --stats-phase pass2 \
    --shard-id "$RANK" \
    --num-shards "$WORLD_SIZE"
touch "$SYNC_DIR/stats_pass2_shard_${RANK}.done"

if [[ "$RANK" == "0" ]]; then
    for ((i = 0; i < WORLD_SIZE; i++)); do
        while [[ ! -f "$SYNC_DIR/stats_pass2_shard_${i}.done" ]]; do sleep 3; done
    done
    "$PYTHON" -u run.py \
        "${COMMON_STATS[@]}" \
        --output-dir "$PILOT/stats_finalize" \
        --stats-phase finalize \
        --num-shards "$WORLD_SIZE"
    touch "$SYNC_DIR/stats.done"
else
    while [[ ! -f "$SYNC_DIR/stats.done" ]]; do sleep 3; done
fi

SHARD_DIR="$PILOT/stage_b_shards/shard_${RANK}"
mkdir -p "$SHARD_DIR"
echo "[timing] shard_${RANK} start $(date '+%Y-%m-%d %H:%M:%S')"
"$PYTHON" -u run.py \
    --config "$CONFIG" \
    --output-dir "$SHARD_DIR" \
    --input-root "$STAGE_A_ROOT" \
    --input-format pseudo_v3 \
    --pipeline-mode clean \
    --export-layout "$OUTPUT_LAYOUT" \
    "${SAMPLE_ARGS[@]}" \
    --force-skip-gate \
    --stats-cache-dir "$STATS" \
    --shard-id "$RANK" \
    --num-shards "$WORLD_SIZE" \
    --export-shard-only

touch "$SYNC_DIR/shard_${RANK}.done"

if [[ "$RANK" == "0" ]]; then
    for ((i = 0; i < WORLD_SIZE; i++)); do
        while [[ ! -f "$SYNC_DIR/shard_${i}.done" ]]; do sleep 5; done
    done
    if [[ ! -e merge_shards.py ]]; then
        ln -sfn ../humanoid_merged/merge_shards.py merge_shards.py
    fi
    MERGE_ARGS=()
    for ((i = 0; i < WORLD_SIZE; i++)); do
        MERGE_ARGS+=(--shard-dir "$PILOT/stage_b_shards/shard_${i}")
    done
    echo "[timing] merge start $(date '+%Y-%m-%d %H:%M:%S')"
    "$PYTHON" -u merge_shards.py \
        "${MERGE_ARGS[@]}" \
        --output-dir "$MERGED" \
        --config "$CONFIG" \
        --output-layout "$OUTPUT_LAYOUT" \
        --merge-workers "$MERGE_WORKERS" \
        --relative-stats-workers "$RELATIVE_STATS_WORKERS"
    if [[ "$KEEP_SHARDS" == "1" ]]; then
        echo "[cleanup] KEEP_SHARDS=1, leave $PILOT/stage_b_shards"
    else
        "$PYTHON" -u - "$MERGED" <<'PY'
import json
import sys
from pathlib import Path

out = Path(sys.argv[1])
info_path = out / "meta" / "info.json"
if not info_path.is_file():
    raise SystemExit(f"merge output missing {info_path}")
info = json.loads(info_path.read_text(encoding="utf-8"))
n = int(info.get("total_episodes") or 0)
frames = int(info.get("total_frames") or 0)
if n <= 0 or frames <= 0:
    raise SystemExit(f"invalid merge totals episodes={n} frames={frames}")
print(f"[cleanup] verified {out} episodes={n} frames={frames}", flush=True)
PY
        echo "[cleanup] removing unused $PILOT/stage_b_shards"
        rm -rf "$PILOT/stage_b_shards"
    fi
    touch "$SYNC_DIR/merge.done"
    echo "Stage B done → $MERGED  wall=$(( $(date +%s) - T0 ))s"
else
    while [[ ! -f "$SYNC_DIR/merge.done" ]]; do sleep 5; done
fi
