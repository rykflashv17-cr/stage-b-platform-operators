from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import numpy as np
from scipy.signal import correlate

from robot_data_processing.schema import DatasetSchema
from robot_data_processing.transforms import robomind_ur_compact_teleop


@dataclass
class StateActionAlignConfig:
    """P4 temporal-align config.

    Modes (see ``resolve_p4_plan``):
      - ``stats`` + ``apply_delay=False`` (default): compute lag stats only; do not
        rewrite action; ``state_action_delay`` = statistical ``lag_mean``.
      - ``stats`` + ``apply_delay=True``: compute lag, shift action to target delay=1;
        ``state_action_delay`` = 1; drop last ``shift`` frames (no padded action tail).
      - ``manual``: skip lag stats; shift action by user convention
        (+k = delay action k steps, -k = advance action k steps);
        ``state_action_delay`` = the given ``manual_delay``.
        Positive row-shift also truncates the episode tail.
    """

    enabled: bool = True
    apply_delay: bool = False
    mode: str = "stats"  # "stats" | "manual"
    manual_delay: int | None = None
    max_lag_frames: int = 5
    diff_epsilon: float = 1e-4
    min_active_samples: int = 10
    fixed_lag: int | None = None
    default_lag: int = 1


@dataclass
class P4Plan:
    """Resolved P4 behavior."""

    name: str  # "stats_only" | "apply_stats" | "manual"
    compute_stats: bool
    apply_shift: bool
    manual_delay: int | None = None


@dataclass
class StateActionAlignStats:
    lag_mean: int
    lag_min: int
    lag_max: int
    per_dim_lag_mean: np.ndarray
    per_dim_lag_min: np.ndarray
    per_dim_lag_max: np.ndarray
    num_episodes: int

    def save(self, path: str) -> None:
        np.savez_compressed(
            path,
            lag_mean=np.array([self.lag_mean]),
            lag_min=np.array([self.lag_min]),
            lag_max=np.array([self.lag_max]),
            per_dim_lag_mean=self.per_dim_lag_mean,
            per_dim_lag_min=self.per_dim_lag_min,
            per_dim_lag_max=self.per_dim_lag_max,
            num_episodes=np.array([self.num_episodes]),
        )

    @classmethod
    def load(cls, path: str) -> StateActionAlignStats:
        data = np.load(path)
        return cls(
            lag_mean=int(data["lag_mean"][0]),
            lag_min=int(data["lag_min"][0]),
            lag_max=int(data["lag_max"][0]),
            per_dim_lag_mean=data["per_dim_lag_mean"],
            per_dim_lag_min=data["per_dim_lag_min"],
            per_dim_lag_max=data["per_dim_lag_max"],
            num_episodes=int(data["num_episodes"][0]),
        )


def resolve_p4_plan(cfg: StateActionAlignConfig) -> P4Plan:
    mode = str(cfg.mode or "stats").strip().lower()
    if mode == "manual":
        if cfg.manual_delay is None:
            raise ValueError(
                "temporal_align.mode=manual requires temporal_align.manual_delay "
                "(+k delays action by k steps; -k advances action by k steps)"
            )
        return P4Plan(
            name="manual",
            compute_stats=False,
            apply_shift=True,
            manual_delay=int(cfg.manual_delay),
        )
    if mode not in ("stats", "statistical", "auto"):
        raise ValueError(f"Unknown temporal_align.mode={cfg.mode!r}; use 'stats' or 'manual'")
    if cfg.apply_delay:
        return P4Plan(name="apply_stats", compute_stats=True, apply_shift=True)
    return P4Plan(name="stats_only", compute_stats=True, apply_shift=False)


def resolve_state_action_delay(
    cfg: StateActionAlignConfig,
    stats: StateActionAlignStats | None,
    plan: P4Plan | None = None,
) -> int:
    """Value written to ``meta.info.state_action_delay``."""
    plan = plan or resolve_p4_plan(cfg)
    if plan.name == "manual":
        return int(plan.manual_delay)  # type: ignore[arg-type]
    if plan.name == "apply_stats":
        return 1
    # stats_only: report measured lag (or default if stats unavailable)
    if stats is not None:
        return int(stats.lag_mean)
    if cfg.fixed_lag is not None:
        return max(0, int(cfg.fixed_lag))
    return int(cfg.default_lag)


def parse_temporal_align_config(
    dataset_ta: dict[str, Any] | None = None,
    quality_sa: dict[str, Any] | None = None,
    *,
    overrides: dict[str, Any] | None = None,
) -> StateActionAlignConfig:
    """Merge dataset ``temporal_align`` over quality ``state_action_alignment``.

    Dataset keys win for ``apply_delay`` / ``mode`` / ``manual_delay``.
    """
    sa = dict(quality_sa or {})
    ta = dict(dataset_ta or {})
    ov = dict(overrides or {})

    def _get(key: str, default: Any = None) -> Any:
        if key in ov and ov[key] is not None:
            return ov[key]
        if key in ta and ta[key] is not None:
            return ta[key]
        if key in sa and sa[key] is not None:
            return sa[key]
        return default

    fixed_lag = _get("fixed_lag", None)
    if fixed_lag is not None:
        fixed_lag = int(fixed_lag)
    manual_delay = _get("manual_delay", None)
    if manual_delay is not None:
        manual_delay = int(manual_delay)

    enabled = _get("enabled", True)
    if "state_action_alignment_enabled" in ov:
        enabled = bool(ov["state_action_alignment_enabled"])

    return StateActionAlignConfig(
        enabled=bool(enabled),
        apply_delay=bool(_get("apply_delay", False)),
        mode=str(_get("mode", "stats")),
        manual_delay=manual_delay,
        max_lag_frames=int(_get("max_lag_frames", 5)),
        diff_epsilon=float(_get("diff_epsilon", 1e-4)),
        min_active_samples=int(_get("min_active_samples", 10)),
        fixed_lag=fixed_lag,
        default_lag=int(_get("default_lag", 1)),
    )


def compute_peak_lag_1d(
    action: np.ndarray,
    state: np.ndarray,
    max_lag_frames: int,
    diff_epsilon: float,
    min_active_samples: int,
) -> int:
    """Cross-correlate Δaction(t) with Δstate(t+lag); positive lag => state lags action."""
    da = np.diff(action)
    ds = np.diff(state)
    n = min(da.size, ds.size)
    if n < min_active_samples + max_lag_frames:
        return 0

    da = da[:n]
    ds = ds[:n]
    da_n = (da - da.mean()) / (da.std() + 1e-8)
    ds_n = (ds - ds.mean()) / (ds.std() + 1e-8)
    corr = correlate(ds_n, da_n, mode="full") / n
    lags = np.arange(-n + 1, n)
    mask = (lags >= 0) & (lags <= max_lag_frames)
    if not mask.any():
        return 0
    return int(lags[mask][np.argmax(corr[mask])])


def compute_episode_lags(
    state: np.ndarray,
    action: np.ndarray,
    cfg: StateActionAlignConfig,
    num_dims: int,
) -> np.ndarray:
    lags = np.zeros(num_dims, dtype=np.int64)
    for d in range(num_dims):
        lags[d] = compute_peak_lag_1d(
            action[:, d],
            state[:, d],
            cfg.max_lag_frames,
            cfg.diff_epsilon,
            cfg.min_active_samples,
        )
    return lags


def temporal_shift_rows(x: np.ndarray, shift: int) -> np.ndarray:
    """Shift rows so out[t] = x[t + shift], with edge padding.

    shift > 0: use future values (advance signal content toward earlier indices).
    shift < 0: use past values (delay the signal).
    """
    out = np.asarray(x, dtype=np.float64).copy()
    if shift == 0 or out.shape[0] == 0:
        return out
    t = out.shape[0]
    if shift > 0:
        if shift >= t:
            out[:] = x[-1]
            return out
        out[: t - shift] = x[shift:]
        out[t - shift :] = x[-1]
    else:
        s = -shift
        if s >= t:
            out[:] = x[0]
            return out
        out[s:] = x[: t - s]
        out[:s] = x[0]
    return out


def apply_manual_action_delay(action: np.ndarray, delay_k: int) -> np.ndarray:
    """Apply user-convention delay: +k delays action by k; -k advances by |k|.

    Implemented as ``temporal_shift_rows(action, -delay_k)``.
    """
    arr = np.asarray(action)
    if arr.ndim == 1:
        return temporal_shift_rows(arr, -int(delay_k)).astype(arr.dtype, copy=False)
    out = arr.copy()
    for d in range(out.shape[1]):
        out[:, d] = temporal_shift_rows(out[:, d], -int(delay_k))
    return out.astype(arr.dtype, copy=False)


def p4_action_row_shift(
    plan: P4Plan,
    *,
    lag: int = 0,
    target_delay: int = 1,
) -> int:
    """Row shift applied to ``action.*``: ``out[t] = action[t + shift]``.

    ``apply_stats``: ``target_delay - lag`` (positive → peek future / drop tail).
    ``manual``: ``-manual_delay`` (``+k`` delays action; ``-k`` advances).
    """
    if not plan.apply_shift:
        return 0
    if plan.name == "manual":
        return -int(plan.manual_delay)  # type: ignore[arg-type]
    return int(target_delay) - int(lag)


def apply_p4_to_action_fields(
    fields: dict[str, np.ndarray],
    plan: P4Plan,
    *,
    lag: int = 0,
    target_delay: int = 1,
) -> dict[str, np.ndarray]:
    """Temporally shift all ``action.*`` fields according to P4 plan.

    Positive shift pads the tail by repeating the last valid action frame
    (§6.3 tail rule); episode length is unchanged.
    """
    row_shift = p4_action_row_shift(plan, lag=lag, target_delay=target_delay)
    if row_shift == 0:
        return fields
    out = dict(fields)
    for name, arr in fields.items():
        if not name.startswith("action."):
            continue
        a = np.asarray(arr)
        if a.ndim == 1:
            out[name] = temporal_shift_rows(a, row_shift).astype(a.dtype, copy=False)
        elif a.ndim >= 2:
            shifted = a.copy()
            # shift along time axis 0 for each trailing column
            flat = shifted.reshape(shifted.shape[0], -1)
            for d in range(flat.shape[1]):
                flat[:, d] = temporal_shift_rows(flat[:, d], row_shift)
            out[name] = flat.reshape(shifted.shape).astype(a.dtype, copy=False)
        else:
            out[name] = a
    return out


def _truncate_time_axis(arr: np.ndarray, orig_t: int, new_t: int) -> np.ndarray:
    a = np.asarray(arr)
    if a.ndim >= 1 and int(a.shape[0]) == int(orig_t):
        return a[:new_t].copy()
    return a


def truncate_standard_episode_tail(standard: Any, n: int) -> Any:
    """Drop the last ``n`` frames from fields / time-varying extrinsics / ``num_frames``.

    Used after a positive action shift (delay): padded repeated action tails are
    removed together with matching state and video length (export keys off
    ``num_frames`` + prefix ``keep_indices``).
    """
    n = int(n)
    if n <= 0:
        return standard
    orig_t = int(standard.num_frames)
    if n >= orig_t:
        raise ValueError(
            f"delay tail truncate n={n} >= num_frames={orig_t}; episode too short"
        )
    new_t = orig_t - n
    standard.fields = {
        k: _truncate_time_axis(v, orig_t, new_t) for k, v in standard.fields.items()
    }
    standard.extrinsic = {
        k: _truncate_time_axis(v, orig_t, new_t) for k, v in standard.extrinsic.items()
    }
    standard.num_frames = new_t
    standard.meta = {
        **standard.meta,
        "delay_tail_truncated_frames": n,
        "num_frames_before_delay_truncate": orig_t,
    }
    return standard


ACTION_TAIL_POLICY = "repeat_last_valid_action"


def apply_p4_delay_to_standard_episode(
    standard: Any,
    plan: P4Plan,
    *,
    lag: int = 0,
    target_delay: int = 1,
) -> Any:
    """Shift ``action.*`` per P4 and keep every observed frame.

    EPISODE_CAMERA_GEOMETRY_PREPROCESSING.md §6.3/§6.4: for delay ``n``
    (``apply_stats`` with ``fixed_lag=0``, ``target_delay=n``) ``action[t]=old[t+n]``
    for ``t < L-n``; the last ``n`` rows have no future target and repeat the last
    valid action (for ``n=1``: ``action[L-1] = action[L-2]``). State, video and
    extrinsics keep length ``L``; nothing is dropped to hide the missing target
    (REPORT EGD-03). Padded rows are recorded in ``meta``.
    """
    shift = p4_action_row_shift(plan, lag=lag, target_delay=target_delay)
    if shift == 0:
        return standard
    num_frames = int(standard.num_frames)
    if shift > 0 and num_frames <= shift:
        raise ValueError(
            f"action delay shift={shift} needs more than {shift} frames; got {num_frames}"
        )
    standard.fields = apply_p4_to_action_fields(
        standard.fields, plan, lag=lag, target_delay=target_delay
    )
    padded = [name for name in standard.fields if name.startswith("action.")]
    standard.meta = {
        **standard.meta,
        "action_row_shift": shift,
        "action_tail_policy": ACTION_TAIL_POLICY if shift > 0 else "repeat_first_action_for_head",
        "action_padded_rows": min(abs(shift), num_frames),
        "action_padded_fields": sorted(padded),
    }
    return standard


def fill_action_from_state(
    state: np.ndarray,
    action: np.ndarray,
    dims: list[int] | np.ndarray,
    *,
    target_delay: int = 1,
) -> np.ndarray:
    """Fill missing action dims from state so action[t] ≈ state[t + target_delay]."""
    aligned = action.copy()
    delay = max(0, int(target_delay))
    t = state.shape[0]
    if t == 0:
        return aligned
    for d in dims:
        d = int(d)
        if d >= aligned.shape[1] or d >= state.shape[1]:
            continue
        if delay <= 0:
            aligned[:, d] = state[:, d]
        elif delay >= t:
            aligned[:, d] = state[-1, d]
        else:
            aligned[: t - delay, d] = state[delay:, d]
            aligned[t - delay :, d] = state[-1, d]
    return aligned


def apply_state_action_temporal_alignment(
    state: np.ndarray,
    action: np.ndarray,
    lag: int,
    num_dims: int | None = None,
    *,
    target_delay: int = 1,
    action_present: np.ndarray | None = None,
) -> np.ndarray:
    """Align action to ``target_delay`` (default 1).

    - Dims with real action (``action_present[d]=True``): temporally *shift* action by
      ``(target_delay - lag)`` so that action[t] lines up with state[t+target_delay].
      Does **not** overwrite action values with state.
    - Dims missing action (``action_present[d]=False``): fill from state at
      ``state[t+target_delay]``.

    ``lag`` is the measured delay (positive => state lags action / action leads).
    ``action_present`` defaults to all-True (shift-only).
    """
    num_dims = num_dims if num_dims is not None else min(state.shape[1], action.shape[1])
    num_dims = min(num_dims, state.shape[1], action.shape[1])
    aligned = action.copy()
    if num_dims <= 0:
        return aligned

    if action_present is None:
        present = np.ones(num_dims, dtype=bool)
    else:
        present = np.asarray(action_present, dtype=bool).reshape(-1)
        if present.size < num_dims:
            pad = np.ones(num_dims - present.size, dtype=bool)
            present = np.concatenate([present, pad])
        present = present[:num_dims]

    missing = [d for d in range(num_dims) if not present[d]]
    if missing:
        aligned = fill_action_from_state(
            state, aligned, missing, target_delay=target_delay
        )

    # Shift existing action: measured lag L, want delay=1 => shift = 1 - L
    # (out[t] = action[t + (1-L)])
    shift = int(target_delay) - int(lag)
    if shift != 0 and present.any():
        for d in range(num_dims):
            if present[d]:
                aligned[:, d] = temporal_shift_rows(aligned[:, d], shift)

    return aligned


def aggregate_lag_stats(per_episode_lags: list[np.ndarray]) -> StateActionAlignStats:
    stacked = np.stack(per_episode_lags, axis=0)
    per_dim_mean = stacked.mean(axis=0)
    return StateActionAlignStats(
        lag_mean=int(round(float(per_dim_mean.mean()))),
        lag_min=int(stacked.min()),
        lag_max=int(stacked.max()),
        per_dim_lag_mean=per_dim_mean,
        per_dim_lag_min=stacked.min(axis=0),
        per_dim_lag_max=stacked.max(axis=0),
        num_episodes=len(per_episode_lags),
    )


def resolve_alignment_lag(
    cfg: StateActionAlignConfig,
    stats: StateActionAlignStats | None,
) -> int:
    if cfg.fixed_lag is not None:
        return max(0, int(cfg.fixed_lag))
    if stats is not None:
        return max(0, stats.lag_mean)
    return max(0, cfg.default_lag)


def alignment_metadata(
    cfg: StateActionAlignConfig,
    stats: StateActionAlignStats | None,
    lag: int,
    *,
    target_delay: int = 1,
    plan: P4Plan | None = None,
) -> dict:
    plan = plan or resolve_p4_plan(cfg)
    meta: dict = {
        "state_action_alignment_applied": bool(plan.apply_shift),
        "state_action_alignment_plan": plan.name,
        "state_action_alignment_mode": cfg.mode,
        "state_action_alignment_apply_delay": bool(cfg.apply_delay),
        "state_action_delay": resolve_state_action_delay(cfg, stats, plan),
        "state_action_alignment_lag": lag,
        "state_action_alignment_target_delay": int(target_delay) if plan.name == "apply_stats" else None,
        "state_action_alignment_manual_delay": plan.manual_delay,
        "state_action_alignment_shift": (
            -int(plan.manual_delay)
            if plan.name == "manual" and plan.manual_delay is not None
            else (int(target_delay) - int(lag) if plan.name == "apply_stats" else 0)
        ),
    }
    if stats is not None:
        meta.update(
            {
                "state_action_alignment_lag_mean": stats.lag_mean,
                "state_action_alignment_lag_min": stats.lag_min,
                "state_action_alignment_lag_max": stats.lag_max,
                "state_action_alignment_per_dim_lag_mean": stats.per_dim_lag_mean.tolist(),
            }
        )
    return meta


def matched_alignment_dims(schema: DatasetSchema, state: np.ndarray, action: np.ndarray) -> int:
    return min(schema.alignment_dim, state.shape[1], action.shape[1])


def alignment_state_action_pair(
    state: np.ndarray,
    action: np.ndarray,
    schema: DatasetSchema,
) -> tuple[np.ndarray, np.ndarray]:
    if schema.embodiment == "robomind_ur":
        return robomind_ur_compact_teleop(state, action)
    num_dims = matched_alignment_dims(schema, state, action)
    return state[:, :num_dims], action[:, :num_dims]
