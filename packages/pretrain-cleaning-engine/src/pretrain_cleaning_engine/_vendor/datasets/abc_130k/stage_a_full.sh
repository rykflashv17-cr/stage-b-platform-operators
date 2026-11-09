#!/bin/bash
set -euo pipefail

########################################
# ABC-130k 全量 Stage A（多机）
#
# 源: /mnt/pfs/datasets/ABC-130k-EEF  (~109k train，已排除无 eef_pose.parquet)
# 出: $PILOT/stage_a_aligned          (pseudo_v3, dataset.fps=30)
# 60Hz 集在 adapter 隔帧 → 30Hz；原生 ~30Hz 不动。
#
# 每节点各跑一份（RANK=0..WORLD_SIZE-1）:
#   WORLD_SIZE=8 RANK=<i> bash stage_a_full.sh
#
# 可选环境变量:
#   PILOT                    默认 /mnt/pfs/Data/liuweilin/Dataclean/outputs/abc130k_stageA
#   WORLD_SIZE / RANK        默认 8 / 0
#   CONFIG                   默认 run_config_stage_a_mcap_8node.yaml
#   MERGE_WORKERS            默认 64（rank0 merge）
#   RELATIVE_STATS_WORKERS   默认 64
#   KEEP_SHARDS=1            合并后保留 stage_a_shards
#   ABC130K_VIDEO_CACHE      默认 /tmp/abc130k_video_cache_r${RANK} → Dataclean/tmp（防节点 /tmp 爆盘）
#   ABC130K_PFS_TMP          默认 /mnt/pfs/Data/liuweilin/Dataclean/tmp
#   PYTHON                   默认 python3
#
# 流程: 并行 stats pass1 → rank0 prepare → 并行 pass2 → rank0 finalize
#       → 并行 --export-shard-only → rank0 merge_shards.py
########################################

cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")"  # package dir of this script (not the legacy Dataclean tree)
export PYTHONUNBUFFERED=1

CONFIG=${CONFIG:-run_config_stage_a_mcap_8node.yaml}
PILOT=${PILOT:-/mnt/pfs/datasets/Processed/ABC130k_stageA}
EXPECT_WORLD_SIZE=${EXPECT_WORLD_SIZE:-8}
MERGE_WORKERS=${MERGE_WORKERS:-64}
RELATIVE_STATS_WORKERS=${RELATIVE_STATS_WORKERS:-64}
KEEP_SHARDS=${KEEP_SHARDS:-0}
PYTHON=${PYTHON:-python3}

RANK=${RANK:-0}
WORLD_SIZE=${WORLD_SIZE:-8}

export PILOT
export STATS=$PILOT/stats_cache_a
export MERGED=$PILOT/stage_a_aligned
export SYNC_DIR=$PILOT/.sync_a
export MERGE_WORKERS
export RELATIVE_STATS_WORKERS
export ABC130K_SPLITS=${ABC130K_SPLITS:-train}
# shellcheck source=_setup_pfs_tmp.sh
source /mnt/pfs/Data/liuweilin/Dataclean/datasets/abc_130k/_setup_pfs_tmp.sh

if [[ "$WORLD_SIZE" -ne "$EXPECT_WORLD_SIZE" ]]; then
    echo "WARN: WORLD_SIZE=$WORLD_SIZE != EXPECT_WORLD_SIZE=$EXPECT_WORLD_SIZE" >&2
fi

mkdir -p "$PILOT" "$STATS" "$SYNC_DIR" "$PILOT/logs"
if [[ -z "${ABC130K_RANK_LOG:-}" ]]; then
    export ABC130K_RANK_LOG="$PILOT/logs/rank_${RANK}.log"
    if command -v stdbuf >/dev/null 2>&1; then
        exec > >(stdbuf -oL -eL tee -a "$ABC130K_RANK_LOG") 2>&1
    else
        exec > >(tee -a "$ABC130K_RANK_LOG") 2>&1
    fi
fi

fmt_elapsed() {
    local s=$1
    printf '%dh%02dm%02ds' $((s / 3600)) $(((s % 3600) / 60)) $((s % 60))
}

phase_start() {
    PHASE_NAME=$1
    PHASE_T0=$(date +%s)
    echo "[timing] ${PHASE_NAME} start $(date '+%Y-%m-%d %H:%M:%S') RANK=$RANK"
}

phase_end() {
    local t1 elapsed
    t1=$(date +%s)
    elapsed=$((t1 - PHASE_T0))
    echo "[timing] ${PHASE_NAME} end   $(date '+%Y-%m-%d %H:%M:%S') RANK=$RANK elapsed=$(fmt_elapsed "$elapsed") (${elapsed}s)"
}

echo "========================================"
echo "ABC-130k Stage A FULL  host=$(hostname) RANK=$RANK/$WORLD_SIZE"
echo "CONFIG=$CONFIG"
echo "PILOT=$PILOT"
echo "MERGED=$MERGED"
echo "root=/mnt/pfs/datasets/ABC-130k-EEF  fps=30  60Hz→stride-2"
echo "index=.cache/episode_index_train_eef.json  rebuild_index=false"
echo "MERGE_WORKERS=$MERGE_WORKERS RELATIVE_STATS_WORKERS=$RELATIVE_STATS_WORKERS"
echo "ABC130K_VIDEO_CACHE=$ABC130K_VIDEO_CACHE"
echo "LOG=$ABC130K_RANK_LOG"
echo "========================================"

COMMON_STATS=(
    --config "$CONFIG"
    --force-skip-gate
    --pipeline-mode align
    --stats-cache-dir "$STATS"
    --stats-only
)

SCRIPT_T0=$(date +%s)

if [[ "$RANK" == "0" ]]; then
    rm -f "$SYNC_DIR"/stats_*.done "$SYNC_DIR"/shard_*.done "$SYNC_DIR/merge.done" "$SYNC_DIR"/resolution*.done "$SYNC_DIR/resolution.failed"
    rm -rf "$STATS/video_resolution"
    rm -rf "$STATS/partials" "$STATS/bounds"
    mkdir -p "$STATS/partials" "$STATS/bounds"
    touch "$SYNC_DIR/stats_ready.done"
else
    while [[ ! -f "$SYNC_DIR/stats_ready.done" ]]; do sleep 2; done
fi

# P0/resolution: per-camera video resolution census (all ranks) -> plan (rank 0).
# Episodes dropped by the plan are excluded from stats and export; >25% dropped fails the run.
RES_FLAG="$SYNC_DIR/resolution.done"
RES_FAIL="$SYNC_DIR/resolution.failed"
"$PYTHON" -u run.py \
    "${COMMON_STATS[@]}" \
    --output-dir "$PILOT/resolution_census/shard_${RANK}" \
    --stats-phase resolution_census \
    --shard-id "$RANK" \
    --num-shards "$WORLD_SIZE"
touch "$SYNC_DIR/resolution_census_shard_${RANK}.done"
if [[ "$RANK" == "0" ]]; then
    for ((i = 0; i < WORLD_SIZE; i++)); do
        while [[ ! -f "$SYNC_DIR/resolution_census_shard_${i}.done" ]]; do sleep 3; done
    done
    if ! "$PYTHON" -u run.py \
        "${COMMON_STATS[@]}" \
        --output-dir "$PILOT/resolution_plan" \
        --stats-phase resolution_plan \
        --num-shards "$WORLD_SIZE"; then
        touch "$RES_FAIL"
        echo "[resolution] FAIL: see $STATS/video_resolution/plan.json" >&2
        exit 1
    fi
    touch "$RES_FLAG"
else
    while [[ ! -f "$RES_FLAG" ]]; do
        if [[ -f "$RES_FAIL" ]]; then
            echo "[resolution] rank 0 reported FAIL; exiting" >&2
            exit 1
        fi
        sleep 3
    done
fi

phase_start "stats_pass1"
"$PYTHON" -u run.py \
    "${COMMON_STATS[@]}" \
    --output-dir "$PILOT/stats_pass1/shard_${RANK}" \
    --stats-phase pass1 \
    --shard-id "$RANK" \
    --num-shards "$WORLD_SIZE"
touch "$SYNC_DIR/stats_pass1_shard_${RANK}.done"
phase_end

if [[ "$RANK" == "0" ]]; then
    for ((i = 0; i < WORLD_SIZE; i++)); do
        while [[ ! -f "$SYNC_DIR/stats_pass1_shard_${i}.done" ]]; do sleep 3; done
    done
    phase_start "stats_prepare_pass2"
    "$PYTHON" -u run.py \
        "${COMMON_STATS[@]}" \
        --output-dir "$PILOT/stats_prepare" \
        --stats-phase prepare_pass2 \
        --num-shards "$WORLD_SIZE"
    touch "$SYNC_DIR/stats_pass1.done"
    phase_end
else
    while [[ ! -f "$SYNC_DIR/stats_pass1.done" ]]; do sleep 3; done
fi

phase_start "stats_pass2"
"$PYTHON" -u run.py \
    "${COMMON_STATS[@]}" \
    --output-dir "$PILOT/stats_pass2/shard_${RANK}" \
    --stats-phase pass2 \
    --shard-id "$RANK" \
    --num-shards "$WORLD_SIZE"
touch "$SYNC_DIR/stats_pass2_shard_${RANK}.done"
phase_end

if [[ "$RANK" == "0" ]]; then
    for ((i = 0; i < WORLD_SIZE; i++)); do
        while [[ ! -f "$SYNC_DIR/stats_pass2_shard_${i}.done" ]]; do sleep 3; done
    done
    phase_start "stats_finalize"
    "$PYTHON" -u run.py \
        "${COMMON_STATS[@]}" \
        --output-dir "$PILOT/stats_finalize" \
        --stats-phase finalize \
        --num-shards "$WORLD_SIZE"
    touch "$SYNC_DIR/stats.done"
    phase_end
else
    while [[ ! -f "$SYNC_DIR/stats.done" ]]; do sleep 3; done
fi

SHARD_DIR="$PILOT/stage_a_shards/shard_${RANK}"
mkdir -p "$SHARD_DIR"

phase_start "export_shard"
"$PYTHON" -u run.py \
    --config "$CONFIG" \
    --output-dir "$SHARD_DIR" \
    --force-skip-gate \
    --pipeline-mode align \
    --stats-cache-dir "$STATS" \
    --shard-id "$RANK" \
    --num-shards "$WORLD_SIZE" \
    --export-shard-only
touch "$SYNC_DIR/shard_${RANK}.done"
phase_end

if [[ "$RANK" == "0" ]]; then
    for ((i = 0; i < WORLD_SIZE; i++)); do
        while [[ ! -f "$SYNC_DIR/shard_${i}.done" ]]; do sleep 5; done
    done
    MERGE_ARGS=()
    for ((i = 0; i < WORLD_SIZE; i++)); do
        MERGE_ARGS+=(--shard-dir "$PILOT/stage_a_shards/shard_${i}")
    done
    phase_start "merge"
    "$PYTHON" -u merge_shards.py \
        "${MERGE_ARGS[@]}" \
        --output-dir "$MERGED" \
        --config "$CONFIG" \
        --output-layout pseudo_v3 \
        --merge-workers "$MERGE_WORKERS" \
        --relative-stats-workers "$RELATIVE_STATS_WORKERS"
    if [[ "$KEEP_SHARDS" == "1" ]]; then
        echo "[cleanup] KEEP_SHARDS=1, leave $PILOT/stage_a_shards"
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
fps = info.get("fps")
if n <= 0 or frames <= 0:
    raise SystemExit(f"invalid merge totals episodes={n} frames={frames}")
print(f"[cleanup] verified {out} episodes={n} frames={frames} fps={fps}", flush=True)
PY
        echo "[cleanup] removing unused $PILOT/stage_a_shards"
        rm -rf "$PILOT/stage_a_shards"
    fi
    touch "$SYNC_DIR/merge.done"
    phase_end
    if [[ -f "$MERGED/merge_report.json" ]]; then
        "$PYTHON" -c "import json; r=json.load(open('$MERGED/merge_report.json')); print('[merge] episodes=', r.get('total_episodes'), 'frames=', r.get('total_frames'), 'phase_timing=', r.get('phase_timing'))"
    fi
    SCRIPT_ELAPSED=$(( $(date +%s) - SCRIPT_T0 ))
    echo "[timing] TOTAL wall (rank0) $(fmt_elapsed "$SCRIPT_ELAPSED") (${SCRIPT_ELAPSED}s)"
    echo "Stage A FULL done → $MERGED"
else
    while [[ ! -f "$SYNC_DIR/merge.done" ]]; do sleep 5; done
    SCRIPT_ELAPSED=$(( $(date +%s) - SCRIPT_T0 ))
    echo "[timing] TOTAL wall (rank$RANK wait+own) $(fmt_elapsed "$SCRIPT_ELAPSED") (${SCRIPT_ELAPSED}s)"
fi
