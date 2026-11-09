#!/bin/bash
set -euo pipefail

########################################
# ABC-130k Stage A (default: MCAP oneshot on ABC-130k-EEF)
#   WORLD_SIZE=8 RANK=<i> bash stage_a_align.sh
# Converted tree path:
#   CONFIG=run_config_stage_a.yaml bash stage_a_align.sh
#
# 可选: PILOT / CONFIG / SAMPLE_SIZE / MERGE_WORKERS / KEEP_SHARDS=1
########################################

cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")"  # package dir of this script (not the legacy Dataclean tree)
SAMPLE_SIZE=${SAMPLE_SIZE:-}
CONFIG=${CONFIG:-run_config_stage_a_mcap.yaml}
PILOT=${PILOT:-/mnt/pfs/datasets/Processed/abc130k_stageA}
EXPECT_WORLD_SIZE=${EXPECT_WORLD_SIZE:-8}
MERGE_WORKERS=${MERGE_WORKERS:-64}
RELATIVE_STATS_WORKERS=${RELATIVE_STATS_WORKERS:-64}
KEEP_SHARDS=${KEEP_SHARDS:-0}
PYTHON=${PYTHON:-python3}

export PILOT
export STATS=$PILOT/stats_cache_a
export MERGED=$PILOT/stage_a_aligned
export SYNC_DIR=$PILOT/.sync_a
export MERGE_WORKERS
export RELATIVE_STATS_WORKERS

RANK=${RANK:-0}
WORLD_SIZE=${WORLD_SIZE:-1}
# shellcheck source=_setup_pfs_tmp.sh
source /mnt/pfs/Data/liuweilin/Dataclean/datasets/abc_130k/_setup_pfs_tmp.sh

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
echo "ABC-130k Stage A  host=$(hostname) RANK=$RANK/$WORLD_SIZE"
echo "CONFIG=$CONFIG PILOT=$PILOT SAMPLE_SIZE=${SAMPLE_SIZE:-full}"
echo "LOG=$ABC130K_RANK_LOG"
echo "========================================"

SAMPLE_ARGS=()
if [[ -n "${SAMPLE_SIZE}" ]]; then
    SAMPLE_ARGS=(--sample-size "$SAMPLE_SIZE")
fi

COMMON_STATS=(
    --config "$CONFIG"
    --force-skip-gate
    --pipeline-mode align
    --stats-cache-dir "$STATS"
    --stats-only
    "${SAMPLE_ARGS[@]}"
)

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

SHARD_DIR="$PILOT/stage_a_shards/shard_${RANK}"
mkdir -p "$SHARD_DIR"

"$PYTHON" -u run.py \
    --config "$CONFIG" \
    --output-dir "$SHARD_DIR" \
    "${SAMPLE_ARGS[@]}" \
    --force-skip-gate \
    --pipeline-mode align \
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
        MERGE_ARGS+=(--shard-dir "$PILOT/stage_a_shards/shard_${i}")
    done
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
if n <= 0 or frames <= 0:
    raise SystemExit(f"invalid merge totals episodes={n} frames={frames}")
print(f"[cleanup] verified {out} episodes={n} frames={frames}", flush=True)
PY
        echo "[cleanup] removing unused $PILOT/stage_a_shards"
        rm -rf "$PILOT/stage_a_shards"
    fi
    touch "$SYNC_DIR/merge.done"
    echo "Stage A done → $MERGED"
else
    while [[ ! -f "$SYNC_DIR/merge.done" ]]; do sleep 5; done
fi
