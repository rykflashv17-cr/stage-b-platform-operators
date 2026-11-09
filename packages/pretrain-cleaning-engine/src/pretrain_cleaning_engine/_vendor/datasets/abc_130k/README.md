# ABC-130k

YAM 双臂遥操作。原 MCAP 在 `/mnt/pfs/datasets/ABC-130k`；**EEF 以** `/mnt/pfs/datasets/ABC-130k-EEF` **为准**（官方 repo FK：`eef_pose.parquet`，`episode.mcap` 为指向原树的 symlink）。

默认 **oneshot**：ABC-130k-EEF → Stage A pseudo_v3（经 adapter hooks，无需 convert 中间树）。

参照 robot 包 [`AgibotWorld_Beta`](../AgibotWorld_Beta/) + [`humanoid_merged`](../humanoid_merged/)。

## 映射要点

| 项 | 处理 |
|---|---|
| 关节 | 12d → L6 + R6 |
| 夹爪 | MCAP aperture 0=闭 1=开 → closedness `clip(1-aperture,0,1)` |
| EEF | `eef_pose.parquet` row-major 4×4，**left_arm_base**；NN 到相机时间轴 → xyz + rotation_6d |
| 外参 | 无 `T_World_Camera` → `extrinsic_confidence: untrusted`，不导出 `extrinsic.*` |
| 内参 | MCAP CameraCalibration K（有则写入 `camera_intrinsics`，短边 384 缩放） |
| FPS | 源混有 ~30 / ~60 Hz。adapter 对 60Hz 隔帧 → 30Hz；`dataset.fps: 30` |
| Quality A | 14d = L6+Lg+R6+Rg（EEF 导出但不进 A 的 quality 向量） |
| Quality B | 32d xyz+rot6d（InternData/humanoid canvas） |

## Stage A 小样

```bash
cd datasets/abc_130k
python3 run.py --config run_config_stage_a_mcap.yaml \
  --output-dir /tmp/abc130k_a_smoke \
  --pipeline-mode align --sample-size 2 --force-skip-gate
```

8 机全量 train（~109k，60Hz 隔帧→30Hz）：

```bash
WORLD_SIZE=8 RANK=<i> bash stage_a_full.sh
```

产出 `$PILOT/stage_a_aligned`（默认 `outputs/abc130k_stageA`）。视频缓存本机 `/tmp/abc130k_video_cache_r${RANK}`。

## Stage B

Identity P1：不重做 P6 / delay / gripper affine。Quality 用 `quality_stage_b.yaml`。

```bash
SAMPLE_SIZE=20 WORLD_SIZE=1 RANK=0 \
  STAGE_A_ROOT=/path/to/stage_a_aligned \
  PILOT=/tmp/abc130k_b_smoke \
  bash stage_b_clean.sh

WORLD_SIZE=8 RANK=<i> STAGE_A_ROOT=.../stage_a_aligned bash stage_b_clean.sh
```

## 包文件

| 文件 | 作用 |
|------|------|
| `mcap_io.py` | Discover / 同步 / Annex-B→mp4 / 读 `eef_pose.parquet` |
| `adapter.py` | `load_raw` / identity Stage B / `resolve_source_video` |
| `episode_geometry.py` | P6：EEF 留在 left_arm_base，不编造单位阵外参 |
| `run_config_stage_a_mcap.yaml` | Stage A oneshot |
| `run_config_stage_b.yaml` | Stage B identity + 32d quality |
| `quality.yaml` / `quality_stage_b.yaml` | 14d / 32d |
| `stage_a_align.sh` / `stage_a_align_mcap_8node.sh` | 多机 A |
| `stage_b_clean.sh` | 多机 B |

## 依赖

```bash
pip install -r requirements.txt   # mcap, mcap-protobuf-support, pyarrow
```

可选两步 convert（**不含 EEF**，一般不要走这条）：`python convert.py ...` 再 `CONFIG=run_config_stage_a.yaml`。
