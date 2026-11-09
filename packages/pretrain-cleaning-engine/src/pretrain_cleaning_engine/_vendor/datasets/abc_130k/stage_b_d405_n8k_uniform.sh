#!/bin/bash
set -euo pipefail

########################################
# ABC-130k D405 Stage B 多机测速 / 参数检查：全局 uniform 抽 8000 条。
# 参数与全量 run_config_stage_b_d405.yaml 相同，仅 sample_mode=uniform。
#
# 每台机器各起一条，WORLD_SIZE 与全量计划一致（外推才成立），例如 8 机：
#   WORLD_SIZE=8 RANK=<i> bash stage_b_d405_n8k_uniform.sh
# SAMPLE_SIZE 在分片之前生效，8000 是全局条数。
# 输入: ABC-130k-D405-EEF-640x480-30fps-crop-repaired（只读）；视频输出短边 384（512x384）。
#
# 输出: $PILOT/stage_b_cleaned（质量报告在 $PILOT/stage_b_shards/shard_*/_quality/reports/）
# 日志: $PILOT/logs/stage_b_rank_N.log / stage_b_timing_rank_N.tsv /
#       timing_summary.txt（rank 0，含 ×90039/8000 线性外推）
# 可选覆盖: PILOT / SAMPLE_SIZE / STAGE_A_ROOT / TMPDIR / WAIT_TIMEOUT_SEC / MERGE_WORKERS / RELATIVE_STATS_WORKERS
# 重跑同一 PILOT：先 rm -rf $PILOT/.sync_b
########################################

cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")"
: "${WORLD_SIZE:?请设置 WORLD_SIZE，例如 WORLD_SIZE=8}"
: "${RANK:?请设置 RANK，例如 RANK=0}"
export PILOT=${PILOT:-/mnt/pfs/datasets/Processed/ABC-130k-D405_stageB_n8k_uniform_260929}
export CONFIG=run_config_stage_b_d405_n8k_uniform.yaml
export SAMPLE_SIZE=${SAMPLE_SIZE:-8000}
# 全量 Stage A episode 数（meta/info.json total_episodes）。
export FULL_EPISODES=${FULL_EPISODES:-90039}
exec bash stage_b_clean_d405.sh
