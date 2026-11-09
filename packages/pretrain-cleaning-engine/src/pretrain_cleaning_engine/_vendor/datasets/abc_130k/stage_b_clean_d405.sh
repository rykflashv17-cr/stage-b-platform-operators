#!/bin/bash
set -euo pipefail

########################################
# Stage B: ABC-130k D405 repaired (aggregated v3, 640x480 @ 30 fps, yam) → cleaned pseudo_v3
# Stage A review: review/audits/abc_130k/20260929_stageA_D405_repaired/REPORT.md
#   info.video_resolution comes from the reviewed patch (run_config_stage_b_d405.yaml::stage_a_info_patch);
#   videos are re-encoded to short side 384 (512x384), GOP 10 / CRF 18 (Stage A: 640x480, keyint 250).
# Multi-node stats (same as Stage A): pass1 (all ranks) → prepare_pass2 (rank 0) → pass2 (all) → finalize (rank 0)
# 用法: WORLD_SIZE=8 RANK=0..7 bash stage_b_clean.sh
# 需设置 STAGE_A_ROOT 指向 Stage A 伪 v3 输出
#
# 可选: PILOT / CONFIG / SAMPLE_SIZE / MERGE_WORKERS / RELATIVE_STATS_WORKERS / TMPDIR / WAIT_TIMEOUT_SEC
#       OUTPUT_LAYOUT=pseudo_v3（默认，与 Stage A 相同：每集一个 file，不 concat 视频）
#         aggregated_v3 目前不作为导出格式，仅保留开关
#       KEEP_SHARDS=1 保留 stage_b_shards（默认 1）
# 日志 (rank 0 结束时另写 $PILOT/logs/timing_summary.txt): $PILOT/logs/stage_b_rank_${RANK}.log，阶段耗时 $PILOT/logs/stage_b_timing_rank_${RANK}.tsv
# 重跑同一 PILOT 前先删掉 $PILOT/.sync_b
########################################

# Whole body is one { } block: bash parses it fully before running, so editing this file
# while a (multi-node) run is in progress cannot shift the read offset and corrupt commands.
{
cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")"  # package dir of this script (not the legacy Dataclean tree)
export TMPDIR=${TMPDIR:-/mnt/pfs/Data/liuweilin/Dataclean/tmp}
export TEMP=$TMPDIR
export TMP=$TMPDIR
mkdir -p "$TMPDIR"

SAMPLE_SIZE=${SAMPLE_SIZE:-}
CONFIG=${CONFIG:-run_config_stage_b_d405.yaml}
PILOT=${PILOT:-/mnt/pfs/datasets/Processed/ABC-130k-D405_stageB_260929}
STAGE_A_ROOT=${STAGE_A_ROOT:-/mnt/pfs/datasets/Processed/ABC-130k-D405-EEF-640x480-30fps-crop-repaired}
EXPECT_WORLD_SIZE=${EXPECT_WORLD_SIZE:-8}
MERGE_WORKERS=${MERGE_WORKERS:-128}
RELATIVE_STATS_WORKERS=${RELATIVE_STATS_WORKERS:-128}
OUTPUT_LAYOUT=${OUTPUT_LAYOUT:-pseudo_v3}
KEEP_SHARDS=${KEEP_SHARDS:-1}

export PILOT
export STATS=$PILOT/stats_cache_b
export MERGED=$PILOT/stage_b_cleaned
export SYNC_DIR=$PILOT/.sync_b
export MERGE_WORKERS
export RELATIVE_STATS_WORKERS

RANK=${RANK:-0}
WORLD_SIZE=${WORLD_SIZE:-8}

if [[ "$OUTPUT_LAYOUT" != "aggregated_v3" && "$OUTPUT_LAYOUT" != "pseudo_v3" ]]; then
    echo "ERROR: OUTPUT_LAYOUT must be aggregated_v3 or pseudo_v3, got: $OUTPUT_LAYOUT" >&2
    exit 1
fi

STAGE_TAG=stage_b
# Shared with humanoid_merged (like merge_shards.py): per-rank log/timing, failure flags, wait timeout.
source ../humanoid_merged/_multinode_common.sh

echo "========================================"
echo "ABC-130k D405 Stage B  host=$(hostname) RANK=$RANK/$WORLD_SIZE  $(date '+%F %T')"
echo "STAGE_A_ROOT=$STAGE_A_ROOT CONFIG=$CONFIG"
echo "PILOT=$PILOT OUTPUT_LAYOUT=$OUTPUT_LAYOUT SAMPLE_SIZE=${SAMPLE_SIZE:-full} KEEP_SHARDS=$KEEP_SHARDS"
echo "MERGE_WORKERS=$MERGE_WORKERS RELATIVE_STATS_WORKERS=$RELATIVE_STATS_WORKERS"
echo "LOG=$RANK_LOG"
echo "========================================"
if [[ "$WORLD_SIZE" -ne "$EXPECT_WORLD_SIZE" ]]; then
    echo "WARN: WORLD_SIZE=$WORLD_SIZE != EXPECT_WORLD_SIZE=$EXPECT_WORLD_SIZE" >&2
fi

if [[ ! -f "$STAGE_A_ROOT/meta/info.json" ]]; then
    echo "ERROR: Stage A root missing info.json: $STAGE_A_ROOT" >&2
    exit 1
fi

mkdir -p "$PILOT" "$STATS"

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

# ---------- Multi-node stats ----------
if [[ "$RANK" == "0" ]]; then
    rm -f "$SYNC_DIR"/stats_*.done "$SYNC_DIR"/shard_*.done "$SYNC_DIR/merge.done" "$SYNC_DIR"/failed_rank_*
    rm -rf "$STATS/partials" "$STATS/bounds"
    mkdir -p "$STATS/partials" "$STATS/bounds"
    touch "$SYNC_DIR/stats_ready.done"
else
    wait_for "$SYNC_DIR/stats_ready.done" 2
fi

run_phase stats_pass1 python3 -u run.py \
    "${COMMON_STATS[@]}" \
    --output-dir "$PILOT/stage_b_stats_pass1/shard_${RANK}" \
    --stats-phase pass1 \
    --shard-id "$RANK" \
    --num-shards "$WORLD_SIZE"
touch "$SYNC_DIR/stats_pass1_shard_${RANK}.done"

if [[ "$RANK" == "0" ]]; then
    wait_all_shards stats_pass1_shard_
    run_phase stats_prepare_pass2 python3 -u run.py \
        "${COMMON_STATS[@]}" \
        --output-dir "$PILOT/stage_b_stats_prepare" \
        --stats-phase prepare_pass2 \
        --num-shards "$WORLD_SIZE"
    touch "$SYNC_DIR/stats_pass1.done"
else
    wait_for "$SYNC_DIR/stats_pass1.done" 3
fi

run_phase stats_pass2 python3 -u run.py \
    "${COMMON_STATS[@]}" \
    --output-dir "$PILOT/stage_b_stats_pass2/shard_${RANK}" \
    --stats-phase pass2 \
    --shard-id "$RANK" \
    --num-shards "$WORLD_SIZE"
touch "$SYNC_DIR/stats_pass2_shard_${RANK}.done"

if [[ "$RANK" == "0" ]]; then
    wait_all_shards stats_pass2_shard_
    run_phase stats_finalize python3 -u run.py \
        "${COMMON_STATS[@]}" \
        --output-dir "$PILOT/stage_b_stats_finalize" \
        --stats-phase finalize \
        --num-shards "$WORLD_SIZE"
    touch "$SYNC_DIR/stats.done"
else
    wait_for "$SYNC_DIR/stats.done" 3
fi

# ---------- Shard clean export ----------
SHARD_DIR="$PILOT/stage_b_shards/shard_${RANK}"
mkdir -p "$SHARD_DIR"

run_phase shard_export python3 -u run.py \
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
    wait_all_shards shard_
    MERGE_ARGS=()
    for ((i = 0; i < WORLD_SIZE; i++)); do
        MERGE_ARGS+=(--shard-dir "$PILOT/stage_b_shards/shard_${i}")
    done
    run_phase merge python3 -u merge_shards.py \
        "${MERGE_ARGS[@]}" \
        --output-dir "$MERGED" \
        --config "$CONFIG" \
        --output-layout "$OUTPUT_LAYOUT" \
        --merge-workers "$MERGE_WORKERS" \
        --relative-stats-workers "$RELATIVE_STATS_WORKERS"
    if [[ "$KEEP_SHARDS" != "1" ]]; then
        python3 - "$MERGED" <<'PY'
import json, sys
from pathlib import Path
info = json.loads((Path(sys.argv[1]) / "meta" / "info.json").read_text(encoding="utf-8"))
if int(info.get("total_episodes") or 0) <= 0 or int(info.get("total_frames") or 0) <= 0:
    raise SystemExit(f"invalid merge totals: {info.get('total_episodes')=} {info.get('total_frames')=}")
PY
        echo "[cleanup] removing $PILOT/stage_b_shards"
        rm -rf "$PILOT/stage_b_shards"
    fi
    touch "$SYNC_DIR/merge.done"
    echo "Stage B done → $MERGED  wall=$(( $(date +%s) - STAGE_T0 ))s"
else
    wait_for "$SYNC_DIR/merge.done" 5
fi

if [[ "$RANK" == "0" ]]; then
    # Per-phase min/mean/max across ranks; linear extrapolation when TIMING_SAMPLE_SIZE/FULL_EPISODES are set.
    TIMING_SAMPLE_SIZE=${TIMING_SAMPLE_SIZE:-$SAMPLE_SIZE}
    python3 ../humanoid_merged/summarize_timing.py --pilot "$PILOT" \
        ${TIMING_SAMPLE_SIZE:+--sample-size "$TIMING_SAMPLE_SIZE"} \
        ${FULL_EPISODES:+--full-episodes "$FULL_EPISODES"} \
        --out "$PILOT/logs/timing_summary.txt" || echo "WARN: summarize_timing failed" >&2
fi
}; exit
