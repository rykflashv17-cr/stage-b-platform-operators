"""Stage2: direction-agreement (DA) on Dataclean **standard** field keys.

Pairs physically corresponding state/action attributes:

* ``observation.state.eef.<role>.position`` / ``rotation_6d`` ↔ ``action.eef.<role>.position`` / ``rotation_6d``
* ``observation.state.arm.<role>.joint_position`` ↔ ``action.arm.<role>.joint_position``

Roles: bimanual → ``left``+``right``; single-arm → ``primary``.

Lag / DA flow per episode:

1. Per dim: if unaligned active samples (on **diffs**) < ``min_active_samples``,
   skip lag for that dim.
2. Else peak-lag via cross-correlation on **smoothed first differences**; if any
   such lag ``< 0`` → discard (``state before action``).
3. Among computed lags: if ``round(mean)`` equals a mode → episode lag = that value;
   else discard (``维度间延迟不匹配``).

Before step 2, an exact shift is tried: if one ``k`` in ``[0, max_lag_frames]`` gives
``action[t] == state[t + k]`` (within ``exact_shift_tolerance``) on every compared dim that
moves, ``k`` is the episode lag and the cross-correlation estimate is skipped. Simulated data
(action = next state) hits this; smooth motion there can make the correlation peak ambiguous.
4. Align **diffs** of all dims with the unified episode lag, then compute per-dim DA.

If ``action.eef.<role>.position`` / ``rotation_6d`` is missing/empty and ``fill_missing_action_eef`` is
enabled (default), fill from state.eef using the **global** P4 ``lag_mean``
(``cfg.fill_lag`` / ``fill_lag`` argument) so downstream export has action.eef::

    action.eef[t] = state.eef[t + lag]   # last ``lag`` steps repeat state.eef[-1]

Filled eef roles are **excluded** from lag / DA stats (synthetic pairs are not
real state↔action agreement).
"""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from typing import Any

import numpy as np
from scipy.signal import correlate

from robot_data_processing.smoothing import smooth_1d

BIMANUAL_ROLES: tuple[str, ...] = ("left", "right")
PRIMARY_ROLES: tuple[str, ...] = ("primary",)

DISCARD_STATE_BEFORE_ACTION = "state before action"
DISCARD_DIM_LAG_MISMATCH = "维度间延迟不匹配"
DISCARD_NO_LAG_DIMS = "no_lag_dims"


@dataclass
class Stage2Config:
    median_kernel: int = 5
    savgol_window: int = 11
    savgol_polyorder: int = 3
    max_lag_frames: int = 5
    diff_epsilon: float = 1e-3
    min_active_samples: int = 10
    da_per_dim: float = 0.65
    da_episode_mean: float = 0.65
    action_type: str = "absolute"
    # If True (default): when action.eef.<role> missing/empty, fill from state.eef
    # using global P4 lag_mean (fill_lag), not per-episode / per-arm joint lag.
    fill_missing_action_eef: bool = True
    fill_lag: int | None = None
    # <= 0 disables the exact-shift check (see module docstring).
    exact_shift_tolerance: float = 1e-6


@dataclass
class Stage2Result:
    discard: bool
    discard_reasons: list[str]
    da_mean: float | None
    da_per_dim: list[float | None]
    lags: list[int | None]  # per-dim peak lag; None if active samples insufficient
    dim_names: list[str] = field(default_factory=list)
    roles: list[str] = field(default_factory=list)
    filled_action_eef: dict[str, bool] = field(default_factory=dict)
    fill_lags: dict[str, int] = field(default_factory=dict)
    episode_lag: int | None = None  # unified lag used for DA (mode when accepted)
    lag_mean: float | None = None  # mean of per-dim lags that were computed
    lag_mode: int | None = None
    lag_source: str = "xcorr"  # "exact_shift" when action[t] == state[t + lag] exactly


def _integrate_delta(action: np.ndarray) -> np.ndarray:
    return np.cumsum(action, axis=0) + action[0]


def _is_missing_field(fields: dict[str, np.ndarray], key: str) -> bool:
    if key not in fields:
        return True
    arr = fields.get(key)
    if arr is None:
        return True
    a = np.asarray(arr)
    return a.size == 0


def detect_arm_roles(fields: dict[str, np.ndarray]) -> list[str]:
    """Bimanual if any left/right arm or eef keys exist; else primary if present."""

    def _role_present(role: str) -> bool:
        for key in (
            f"observation.state.arm.{role}.joint_position",
            f"observation.state.eef.{role}.position",
            f"action.arm.{role}.joint_position",
            f"action.eef.{role}.position",
        ):
            if not _is_missing_field(fields, key):
                return True
        return False

    if any(_role_present(r) for r in BIMANUAL_ROLES):
        return list(BIMANUAL_ROLES)
    if any(_role_present(r) for r in PRIMARY_ROLES):
        return list(PRIMARY_ROLES)
    return list(BIMANUAL_ROLES)


def _smooth_pair(
    state: np.ndarray,
    action: np.ndarray,
    cfg: Stage2Config,
) -> tuple[np.ndarray, np.ndarray] | None:
    """Return aligned smoothed level series ``(s, a)``."""
    s = smooth_1d(state, cfg.median_kernel, cfg.savgol_window, cfg.savgol_polyorder)
    a = smooth_1d(action, cfg.median_kernel, cfg.savgol_window, cfg.savgol_polyorder)
    n = min(s.size, a.size)
    if n <= 0:
        return None
    return np.asarray(s[:n], dtype=np.float64), np.asarray(a[:n], dtype=np.float64)


def _smooth_diffs(
    state: np.ndarray,
    action: np.ndarray,
    cfg: Stage2Config,
) -> tuple[np.ndarray, np.ndarray] | None:
    pair = _smooth_pair(state, action, cfg)
    if pair is None:
        return None
    s, a = pair
    ds = np.diff(s)
    da = np.diff(a)
    n = min(ds.size, da.size)
    if n <= 0:
        return None
    return ds[:n], da[:n]


def _active_count_unaligned(ds: np.ndarray, da: np.ndarray, eps: float) -> int:
    both_still = (np.abs(ds) <= eps) & (np.abs(da) <= eps)
    return int((~both_still).sum())


def _align_diffs(ds: np.ndarray, da: np.ndarray, lag: int) -> tuple[np.ndarray, np.ndarray]:
    n = min(ds.size, da.size)
    lag = int(lag)
    if lag >= 0:
        return ds[lag:n], da[: n - lag]
    return ds[: n + lag], da[-lag:n]


def _peak_lag(x: np.ndarray, y: np.ndarray, max_lag_frames: int) -> int | None:
    """Peak cross-correlation lag of ``x`` vs ``y`` within ``±max_lag_frames``.

    Positive lag ⇒ ``x`` lags ``y`` (state lags action when ``x=state``, ``y=action``).
    """
    n = min(x.size, y.size)
    if n < 2:
        return None
    x = np.asarray(x[:n], dtype=np.float64)
    y = np.asarray(y[:n], dtype=np.float64)
    x_n = (x - x.mean()) / (x.std() + 1e-8)
    y_n = (y - y.mean()) / (y.std() + 1e-8)
    corr = correlate(x_n, y_n, mode="full") / n
    lags = np.arange(-n + 1, n)
    mask = (lags >= -max_lag_frames) & (lags <= max_lag_frames)
    if not np.any(mask):
        return None
    return int(lags[mask][np.argmax(corr[mask])])


def _da_score_with_lag(
    ds: np.ndarray,
    da: np.ndarray,
    lag: int,
    cfg: Stage2Config,
) -> float | None:
    s1, s2 = _align_diffs(ds, da, lag)
    if s1.size == 0:
        return None
    both_still = (np.abs(s1) <= cfg.diff_epsilon) & (np.abs(s2) <= cfg.diff_epsilon)
    active = ~both_still
    if int(active.sum()) < cfg.min_active_samples:
        return None
    return float((np.sign(s1[active]) == np.sign(s2[active])).mean())


def compute_dim_lag(
    state: np.ndarray,
    action: np.ndarray,
    cfg: Stage2Config,
) -> int | None:
    """Peak lag on smoothed first differences; ``None`` if active samples insufficient."""
    diffs = _smooth_diffs(state, action, cfg)
    if diffs is None:
        return None
    ds, da = diffs
    if ds.size < cfg.min_active_samples:
        return None
    if _active_count_unaligned(ds, da, cfg.diff_epsilon) < cfg.min_active_samples:
        return None
    return _peak_lag(ds, da, cfg.max_lag_frames)


def compute_dim_da_with_lag(
    state: np.ndarray,
    action: np.ndarray,
    lag: int,
    cfg: Stage2Config,
) -> float | None:
    """DA on first differences after aligning with ``lag``."""
    diffs = _smooth_diffs(state, action, cfg)
    if diffs is None:
        return None
    ds, da = diffs
    return _da_score_with_lag(ds, da, lag, cfg)


def resolve_episode_lag(lags: list[int]) -> tuple[int | None, float | None, int | None, str | None]:
    """From per-dim lags → (episode_lag, lag_mean, lag_mode, discard_reason_or_None)."""
    if not lags:
        return None, None, None, DISCARD_NO_LAG_DIMS
    if any(int(x) < 0 for x in lags):
        lag_mean = float(np.mean(lags))
        cnt = Counter(int(x) for x in lags)
        mode = int(cnt.most_common(1)[0][0])
        return None, lag_mean, mode, DISCARD_STATE_BEFORE_ACTION

    lag_mean = float(np.mean(lags))
    mean_r = int(round(lag_mean))
    cnt = Counter(int(x) for x in lags)
    max_freq = max(cnt.values())
    modes = {v for v, c in cnt.items() if c == max_freq}
    # Prefer the mode that matches rounded mean when multimodal.
    lag_mode = mean_r if mean_r in modes else int(cnt.most_common(1)[0][0])
    if mean_r not in modes:
        return None, lag_mean, lag_mode, DISCARD_DIM_LAG_MISMATCH
    return mean_r, lag_mean, mean_r, None


def _compute_da_and_lag(
    state: np.ndarray,
    action: np.ndarray,
    cfg: Stage2Config,
) -> tuple[float | None, int]:
    """Legacy helper: peak lag then DA with that lag. Kept for ``estimate_role_joint_lag``."""
    lag = compute_dim_lag(state, action, cfg)
    if lag is None:
        return None, 0
    score = compute_dim_da_with_lag(state, action, lag, cfg)
    return score, int(lag)


def _as_2d(arr: np.ndarray) -> np.ndarray:
    a = np.asarray(arr, dtype=np.float64)
    if a.ndim == 1:
        return a.reshape(-1, 1)
    return a


def shift_state_to_action_timeline(state_eef: np.ndarray, lag: int) -> np.ndarray:
    """Build action.eef from state.eef: action[t] = state[t+lag], pad tail with last value.

    ``lag`` is the measured delay (positive => state lags action / action leads).
    """
    s = _as_2d(state_eef)
    T = s.shape[0]
    out = np.empty_like(s)
    lag = int(lag)
    if lag <= 0:
        shift = -lag
        if shift == 0:
            return s.copy()
        if shift >= T:
            out[:] = s[0]
            return out
        out[shift:] = s[: T - shift]
        out[:shift] = s[0]
        return out
    if lag >= T:
        out[:] = s[-1]
        return out
    out[: T - lag] = s[lag:]
    out[T - lag :] = s[-1]
    return out


def estimate_role_joint_lag(
    fields: dict[str, np.ndarray],
    role: str,
    cfg: Stage2Config,
) -> int:
    """Mean peak-lag over arm joint dims for one role (fallback 0)."""
    s_key = f"observation.state.arm.{role}.joint_position"
    a_key = f"action.arm.{role}.joint_position"
    if _is_missing_field(fields, s_key) or _is_missing_field(fields, a_key):
        return 0
    s = _as_2d(fields[s_key])
    a = _as_2d(fields[a_key])
    if cfg.action_type == "delta":
        a = _integrate_delta(a)
    n_dims = min(s.shape[1], a.shape[1])
    lags: list[int] = []
    for d in range(n_dims):
        lag = compute_dim_lag(s[:, d], a[:, d], cfg)
        if lag is not None:
            lags.append(int(lag))
    if not lags:
        return 0
    return int(round(float(np.mean(lags))))


def fill_missing_action_eef(
    fields: dict[str, np.ndarray],
    cfg: Stage2Config,
    *,
    roles: list[str] | None = None,
    fill_lag: int | None = None,
) -> tuple[dict[str, np.ndarray], dict[str, bool], dict[str, int]]:
    """Fill missing/empty ``action.eef.<role>.position`` / ``rotation_6d`` from state using global lag.

    ``fill_lag`` (argument) or ``cfg.fill_lag`` should be P4 global ``lag_mean``.
    All roles share the same lag. Returns ``(fields, filled_flags, fill_lags)``.
    Mutates ``fields`` in place.
    """
    roles = roles or detect_arm_roles(fields)
    filled: dict[str, bool] = {}
    fill_lags: dict[str, int] = {}
    if not cfg.fill_missing_action_eef:
        for role in roles:
            filled[role] = False
            fill_lags[role] = 0
        return fields, filled, fill_lags

    lag_src = fill_lag if fill_lag is not None else cfg.fill_lag
    lag_fill = int(np.clip(0 if lag_src is None else int(lag_src), 0, cfg.max_lag_frames))

    for role in roles:
        a_pos_key = f"action.eef.{role}.position"
        a_rot_key = f"action.eef.{role}.rotation_6d"
        s_pos_key = f"observation.state.eef.{role}.position"
        s_rot_key = f"observation.state.eef.{role}.rotation_6d"
        if not _is_missing_field(fields, a_pos_key) and not _is_missing_field(fields, a_rot_key):
            filled[role] = False
            fill_lags[role] = 0
            continue
        if _is_missing_field(fields, s_pos_key) or _is_missing_field(fields, s_rot_key):
            filled[role] = False
            fill_lags[role] = 0
            continue
        fields[a_pos_key] = shift_state_to_action_timeline(fields[s_pos_key], lag_fill).astype(
            np.float32, copy=False
        )
        fields[a_rot_key] = shift_state_to_action_timeline(fields[s_rot_key], lag_fill).astype(
            np.float32, copy=False
        )
        fields.pop(f"action.eef.{role}.pose", None)
        filled[role] = True
        fill_lags[role] = lag_fill
    return fields, filled, fill_lags


def _iter_da_pairs(
    fields: dict[str, np.ndarray],
    roles: list[str],
    *,
    skip_eef_roles: set[str] | None = None,
) -> list[tuple[str, np.ndarray, np.ndarray]]:
    """Yield (dim_name, state_1d, action_1d) for corresponding standard attributes.

    ``skip_eef_roles``: roles whose ``action.eef`` was synthetically filled — skip
    those eef pairs for lag/DA (arm joints still included).
    """
    skip_eef = skip_eef_roles or set()
    pairs: list[tuple[str, np.ndarray, np.ndarray]] = []
    for role in roles:
        if role in skip_eef:
            arm_only = True
        else:
            arm_only = False
        for kind, suffix, label in (
            ("arm", "joint_position", "arm"),
        ):
            s_key = f"observation.state.{kind}.{role}.{suffix}"
            a_key = f"action.{kind}.{role}.{suffix}"
            if _is_missing_field(fields, s_key) or _is_missing_field(fields, a_key):
                continue
            s = _as_2d(fields[s_key])
            a = _as_2d(fields[a_key])
            n = min(s.shape[0], a.shape[0])
            s, a = s[:n], a[:n]
            n_dims = min(s.shape[1], a.shape[1])
            for d in range(n_dims):
                pairs.append((f"{label}.{role}.{suffix}[{d}]", s[:, d], a[:, d]))
        if arm_only:
            continue
        for suffix in ("position", "rotation_6d"):
            s_key = f"observation.state.eef.{role}.{suffix}"
            a_key = f"action.eef.{role}.{suffix}"
            if _is_missing_field(fields, s_key) or _is_missing_field(fields, a_key):
                continue
            s = _as_2d(fields[s_key])
            a = _as_2d(fields[a_key])
            n = min(s.shape[0], a.shape[0])
            s, a = s[:n], a[:n]
            n_dims = min(s.shape[1], a.shape[1])
            for d in range(n_dims):
                pairs.append((f"eef.{role}.{suffix}[{d}]", s[:, d], a[:, d]))
    return pairs


def exact_shift_lag(
    prepared: list[tuple[np.ndarray, np.ndarray]],
    cfg: Stage2Config,
) -> int | None:
    """Smallest ``k`` with ``action[t] == state[t + k]`` on every moving dim, else None."""
    tol = float(cfg.exact_shift_tolerance)
    if tol <= 0 or not prepared:
        return None
    pairs = [(np.asarray(s, np.float64), np.asarray(a, np.float64)) for s, a in prepared]
    if not any(np.ptp(s) > cfg.diff_epsilon for s, _ in pairs):
        return None  # nothing moves: every k matches, no evidence
    n = min(s.shape[0] for s, _ in pairs)
    for k in range(0, min(int(cfg.max_lag_frames), n - 2) + 1):
        if all(np.max(np.abs(a[: n - k] - s[k:n])) <= tol for s, a in pairs):
            return k
    return None


def run_stage2_on_fields(
    fields: dict[str, np.ndarray],
    cfg: Stage2Config,
    *,
    fill_lag: int | None = None,
) -> Stage2Result:
    """Lag consensus + unified-lag DA; optional action.eef fill (mutates ``fields``)."""
    roles = detect_arm_roles(fields)
    fields, filled, fill_lags = fill_missing_action_eef(
        fields, cfg, roles=roles, fill_lag=fill_lag
    )
    skip_eef = {r for r, was_filled in filled.items() if was_filled}

    pairs = _iter_da_pairs(fields, roles, skip_eef_roles=skip_eef)
    dim_names: list[str] = []
    prepared: list[tuple[np.ndarray, np.ndarray]] = []
    for name, s1d, a1d in pairs:
        a = a1d
        if cfg.action_type == "delta":
            a = _integrate_delta(a.reshape(-1, 1)).reshape(-1)
        dim_names.append(name)
        prepared.append((s1d, a))

    # Pass 1: exact shift, else per-dim lag when unaligned active samples are sufficient
    lags: list[int | None] = []
    computed_lags: list[int] = []
    exact = exact_shift_lag(prepared, cfg)
    for s1d, a1d in prepared:
        lag = exact if exact is not None else compute_dim_lag(s1d, a1d, cfg)
        lags.append(lag)
        if lag is not None:
            computed_lags.append(int(lag))

    reasons: list[str] = []
    if any(filled.values()):
        reasons.append(
            "filled_action_eef="
            + ",".join(f"{r}:{fill_lags[r]}" for r in roles if filled.get(r))
        )

    episode_lag, lag_mean, lag_mode, lag_reason = resolve_episode_lag(computed_lags)
    if lag_reason is not None:
        reasons.append(lag_reason)
        return Stage2Result(
            discard=True,
            discard_reasons=reasons,
            da_mean=None,
            da_per_dim=[None] * len(dim_names),
            lags=lags,
            dim_names=dim_names,
            roles=roles,
            filled_action_eef=filled,
            fill_lags=fill_lags,
            episode_lag=None,
            lag_mean=lag_mean,
            lag_mode=lag_mode,
        )

    assert episode_lag is not None

    # Pass 2: unified episode lag → DA on all dims
    da_per_dim: list[float | None] = [
        compute_dim_da_with_lag(s1d, a1d, episode_lag, cfg) for s1d, a1d in prepared
    ]
    scored = [v for v in da_per_dim if v is not None]
    da_mean = float(np.mean(scored)) if scored else None

    low_dims = [
        dim_names[i]
        for i, v in enumerate(da_per_dim)
        if v is not None and v < cfg.da_per_dim
    ]
    discard = bool(low_dims)
    if low_dims:
        reasons.append(f"da_low_dims={low_dims}")

    return Stage2Result(
        discard=discard,
        discard_reasons=reasons,
        da_mean=da_mean,
        da_per_dim=da_per_dim,
        lags=lags,
        dim_names=dim_names,
        roles=roles,
        filled_action_eef=filled,
        fill_lags=fill_lags,
        episode_lag=episode_lag,
        lag_mean=lag_mean,
        lag_mode=lag_mode,
        lag_source="exact_shift" if exact is not None else "xcorr",
    )


def run_stage2(
    state_arm: np.ndarray | dict[str, np.ndarray],
    action_arm: np.ndarray | None = None,
    cfg: Stage2Config | None = None,
    *,
    skip_dims: tuple[int, ...] | set[int] | None = None,
    fields: dict[str, np.ndarray] | None = None,
) -> Stage2Result:
    """Stage2 entry.

    Preferred: ``run_stage2(fields=standard_fields, cfg=cfg)`` or
    ``run_stage2(fields_dict, cfg=cfg)``.

    Legacy ndarray ``(state, action)`` path applies the same unified-lag DA policy.
    """
    cfg = cfg or Stage2Config()
    if fields is not None:
        return run_stage2_on_fields(fields, cfg)
    if isinstance(state_arm, dict):
        return run_stage2_on_fields(state_arm, cfg)

    assert action_arm is not None
    state = np.asarray(state_arm, dtype=np.float64)
    action = np.asarray(action_arm, dtype=np.float64)
    if cfg.action_type == "delta":
        action = _integrate_delta(action)
    num_dims = min(state.shape[1], action.shape[1])
    skip = {int(d) for d in (skip_dims or ()) if 0 <= int(d) < num_dims}

    dim_names: list[str] = []
    prepared: list[tuple[np.ndarray, np.ndarray] | None] = []
    for d in range(num_dims):
        dim_names.append(f"legacy_dim[{d}]")
        if d in skip:
            prepared.append(None)
        else:
            prepared.append((state[:, d], action[:, d]))

    lags: list[int | None] = []
    computed_lags: list[int] = []
    for item in prepared:
        if item is None:
            lags.append(None)
            continue
        lag = compute_dim_lag(item[0], item[1], cfg)
        lags.append(lag)
        if lag is not None:
            computed_lags.append(int(lag))

    episode_lag, lag_mean, lag_mode, lag_reason = resolve_episode_lag(computed_lags)
    if lag_reason is not None:
        return Stage2Result(
            discard=True,
            discard_reasons=[lag_reason],
            da_mean=None,
            da_per_dim=[None] * num_dims,
            lags=lags,
            dim_names=dim_names,
            episode_lag=None,
            lag_mean=lag_mean,
            lag_mode=lag_mode,
        )

    assert episode_lag is not None
    da_per_dim: list[float | None] = []
    for item in prepared:
        if item is None:
            da_per_dim.append(None)
        else:
            da_per_dim.append(compute_dim_da_with_lag(item[0], item[1], episode_lag, cfg))
    scored = [v for v in da_per_dim if v is not None]
    da_mean = float(np.mean(scored)) if scored else None
    low_dims = [d for d, v in enumerate(da_per_dim) if v is not None and v < cfg.da_per_dim]
    return Stage2Result(
        discard=bool(low_dims),
        discard_reasons=[f"da_low_dims={low_dims}"] if low_dims else [],
        da_mean=da_mean,
        da_per_dim=da_per_dim,
        lags=lags,
        dim_names=dim_names,
        episode_lag=episode_lag,
        lag_mean=lag_mean,
        lag_mode=lag_mode,
    )


def parse_stage2_fill_config(
    dataset_cfg: dict[str, Any] | None,
    quality_stage2: dict[str, Any] | None = None,
) -> bool:
    """Resolve ``fill_missing_action_eef`` from dataset config (preferred) or quality yaml."""
    ds = dataset_cfg or {}
    s2 = ds.get("stage2") or {}
    if "fill_missing_action_eef" in s2:
        return bool(s2["fill_missing_action_eef"])
    if "fill_missing_action_eef" in ds:
        return bool(ds["fill_missing_action_eef"])
    q = quality_stage2 or {}
    if "fill_missing_action_eef" in q:
        return bool(q["fill_missing_action_eef"])
    return True


def canonical_to_stage2_fields(
    state: np.ndarray,
    action: np.ndarray,
    *,
    embodiment: str,
) -> dict[str, np.ndarray]:
    """Build standard-key fields for Stage2 from quality-space canonical arrays.

    Does **not** invent ``action.eef`` from state (leave missing so fill can run).
    """
    embodiment = str(embodiment).lower()
    state = np.asarray(state, dtype=np.float64)
    action = np.asarray(action, dtype=np.float64)
    fields: dict[str, np.ndarray] = {}

    if embodiment == "franka" and state.shape[1] >= 32:
        # Single-arm 32d: J7 G1 pad | xyz3 rot6d6 | unused right eef.
        n_j = min(7, state.shape[1], action.shape[1])
        fields["observation.state.arm.primary.joint_position"] = state[:, :n_j]
        fields["action.arm.primary.joint_position"] = action[:, :n_j]
        if state.shape[1] > 7:
            fields["observation.state.gripper.primary.closedness"] = state[:, 7:8].astype(np.float32)
        if action.shape[1] > 7:
            fields["action.gripper.primary.closedness"] = action[:, 7:8].astype(np.float32)
        if state.shape[1] >= 17:
            fields["observation.state.eef.primary.position"] = state[:, 14:17].astype(np.float32)
        if action.shape[1] >= 17:
            fields["action.eef.primary.position"] = action[:, 14:17].astype(np.float32)
        if state.shape[1] >= 23:
            fields["observation.state.eef.primary.rotation_6d"] = state[:, 17:23].astype(np.float32)
        if action.shape[1] >= 23:
            fields["action.eef.primary.rotation_6d"] = action[:, 17:23].astype(np.float32)
        return fields

    if state.shape[1] >= 32:
        # Stage B quality 32d: L6 R6 Lg Rg | Lxyz Lrot6d | Rxyz Rrot6d.
        if state.shape[1] >= 12:
            fields["observation.state.arm.left.joint_position"] = state[:, 0:6]
            fields["observation.state.arm.right.joint_position"] = state[:, 6:12]
        if action.shape[1] >= 12:
            fields["action.arm.left.joint_position"] = action[:, 0:6]
            fields["action.arm.right.joint_position"] = action[:, 6:12]
        if state.shape[1] >= 23:
            fields["observation.state.eef.left.position"] = state[:, 14:17].astype(np.float32)
            fields["observation.state.eef.left.rotation_6d"] = state[:, 17:23].astype(np.float32)
        if action.shape[1] >= 23:
            fields["action.eef.left.position"] = action[:, 14:17].astype(np.float32)
            fields["action.eef.left.rotation_6d"] = action[:, 17:23].astype(np.float32)
        if state.shape[1] >= 32:
            fields["observation.state.eef.right.position"] = state[:, 23:26].astype(np.float32)
            fields["observation.state.eef.right.rotation_6d"] = state[:, 26:32].astype(np.float32)
        if action.shape[1] >= 32:
            fields["action.eef.right.position"] = action[:, 23:26].astype(np.float32)
            fields["action.eef.right.rotation_6d"] = action[:, 26:32].astype(np.float32)
        return fields

    if embodiment == "humanoid":
        # state/action 28d: arm12 + grip2 + end14(xyz+quat x2)
        if state.shape[1] >= 12:
            fields["observation.state.arm.left.joint_position"] = state[:, 0:6]
            fields["observation.state.arm.right.joint_position"] = state[:, 6:12]
        if action.shape[1] >= 12:
            fields["action.arm.left.joint_position"] = action[:, 0:6]
            fields["action.arm.right.joint_position"] = action[:, 6:12]
        if state.shape[1] >= 28:
            from robot_data_processing.normalize.transforms_standard import (
                xyz_quat_xyzw_to_position_rotation_6d,
            )

            left_pos, left_rot = xyz_quat_xyzw_to_position_rotation_6d(state[:, 14:21])
            right_pos, right_rot = xyz_quat_xyzw_to_position_rotation_6d(state[:, 21:28])
            fields["observation.state.eef.left.position"] = left_pos
            fields["observation.state.eef.left.rotation_6d"] = left_rot
            fields["observation.state.eef.right.position"] = right_pos
            fields["observation.state.eef.right.rotation_6d"] = right_rot
        if action.shape[1] >= 28:
            from robot_data_processing.normalize.transforms_standard import (
                xyz_quat_xyzw_to_position_rotation_6d,
            )

            a_left_pos, a_left_rot = xyz_quat_xyzw_to_position_rotation_6d(action[:, 14:21])
            a_right_pos, a_right_rot = xyz_quat_xyzw_to_position_rotation_6d(action[:, 21:28])
            fields["action.eef.left.position"] = a_left_pos
            fields["action.eef.left.rotation_6d"] = a_left_rot
            fields["action.eef.right.position"] = a_right_pos
            fields["action.eef.right.rotation_6d"] = a_right_rot
        return fields

    if embodiment in ("lift2", "genie1", "split_aloha"):
        # InternData quality 28d: L6 R6 Lg Rg Lxyz(14:17) Rxyz(21:24); quat slots unused.
        if state.shape[1] >= 12:
            fields["observation.state.arm.left.joint_position"] = state[:, 0:6]
            fields["observation.state.arm.right.joint_position"] = state[:, 6:12]
        if action.shape[1] >= 12:
            fields["action.arm.left.joint_position"] = action[:, 0:6]
            fields["action.arm.right.joint_position"] = action[:, 6:12]
        if state.shape[1] >= 17:
            fields["observation.state.eef.left.position"] = state[:, 14:17].astype(np.float32)
        if action.shape[1] >= 17:
            fields["action.eef.left.position"] = action[:, 14:17].astype(np.float32)
        if state.shape[1] >= 24:
            fields["observation.state.eef.right.position"] = state[:, 21:24].astype(np.float32)
        if action.shape[1] >= 24:
            fields["action.eef.right.position"] = action[:, 21:24].astype(np.float32)
        return fields

    if embodiment in ("egodex", "egostandard", "egoverse"):
        # Quality space is canonical 14d: left/right xyz(3)+rpy(3)+gripper(1).
        # Stage2 DA expects standard keys: eef.*.position / rotation_6d.
        if state.shape[1] >= 14 and action.shape[1] >= 14:
            from scipy.spatial.transform import Rotation

            from robot_data_processing.normalize.transforms_standard import (
                matrices_to_rotation_6d,
            )

            def _rpy_to_rot6d(rpy: np.ndarray) -> np.ndarray:
                mats = Rotation.from_euler("xyz", np.asarray(rpy, dtype=np.float64)).as_matrix()
                return matrices_to_rotation_6d(mats, name="egodex_rpy")

            fields["observation.state.eef.left.position"] = state[:, 0:3].astype(np.float32)
            fields["observation.state.eef.left.rotation_6d"] = _rpy_to_rot6d(state[:, 3:6])
            fields["observation.state.gripper.left.closedness"] = state[:, 6:7].astype(np.float32)
            fields["observation.state.eef.right.position"] = state[:, 7:10].astype(np.float32)
            fields["observation.state.eef.right.rotation_6d"] = _rpy_to_rot6d(state[:, 10:13])
            fields["observation.state.gripper.right.closedness"] = state[:, 13:14].astype(np.float32)
            fields["action.eef.left.position"] = action[:, 0:3].astype(np.float32)
            fields["action.eef.left.rotation_6d"] = _rpy_to_rot6d(action[:, 3:6])
            fields["action.gripper.left.closedness"] = action[:, 6:7].astype(np.float32)
            fields["action.eef.right.position"] = action[:, 7:10].astype(np.float32)
            fields["action.eef.right.rotation_6d"] = _rpy_to_rot6d(action[:, 10:13])
            fields["action.gripper.right.closedness"] = action[:, 13:14].astype(np.float32)
        return fields

    if embodiment in ("robomind_ur", "ur"):
        n = min(state.shape[1], action.shape[1], 6)
        if n > 0:
            fields["observation.state.arm.primary.joint_position"] = state[:, :n]
            fields["action.arm.primary.joint_position"] = action[:, :n]
        return fields

    # abc_130k / yam_bimanual: 14d = L6 + Lg + R6 + Rg
    if embodiment in ("abc_130k", "abc-130k", "yam_bimanual") and state.shape[1] >= 14 and action.shape[1] >= 14:
        fields["observation.state.arm.left.joint_position"] = state[:, 0:6].astype(np.float32)
        fields["observation.state.gripper.left.closedness"] = state[:, 6:7].astype(np.float32)
        fields["observation.state.arm.right.joint_position"] = state[:, 7:13].astype(np.float32)
        fields["observation.state.gripper.right.closedness"] = state[:, 13:14].astype(np.float32)
        fields["action.arm.left.joint_position"] = action[:, 0:6].astype(np.float32)
        fields["action.gripper.left.closedness"] = action[:, 6:7].astype(np.float32)
        fields["action.arm.right.joint_position"] = action[:, 7:13].astype(np.float32)
        fields["action.gripper.right.closedness"] = action[:, 13:14].astype(np.float32)
        return fields

    return fields
