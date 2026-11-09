# ABC-130k MCAP + official EEF → Dataclean keymap

## Source

| 树 | 路径 | 用途 |
|---|---|---|
| 原 MCAP | `/mnt/pfs/datasets/ABC-130k` | 关节 / 夹爪 / 视频 / 相机 K |
| EEF 补齐 | `/mnt/pfs/datasets/ABC-130k-EEF` | **EEF 以这里为准**（`eef_pose.parquet`）；`episode.mcap` 为指向原树的 symlink |

Stage A `dataset.root` 指向 **EEF 树**。discover 会跳过没有 parquet 的集（官方提取排除约 20k）。

- Layout: `data/{train|val}/{task}/episode_<uuid>/{episode.mcap,annotation.mcap,eef_pose.parquet}`
- Robot: YAM bimanual (6+1)×2
- Gripper: aperture **0=closed, 1=open** → Dataclean closedness **`1 - aperture`**
- Cameras: RealSense D405 或 ZED-X；convert / oneshot 把 top / top-left 当作 `camera_top`

## Sync

各关节 / 夹爪 / EEF parquet 行 **不是** 与相机同索引。`load_raw` 把所有流 nearest-neighbor 到参考相机时间轴。

## Packed 14d（Stage A quality）

`[L_j1..L_j6, L_grip_closedness, R_j1..R_j6, R_grip_closedness]`

EEF **不进** Stage A quality 14d（与 Agibot Stage A 只打关节+夹爪同一套路）。

## EE pose

`eef_pose.parquet`：`topic` ∈ `{/left,/right}-arm-{state,action}`，`pose` = row-major 4×4，**frame = `left_arm_base`**。  
右臂基座平移按官方提取假设 `[0, -0.61, 0]` m（D405 cohort，非逐站标定）。  
导出 `observation.state|action.eef.{left,right}.{position,rotation_6d}`。

## 外参

MCAP 有 `CameraCalibration`（K），**没有** `T_World_Camera` / 手眼。  
`extrinsic_confidence: untrusted`：不导出 `extrinsic.*`，也不写单位阵冒充 cam_ref@t0。
