# Dataclean Pipeline Phases (P0–P7)

This document is the operational companion to [`processing.md`](../processing.md) and [`standard_info.json`](../standard_info.json).

## Architecture

- **Shared engine**: `engine/robot_data_processing/` (symlink to `qwen-manip-preprocess/src/robot_data_processing`)
- **Dataset packages**: `datasets/<name>/` — keymap, adapter, review checklist, discover, run entry
- **Template**: copy `datasets/_template/` when onboarding a new dataset

## Phase order (fixed)

| Phase | Name | Shared / Custom | Responsibility |
|-------|------|-----------------|----------------|
| **P0** | Ingest | custom discover + shared loader | Enumerate episodes; load parquet / video / parameters into `EpisodeRef` |
| **P1** | SchemaNormalize | **custom adapter** + shared transforms | Source keys → Dataclean standard keys; rot→wxyz quat; gripper closedness; camera rename; drop if no `camera_top` |
| **P2** | ManualGate | **custom checklist** + shared gate | First slot `eef_direction`; `extrinsic_confidence` controls export of `extrinsic.*` only |
| **P3** | QualityFilter | **shared** Stage1–4 (Stage5 off by default) | Sudden change, DA, extremes, static shorten; no eef SE(3) by default |
| **P4** | TemporalAlign | **shared** + dataset `temporal_align` | Default: lag stats only. Optional `apply_delay` (align to delay=1) or `mode=manual` (`+k` delay / `-k` advance). Writes `state_action_delay` accordingly |
| **P5** | MediaNormalize | **shared** (+ camera map) | Standard camera names; width→384; GOP≤10 |
| **P6** | ParamsAndSpeed | shared algos + custom calib | Intrinsic/extrinsic `T_dst_src`; fps/timestamp check; ee speed vs golden@30fps |
| **P7** | StandardExport | **shared** | Write LeRobot `data/` `videos/` `meta/` `parameters/` aligned with `standard_info.json` |

## P3 embedding (unchanged semantics)

```
Stage1 sudden change
Stage2 trend / DA (episode discard)
Stage3 extreme value
prefix truncate + Stage4 static shorten → validity mask
Stage5 camera-frame align (optional)
```

P3 may still operate on the internal canonical numeric space for reuse; **P1 in / P7 out** must be standard fields. Adapters own the mapping.

Stage A export (`input_format: raw`, `output_mode: filter|both`) defaults to a process-pool stream: each worker does P1 → P6 → write parquet/mp4 immediately. Stage B identity P1 (`pseudo_v3`) uses the same pool without P6 / review_corrections. `export_workers` is the in-flight episode count. `load_raw` is optional (only non-parquet sources).

## ManualGate slots (P2)

| Slot | Meaning |
|------|---------|
| `eef_direction` | **First.** EEF frame / +Z forward **and** extrinsic motion check. Alias: `coord_frame`. Optional fix: `config.yaml` → `review_corrections.extrinsic_rotation_correction` with keys `camera_top.T_ArmLeft_CameraTop` / `camera_top.T_ArmRight_CameraTop` (`T'=R@T`; other extrinsics untouched). |
| `gripper_closedness` | 0=open, 1=closed. Optional fix: `config.yaml` → `review_corrections.gripper_closedness_correction.action` / `.state` (`c'=a*c+b`, default clip). |
| `camera_naming` | Standard camera names; `camera_top` present |
| `fps_stable` | Sampled timestamp stability |
| `extrinsic_confidence` | Human grade: `trusted` / `untrusted`. Controls **export** of `extrinsic.*` only; P6 still uses extrinsics to align EEF. New packs: no source extrinsics → `untrusted`; has extrinsics → `pending` until reviewed. If untrusted but source extrinsics are complete, store the same 4×4 under `extra_info.{standard name}`; `statistics_state.availability.reference_camera` stays false. |

Statuses: first four slots `pending | approved | blocked`. `extrinsic_confidence` uses `pending | trusted | untrusted`. Full `filter`/`both` export requires the first four `approved` and `extrinsic_confidence` resolved unless `--force-skip-gate`.
`assert_eef_direction_or_raise` runs at the start of `run_dataset_phases`.

## Output layout

```
<output_root>/
├── data/chunk-***/episode_******.parquet
├── videos/chunk-***/{camera_*}/...
├── meta/info.json   # standard_info + state_action_delay / fps / embodiment
├── parameters/
├── reports/
├── cache/
└── run_meta.json
```

Multi-task sources (EgoDex): mirror `part/task`; each task is an independent standard LeRobot root.

## Onboarding a dataset

1. `cp -r datasets/_template datasets/<name>`
2. Fill `keymap.md` / `keymap.json`, `review_checklist.yaml`, `config.yaml`
3. Implement `adapter.py` and `discover.py`
4. Run `python datasets/<name>/run.py --help`
