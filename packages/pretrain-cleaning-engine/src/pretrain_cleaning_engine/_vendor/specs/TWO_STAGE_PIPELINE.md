# Two-stage pipeline: align (pseudo v3) → clean (aggregated v3)

## Products

| Stage | Directory role | On-disk layout |
|-------|----------------|----------------|
| **A Align** | Format-aligned backup | Pseudo v3: `codebase_version=v3.0`, **one episode per** `data/.../file-*.parquet` and `videos/{key}/.../file-*.mp4` (no media concat) |
| **B Clean** | Training set | True aggregated v3 (MB-sized data/video shards) + recomputed `statistics_relative.json` |

Public **v2.1** roots (`episode_*.parquet` / `episode_*.mp4` as final output) are deprecated. Brief private staging during export is allowed and deleted when the job finishes.

## Path templates (both pseudo and aggregated)

Same as LeRobot / gr00t contract:

```text
data_path:  data/chunk-{chunk_index:03d}/file-{file_index:03d}.parquet
video_path: videos/{video_key}/chunk-{chunk_index:03d}/file-{file_index:03d}.mp4
```

Pseudo indexing: `chunk_index = ep // 1000`, `file_index = ep % 1000`,
`videos/*/from_timestamp = 0`, `to_timestamp = length / fps`.

## Config keys

```yaml
pipeline:
  mode: align | clean          # default clean (legacy single-pass behavior when input raw)
dataset:
  input_format: raw | pseudo_v3
export:
  layout: pseudo_v3 | aggregated_v3   # preferred over lerobot_version
  # lerobot_version still accepted: pseudo_v3|aggregated_v3|v3.0(=aggregated)|v2.1(legacy staging only)
```

## Stage A (align)

Runs: P2 → P0 → optional P4 lag stats → P1 → P4 → P6 → P7 (video normalize) → pseudo v3 + relative stats.  
Does **not** apply P3 discard / frame masks. `keep_all_frames=true`.

## Stage B (clean)

Runs: discover pseudo v3 → P3 quality on standard columns → streamed identity P1 + filtered export → **aggregated** v3 → relative stats rebuild.  
Skips P6, review_corrections, video re-encode unless an episode is truncated.

## CLI sketch

```bash
python run.py --config run_config_stage_a.yaml --output-dir $OUT/stage_a_aligned \
  --pipeline-mode align --force-skip-gate

python run.py --config run_config_stage_b.yaml --output-dir $OUT/stage_b_cleaned \
  --pipeline-mode clean --force-skip-gate
# stage_b config sets dataset.root / input_format: pseudo_v3 to stage_a output
```
