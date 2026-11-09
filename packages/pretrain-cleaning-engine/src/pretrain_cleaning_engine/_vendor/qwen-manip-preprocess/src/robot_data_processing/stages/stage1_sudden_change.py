from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from robot_data_processing.schema import DatasetSchema, HUMANOID_SCHEMA, max_contiguous_run
from robot_data_processing.mask import apply_per_dim_exclude
from robot_data_processing.smoothing import detect_sudden_changes_2d, detect_sudden_changes_2d_with_thresholds
from robot_data_processing.stage1_stats import Stage1GlobalStats


@dataclass
class GripperFaultConfig:
    """Gripper-specific fault check, used when the sudden-change check exempts the gripper.

    Normal open/close is fast and smooth, and state may stop short of action while grasping,
    so only these are faults (each flags frames as stage1 abnormal):

    * spike: a step of ``spike_min_step`` or more that is undone within ``spike_window`` frames
      (``|x[t+w] - x[t-w]| < spike_return_ratio * |step|``);
    * chatter: ``chatter_reversals`` or more direction reversals (steps above
      ``chatter_min_step``) within ``chatter_window`` frames;
    * uncommanded move: within ``uncommanded_window`` frames state moves at least
      ``uncommanded_travel`` further away from action while action (widened by
      ``uncommanded_lag_margin`` on both sides) moves less than ``uncommanded_action_travel``;
      state catching up to a static action (object slips out) is normal.

    Independent of ``enabled`` (frame flags), ``quick_reversal_discard`` drops the whole
    episode when the gripper reverses within ``quick_reversal_gap`` frames and the smaller of
    the two opposite steps is at least ``quick_reversal_min_step``: a real open/close never
    undoes itself within a few frames (InternData review 2026-09-29). Only steps above
    ``quick_reversal_step_floor`` count as moves.
    """

    enabled: bool = False
    spike_min_step: float = 0.05
    spike_window: int = 3
    spike_return_ratio: float = 0.3
    chatter_min_step: float = 0.02
    chatter_reversals: int = 3
    chatter_window: int = 10
    uncommanded_travel: float = 0.3
    uncommanded_window: int = 15
    uncommanded_action_travel: float = 0.02
    uncommanded_lag_margin: int = 15
    quick_reversal_discard: bool = False
    quick_reversal_gap: int = 3
    quick_reversal_min_step: float = 0.2
    quick_reversal_step_floor: float = 0.02


@dataclass
class EefSpikeConfig:
    """Single-frame EEF position spike: whole-episode discard, state and action checked apart.

    Frame t is a spike when ``|p[t] - p[t-1]|`` and ``|p[t+1] - p[t]|`` are both at least
    ``min_jump`` (meters, per-arm xyz norm) and the pose comes back: ``|p[t+1] - p[t-1]|``
    is below ``return_ratio`` times the smaller jump. A real arm never leaves and returns
    this far in one frame; fast motion and steps do not come back. Not subject to the
    startup grace window (ABC D405 pulse review, 2026-09-30).
    """

    enabled: bool = False
    min_jump: float = 0.2
    return_ratio: float = 0.5


@dataclass
class RotationJointConfig:
    """EEF rotation vs arm joints (humanoid downstream review 2026-09-29).

    A serial chain of revolute joints on a fixed base cannot turn its end effector by more
    than the sum of |joint angle changes| (radians). A step whose EEF rotation (SO(3) distance
    of ``state`` rotation_6d) is at least ``min_rotation_deg`` and exceeds that sum by more
    than ``min_excess_deg`` is a bad source pose (seen near Euler pitch 90 deg). The frame after
    the step is flagged as stage1 abnormal (prefix truncation from there). The stage1 startup
    exclusion does not apply.

    ``arms``: per arm ``(joint state dims, rotation_6d state dims)``.
    """

    enabled: bool = False
    min_rotation_deg: float = 10.0
    min_excess_deg: float = 5.0
    arms: tuple[tuple[tuple[int, ...], tuple[int, ...]], ...] = ()


@dataclass
class Stage1Config:
    median_kernel: int = 5
    savgol_window: int = 11
    savgol_polyorder: int = 3
    k_residual: float = 8.0
    k_accel: float = 8.0
    k_jerk: float = 8.0
    percentile_floor: float = 99.9
    joint_abs_max: float = 3.5
    # Per-joint [lo, hi] (joint_indices order) replacing |q| <= joint_abs_max, e.g. a robot's
    # real limits; ``joint_limit_tolerance`` absorbs float32 rounding at a clamped limit.
    joint_limits: tuple[tuple[float, float], ...] | None = None
    joint_limit_tolerance: float = 0.0
    ee_position_max: float = 2.0
    rpy_abs_max: float = 3.14159265
    gripper_max: float = 1.0
    frame_max_cluster: int = 4
    frame_abnormal_min_cluster: int = 1
    episode_min_cluster: int = 20
    min_cluster_frame_jump: float = 0.08
    on_hard_limit_violation: bool = True
    # Dims left out of the sudden-change check (state / action index).
    exempt_state_dims: tuple[int, ...] = ()
    exempt_action_dims: tuple[int, ...] = ()
    gripper_fault: GripperFaultConfig = field(default_factory=GripperFaultConfig)
    eef_spike: EefSpikeConfig = field(default_factory=EefSpikeConfig)
    rotation_joint: RotationJointConfig = field(default_factory=RotationJointConfig)


def joint_limit_bounds(
    limits: tuple[tuple[float, float], ...] | None,
    n_joints: int,
    tolerance: float,
    fallback: tuple[float, float],
) -> tuple[np.ndarray, np.ndarray]:
    """Per-joint (lo, hi) arrays in ``joint_indices`` order; one shared range when unset."""
    if not limits:
        lo, hi = fallback
        return np.full(n_joints, float(lo)), np.full(n_joints, float(hi))
    arr = np.asarray(limits, dtype=np.float64)
    if arr.shape != (n_joints, 2):
        raise ValueError(f"joint_limits must be [{n_joints}, 2] (one [lo, hi] per joint), got {list(arr.shape)}")
    return arr[:, 0] - float(tolerance), arr[:, 1] + float(tolerance)


@dataclass
class Stage1Result:
    discard: bool
    discard_reasons: list[str]
    abnormal_frames: np.ndarray
    flagged_count: int
    hard_limit_hit: bool
    # Fault kind -> frames flagged by the gripper-fault check.
    gripper_faults: dict[str, int] = field(default_factory=dict)
    # Steps flagged by the rotation/joint check, and the first frame it flagged.
    rotation_joint_steps: int = 0
    rotation_joint_first_frame: int | None = None


def _hard_limit_abnormal_frames(
    state: np.ndarray,
    action: np.ndarray,
    cfg: Stage1Config,
    schema: DatasetSchema,
) -> tuple[np.ndarray, bool, list[str]]:
    num_frames = state.shape[0]
    abnormal = np.zeros(num_frames, dtype=bool)
    reasons: list[str] = []

    if schema.layout == "joint_gripper":
        joints = list(schema.joint_indices)
        action_joints = list(schema.action_joint_indices)
        fallback = (-cfg.joint_abs_max, cfg.joint_abs_max)
        for name, arr, idx in (("state", state, joints), ("action", action, action_joints)):
            if not idx:
                continue
            lo, hi = joint_limit_bounds(cfg.joint_limits, len(idx), cfg.joint_limit_tolerance, fallback)
            out = np.any((arr[:, idx] < lo) | (arr[:, idx] > hi), axis=1)
            abnormal |= out
            if out.any():
                reasons.append(f"hard_limit_{name}_joint")
        if state.shape[1] >= 32:
            from robot_data_processing.schema import (
                QUALITY_EEF_LEFT_XYZ_IDX,
                QUALITY_EEF_RIGHT_XYZ_IDX,
                QUALITY_EEF_ROT6D_ABS_MAX,
                QUALITY_EEF_ROT6D_IDX,
            )

            ee_xyz_idx = QUALITY_EEF_LEFT_XYZ_IDX + QUALITY_EEF_RIGHT_XYZ_IDX
            rot_idx = QUALITY_EEF_ROT6D_IDX
            abnormal |= np.any(np.abs(state[:, ee_xyz_idx]) > cfg.ee_position_max, axis=1)
            if np.any(np.abs(state[:, ee_xyz_idx]) > cfg.ee_position_max):
                reasons.append("hard_limit_state_ee_xyz")
            abnormal |= np.any(np.abs(state[:, rot_idx]) > QUALITY_EEF_ROT6D_ABS_MAX, axis=1)
            if np.any(np.abs(state[:, rot_idx]) > QUALITY_EEF_ROT6D_ABS_MAX):
                reasons.append("hard_limit_state_ee_rot6d")
            if action.shape[1] >= 32:
                abnormal |= np.any(np.abs(action[:, ee_xyz_idx]) > cfg.ee_position_max, axis=1)
                if np.any(np.abs(action[:, ee_xyz_idx]) > cfg.ee_position_max):
                    reasons.append("hard_limit_action_ee_xyz")
                abnormal |= np.any(np.abs(action[:, rot_idx]) > QUALITY_EEF_ROT6D_ABS_MAX, axis=1)
                if np.any(np.abs(action[:, rot_idx]) > QUALITY_EEF_ROT6D_ABS_MAX):
                    reasons.append("hard_limit_action_ee_rot6d")
        elif state.shape[1] > 14 and schema.embodiment == "humanoid":
            ee_xyz_idx = (14, 15, 16, 21, 22, 23)
            abnormal |= np.any(np.abs(state[:, ee_xyz_idx]) > cfg.ee_position_max, axis=1)
            if np.any(np.abs(state[:, ee_xyz_idx]) > cfg.ee_position_max):
                reasons.append("hard_limit_state_ee_xyz")
            quat_idx = list(range(17, 21)) + list(range(24, 28))
            abnormal |= np.any(np.abs(state[:, quat_idx]) > 1.01, axis=1)
            if np.any(np.abs(state[:, quat_idx]) > 1.01):
                reasons.append("hard_limit_state_ee_quat")
            if action.shape[1] > 14:
                abnormal |= np.any(np.abs(action[:, ee_xyz_idx]) > cfg.ee_position_max, axis=1)
                if np.any(np.abs(action[:, ee_xyz_idx]) > cfg.ee_position_max):
                    reasons.append("hard_limit_action_ee_xyz")
                if action.shape[1] >= 28:
                    abnormal |= np.any(np.abs(action[:, quat_idx]) > 1.01, axis=1)
                    if np.any(np.abs(action[:, quat_idx]) > 1.01):
                        reasons.append("hard_limit_action_ee_quat")
    else:
        xyz = list(schema.xyz_indices)
        if xyz:
            abnormal |= np.any(np.abs(state[:, xyz]) > cfg.ee_position_max, axis=1)
            if np.any(np.abs(state[:, xyz]) > cfg.ee_position_max):
                reasons.append("hard_limit_state_xyz")
        rpy = list(schema.rpy_indices)
        if rpy:
            abnormal |= np.any(np.abs(state[:, rpy]) > cfg.rpy_abs_max, axis=1)
            if np.any(np.abs(state[:, rpy]) > cfg.rpy_abs_max):
                reasons.append("hard_limit_state_rpy")

    for gi in schema.gripper_indices:
        abnormal |= np.abs(state[:, gi]) > cfg.gripper_max
    for gi in schema.action_gripper_indices:
        abnormal |= np.abs(action[:, gi]) > cfg.gripper_max
    if any(np.any(np.abs(state[:, gi]) > cfg.gripper_max) for gi in schema.gripper_indices):
        reasons.append("hard_limit_state_gripper")
    if any(np.any(np.abs(action[:, gi]) > cfg.gripper_max) for gi in schema.action_gripper_indices):
        reasons.append("hard_limit_action_gripper")

    return abnormal, bool(reasons), reasons


def _sliding_ptp(x: np.ndarray, window: int) -> np.ndarray:
    """``ptp(x[t - window + 1 : t + 1])`` for every t (shorter at the start)."""
    padded = np.concatenate((np.full(window - 1, x[0]), x))
    view = np.lib.stride_tricks.sliding_window_view(padded, window)
    return view.max(axis=1) - view.min(axis=1)


def _gripper_spike_frames(x: np.ndarray, cfg: GripperFaultConfig) -> np.ndarray:
    n = x.shape[0]
    out = np.zeros(n, dtype=bool)
    w = int(cfg.spike_window)
    if n <= 2 * w:
        return out
    step = np.zeros(n)
    step[1:] = np.diff(x)
    t = np.arange(w, n - w)
    big = np.abs(step[t]) >= cfg.spike_min_step
    undone = np.abs(x[t + w] - x[t - w]) < cfg.spike_return_ratio * np.abs(step[t])
    out[t[big & undone]] = True
    return out


def _gripper_chatter_frames(x: np.ndarray, cfg: GripperFaultConfig) -> np.ndarray:
    out = np.zeros(x.shape[0], dtype=bool)
    d = np.diff(x)
    moves = np.flatnonzero(np.abs(d) > cfg.chatter_min_step)
    if moves.size < 2:
        return out
    sign = np.sign(d[moves])
    rev = moves[1:][sign[1:] != sign[:-1]] + 1  # frame where the reversed step lands
    k = int(cfg.chatter_reversals)
    if k < 1 or rev.size < k:
        return out
    span = rev[k - 1 :] - rev[: rev.size - k + 1]
    out[rev[k - 1 :][span < cfg.chatter_window]] = True
    return out


def _gripper_uncommanded_frames(s: np.ndarray, a: np.ndarray, cfg: GripperFaultConfig) -> np.ndarray:
    n = s.shape[0]
    w, m = int(cfg.uncommanded_window), int(cfg.uncommanded_lag_margin)
    t = np.arange(n)
    # Action window for state frame t: [t - w + 1 - m, t + m].
    a_ptp = _sliding_ptp(a, w + 2 * m)[np.minimum(t + m, n - 1)]
    a_ref = a[np.minimum(t + m, n - 1)]
    s_start = s[np.maximum(t - w + 1, 0)]
    # Moving away from a static command; catching up to it (object slips out) is normal.
    away = np.abs(s - a_ref) - np.abs(s_start - a_ref)
    return (a_ptp < cfg.uncommanded_action_travel) & (away >= cfg.uncommanded_travel)


GRIPPER_QUICK_REVERSAL = "gripper_quick_reversal"
EEF_SPIKE = "eef_position_spike"


def eef_position_spikes(p: np.ndarray, cfg: EefSpikeConfig) -> list[tuple[int, float, float]]:
    """``(frame, jump_in, jump_out)`` for every single-frame spike of a ``(T, 3)`` position."""
    p = np.asarray(p, dtype=np.float64)
    if p.shape[0] < 3:
        return []
    step = np.linalg.norm(np.diff(p, axis=0), axis=1)  # step[i] = |p[i+1] - p[i]|
    back = np.linalg.norm(p[2:] - p[:-2], axis=1)  # |p[t+1] - p[t-1]| for t = 1..T-2
    jin, jout = step[:-1], step[1:]
    hit = (jin >= cfg.min_jump) & (jout >= cfg.min_jump) & (back < cfg.return_ratio * np.minimum(jin, jout))
    return [(int(i) + 1, float(jin[i]), float(jout[i])) for i in np.flatnonzero(hit)]


def eef_spike_reason(
    state: np.ndarray, action: np.ndarray, cfg: EefSpikeConfig, schema: DatasetSchema
) -> str | None:
    """Discard reason naming the largest EEF position spike in state or action, else None."""
    if not cfg.enabled or state.shape[1] < 32:
        return None
    from robot_data_processing.schema import QUALITY_EEF_LEFT_XYZ_IDX, QUALITY_EEF_RIGHT_XYZ_IDX

    del schema
    # Single-arm canvases leave the right slots at 0, which never spike.
    arms = (("left", QUALITY_EEF_LEFT_XYZ_IDX), ("right", QUALITY_EEF_RIGHT_XYZ_IDX))
    worst = None
    for name, arr in (("state", state), ("action", action)):
        if arr.shape[1] < 32:
            continue
        for arm, idx in arms:
            for frame, jin, jout in eef_position_spikes(arr[:, list(idx)], cfg):
                if worst is None or min(jin, jout) > worst[0]:
                    worst = (min(jin, jout), f"{name}_{arm}", frame, jin, jout)
    if worst is None:
        return None
    return f"{EEF_SPIKE}:{worst[1]}:jump={worst[3]:.3f}/{worst[4]:.3f}m@frame{worst[2]}"


def _largest_quick_reversal(x: np.ndarray, cfg: GripperFaultConfig) -> tuple[float, int]:
    """(smaller step of the largest quick reversal, frame where it lands); (0, -1) if none."""
    d = np.diff(x)
    moves = np.flatnonzero(np.abs(d) > cfg.quick_reversal_step_floor)
    if moves.size < 2:
        return 0.0, -1
    a, b = moves[:-1], moves[1:]
    quick = (np.sign(d[a]) != np.sign(d[b])) & (b - a <= int(cfg.quick_reversal_gap))
    if not quick.any():
        return 0.0, -1
    amp = np.where(quick, np.minimum(np.abs(d[a]), np.abs(d[b])), -1.0)
    k = int(np.argmax(amp))
    return float(amp[k]), int(a[k]) + 1


def gripper_quick_reversal_reason(
    state: np.ndarray,
    action: np.ndarray,
    cfg: GripperFaultConfig,
    schema: DatasetSchema,
) -> str | None:
    """Whole-episode discard reason for a gripper open/close undone within a few frames."""
    if not cfg.quick_reversal_discard:
        return None
    worst = (0.0, -1, "")
    channels = [("state", state, gi) for gi in schema.gripper_indices]
    channels += [("action", action, gi) for gi in schema.action_gripper_indices]
    for name, arr, gi in channels:
        amp, frame = _largest_quick_reversal(np.asarray(arr[:, gi], dtype=np.float64), cfg)
        if amp > worst[0]:
            worst = (amp, frame, f"{name}_dim{gi}")
    if worst[0] < cfg.quick_reversal_min_step:
        return None
    return (
        f"{GRIPPER_QUICK_REVERSAL}:{worst[2]}:step={worst[0]:.3f}@frame{worst[1]}"
        f":gap<={int(cfg.quick_reversal_gap)}"
    )


def gripper_fault_frames(
    state: np.ndarray,
    action: np.ndarray,
    cfg: GripperFaultConfig,
    schema: DatasetSchema,
) -> tuple[np.ndarray, dict[str, int]]:
    """Frames flagged by the gripper-fault check, and counts per fault kind."""
    abnormal = np.zeros(state.shape[0], dtype=bool)
    counts: dict[str, int] = {}
    if not cfg.enabled:
        return abnormal, counts

    def _add(kind: str, flags: np.ndarray) -> None:
        nonlocal abnormal
        if flags.any():
            abnormal |= flags
            counts[kind] = counts.get(kind, 0) + int(flags.sum())

    state_g = list(schema.gripper_indices)
    action_g = list(schema.action_gripper_indices)
    for gi in state_g:
        x = np.asarray(state[:, gi], dtype=np.float64)
        _add("state_spike", _gripper_spike_frames(x, cfg))
        _add("state_chatter", _gripper_chatter_frames(x, cfg))
    for gi in action_g:
        x = np.asarray(action[:, gi], dtype=np.float64)
        _add("action_spike", _gripper_spike_frames(x, cfg))
        _add("action_chatter", _gripper_chatter_frames(x, cfg))
    for si, ai in zip(state_g, action_g):
        _add(
            "uncommanded_move",
            _gripper_uncommanded_frames(
                np.asarray(state[:, si], dtype=np.float64), np.asarray(action[:, ai], dtype=np.float64), cfg
            ),
        )
    return abnormal, counts


def rotation_joint_steps(state: np.ndarray, cfg: RotationJointConfig) -> list[dict[str, float]]:
    """Steps ``t -> t + 1`` breaking the rotation/joint bound (see RotationJointConfig)."""
    from robot_data_processing.transforms import rot6d_to_matrix

    out: list[dict[str, float]] = []
    if state.shape[0] < 2:
        return out
    for arm, (joint_dims, rot_dims) in enumerate(cfg.arms):
        R = rot6d_to_matrix(np.asarray(state[:, list(rot_dims)], dtype=np.float64))
        tr = np.einsum("tij,tij->t", R[:-1], R[1:])
        rot = np.degrees(np.arccos(np.clip((tr - 1.0) / 2.0, -1.0, 1.0)))
        dq = np.degrees(np.abs(np.diff(np.asarray(state[:, list(joint_dims)], dtype=np.float64), axis=0)).sum(axis=1))
        for t in np.flatnonzero((rot >= cfg.min_rotation_deg) & (rot - dq > cfg.min_excess_deg)):
            out.append({"arm": arm, "step": int(t), "rotation_deg": float(rot[t]), "joint_sum_deg": float(dq[t])})
    return out


def rotation_joint_frames(state: np.ndarray, cfg: RotationJointConfig) -> tuple[np.ndarray, int]:
    """Frames flagged by the rotation/joint check (the frame after each bad step), step count."""
    abnormal = np.zeros(state.shape[0], dtype=bool)
    if not cfg.enabled:
        return abnormal, 0
    steps = rotation_joint_steps(state, cfg)
    for st in steps:
        abnormal[st["step"] + 1] = True
    return abnormal, len(steps)


def _cluster_filter(flags: np.ndarray, min_len: int) -> np.ndarray:
    if min_len <= 1 or not flags.any():
        return flags
    out = np.zeros_like(flags)
    padded = np.concatenate(([0], flags.astype(np.int8), [0]))
    diff = np.diff(padded)
    starts = np.flatnonzero(diff == 1)
    ends = np.flatnonzero(diff == -1)
    for s, e in zip(starts, ends):
        if e - s >= min_len:
            out[s:e] = True
    return out


def run_stage1(
    state: np.ndarray,
    action: np.ndarray,
    cfg: Stage1Config,
    schema: DatasetSchema = HUMANOID_SCHEMA,
    startup_exclude_per_joint: np.ndarray | None = None,
    global_stats: Stage1GlobalStats | None = None,
) -> Stage1Result:
    num_frames = state.shape[0]
    reasons: list[str] = []

    abnormal, hard_hit, hard_reasons = _hard_limit_abnormal_frames(state, action, cfg, schema)
    if hard_hit:
        reasons.extend(hard_reasons)

    smooth_kwargs = dict(
        median_kernel=cfg.median_kernel,
        savgol_window=cfg.savgol_window,
        savgol_polyorder=cfg.savgol_polyorder,
    )

    discard = hard_hit and cfg.on_hard_limit_violation

    if global_stats is not None:
        state_flags = detect_sudden_changes_2d_with_thresholds(
            state,
            global_stats.thr_residual,
            global_stats.thr_accel,
            global_stats.thr_jerk,
            channel_offset=schema.stage1_state_channel_offset,
            **smooth_kwargs,
        )
        action_flags = detect_sudden_changes_2d_with_thresholds(
            action,
            global_stats.thr_residual,
            global_stats.thr_accel,
            global_stats.thr_jerk,
            channel_offset=schema.stage1_action_channel_offset,
            **smooth_kwargs,
        )
    else:
        per_ep_kwargs = dict(
            k_residual=cfg.k_residual,
            k_accel=cfg.k_accel,
            k_jerk=cfg.k_jerk,
            percentile_floor=cfg.percentile_floor,
            **smooth_kwargs,
        )
        state_flags = detect_sudden_changes_2d(state, **per_ep_kwargs)
        action_flags = detect_sudden_changes_2d(action, **per_ep_kwargs)

    if startup_exclude_per_joint is not None:
        n = min(startup_exclude_per_joint.shape[1], state_flags.shape[1])
        apply_per_dim_exclude(state_flags[:, :n], startup_exclude_per_joint[:, :n])
        n_a = min(startup_exclude_per_joint.shape[1], action_flags.shape[1])
        apply_per_dim_exclude(action_flags[:, :n_a], startup_exclude_per_joint[:, :n_a])

    if cfg.exempt_state_dims:
        state_flags[:, [d for d in cfg.exempt_state_dims if d < state_flags.shape[1]]] = False
    if cfg.exempt_action_dims:
        action_flags[:, [d for d in cfg.exempt_action_dims if d < action_flags.shape[1]]] = False

    all_flags = np.concatenate([state_flags, action_flags], axis=1)

    if cfg.frame_abnormal_min_cluster > 1:
        for d in range(all_flags.shape[1]):
            all_flags[:, d] = _cluster_filter(all_flags[:, d], cfg.frame_abnormal_min_cluster)

    sudden_abnormal = all_flags.any(axis=1)
    abnormal |= sudden_abnormal
    gripper_abnormal, gripper_faults = gripper_fault_frames(state, action, cfg.gripper_fault, schema)
    abnormal |= gripper_abnormal
    rotation_abnormal, rotation_joint_steps_n = rotation_joint_frames(state, cfg.rotation_joint)
    abnormal |= rotation_abnormal
    flagged = int(all_flags.sum())

    state_dim = state.shape[1]
    for d in range(all_flags.shape[1]):
        col_flags = all_flags[:, d]
        if not col_flags.any():
            continue
        max_run = max_contiguous_run(col_flags)
        if max_run >= cfg.episode_min_cluster:
            padded = np.concatenate(([0], col_flags.astype(np.int8), [0]))
            diff = np.diff(padded)
            starts = np.flatnonzero(diff == 1)
            ends = np.flatnonzero(diff == -1)
            signal = state[:, d] if d < state_dim else action[:, d - state_dim]
            for s, e in zip(starts, ends):
                if e - s < cfg.episode_min_cluster:
                    continue
                seg = signal[s:e]
                if seg.size >= 2 and float(np.abs(np.diff(seg)).max()) >= cfg.min_cluster_frame_jump:
                    discard = True
                    reasons.append(f"sudden_change_cluster_dim{d}_len{e - s}")
                    break

    quick = gripper_quick_reversal_reason(state, action, cfg.gripper_fault, schema)
    if quick is not None:
        discard = True
        reasons.append(quick)
    spike = eef_spike_reason(state, action, cfg.eef_spike, schema)
    if spike is not None:
        discard = True
        reasons.append(spike)

    return Stage1Result(
        discard=discard,
        discard_reasons=reasons,
        abnormal_frames=abnormal,
        flagged_count=flagged,
        hard_limit_hit=hard_hit,
        gripper_faults=gripper_faults,
        rotation_joint_steps=rotation_joint_steps_n,
        rotation_joint_first_frame=int(np.flatnonzero(rotation_abnormal)[0]) if rotation_abnormal.any() else None,
    )
