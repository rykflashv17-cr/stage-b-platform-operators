"""Stage-B frame removal: per-episode record, state-derived action rebuild, info.json declaration.

Also the whole-episode discard record (``meta/episode_discard.json``, ``info.json::episode_discard``).

Quality cleaning drops frames inside an episode and splices the rest (no interpolation):

* prefix truncation — from the first frame flagged by stage1 / stage3, that frame and all
  later ones are dropped;
* stage4 static shortening — inside the kept prefix, each static run keeps its first
  ``max_static_steps`` frames;
* tail repeat trim — trailing rows whose source clock repeats (``extra_info.timestamps``
  dt == 0) are dropped; the row before them is kept.

Frame 0 is never dropped by either rule (a flagged frame 0 leaves an empty prefix and the
episode is discarded), so the episode frame (camera_reference at frame_index=0) is unchanged.

Per-episode details go to ``meta/frame_removal.json``; the policy is declared in
``info.json::frame_removal``.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Iterable

import numpy as np

FRAME_REMOVAL_FILE = "frame_removal.json"
FRAME_REMOVAL_SCHEMA = "frame_removal"
EEF_POSE_PARTS = ("position", "rotation_6d")
# Quality-result metadata copied onto the episode before export.
QUALITY_KEYS = ("prefix_valid_end", "prefix_truncate_trigger", "tail_repeat_start")


def attach_quality_frame_removal(ep: Any, result: Any) -> None:
    """Copy the prefix-truncation facts of a quality result onto ``ep.meta``."""
    meta = getattr(result, "metadata", None) or {}
    info = {k: meta[k] for k in QUALITY_KEYS if meta.get(k) is not None}
    if info:
        ep.meta["quality_frame_removal"] = info


def _stage_a_audit(meta: dict[str, Any]) -> dict[str, Any]:
    return ((meta.get("audit") or {}).get("stage_a") or {}).get("audit") or {}


def action_row_shift(meta: dict[str, Any]) -> int:
    """P4 action row shift of this run plus the one recorded by Stage A."""
    stage_a_tail = _stage_a_audit(meta).get("action_tail") or {}
    return int(meta.get("action_row_shift") or 0) + int(stage_a_tail.get("action_row_shift") or 0)


def state_derived_action_eef_lags(meta: dict[str, Any]) -> dict[str, int]:
    """Roles whose ``action.eef`` is ``state.eef[min(t + lag, L - 1)]``, with their lag.

    Stage A: the stage2 fill of this run. Stage B (identity input): the Stage-A audit
    ``action_eef_fill_tail``.
    """
    stage_a = _stage_a_audit(meta)
    filled = meta.get("stage2_filled_action_eef") or {}
    lags = {
        str(role): int(lag)
        for role, lag in (meta.get("stage2_fill_lags") or {}).items()
        if filled.get(role) and int(lag) > 0
    }
    if lags:
        return lags
    tail = stage_a.get("action_eef_fill_tail") or {}
    return {str(role): int(lag) for role, lag in (tail.get("fill_lags") or {}).items() if int(lag) > 0}


def rebuilt_action_eef(ep: Any, indices: np.ndarray) -> dict[str, np.ndarray]:
    """State-derived ``action.eef`` columns recomputed on the kept rows.

    Only when frames were dropped: ``action[t] = state[min(t + lag, L' - 1)]`` over the
    exported rows, so the action target and its repeated tail hold after splicing.
    """
    kept = np.asarray(indices, dtype=np.int64).reshape(-1)
    if kept.size == int(ep.num_frames) or action_row_shift(ep.meta):
        # A P4 row shift moved the action rows too; that tail is audited as action_tail.
        return {}
    n = int(kept.size)
    out: dict[str, np.ndarray] = {}
    for role, lag in state_derived_action_eef_lags(ep.meta).items():
        rows = np.minimum(np.arange(n) + lag, n - 1)
        for part in EEF_POSE_PARTS:
            state_key = f"observation.state.eef.{role}.{part}"
            action_key = f"action.eef.{role}.{part}"
            if state_key in ep.fields and action_key in ep.fields:
                out[action_key] = np.asarray(ep.fields[state_key], dtype=np.float32)[kept][rows]
    return out


def _runs(frames: np.ndarray) -> list[list[int]]:
    """Inclusive ``[start, end]`` runs of consecutive frame indices."""
    if frames.size == 0:
        return []
    breaks = np.flatnonzero(np.diff(frames) != 1)
    starts = np.concatenate(([frames[0]], frames[breaks + 1]))
    ends = np.concatenate((frames[breaks], [frames[-1]]))
    return [[int(s), int(e)] for s, e in zip(starts, ends)]


def episode_frame_removal_record(ep: Any, keep_indices: np.ndarray) -> dict[str, Any] | None:
    """Frames dropped from one episode (source = input-episode frame indices), or None."""
    num_frames = int(ep.num_frames)
    kept = np.asarray(keep_indices, dtype=np.int64).reshape(-1)
    if kept.size == num_frames or kept.size == 0:
        return None
    quality = ep.meta.get("quality_frame_removal") or {}
    prefix_end = int(quality.get("prefix_valid_end", num_frames))
    tail_start = quality.get("tail_repeat_start")
    tail_start = min(int(tail_start), prefix_end) if tail_start is not None else prefix_end
    removed = np.setdiff1d(np.arange(num_frames, dtype=np.int64), kept)
    static = removed[removed < tail_start]
    tail = removed[(removed >= tail_start) & (removed < prefix_end)]
    gaps = np.flatnonzero(np.diff(kept) > 1)
    return {
        "source_length": num_frames,
        "kept_length": int(kept.size),
        "removed_frames": int(removed.size),
        "prefix_truncate": (
            {
                "from_source_frame": prefix_end,
                "removed_frames": num_frames - prefix_end,
                "trigger": quality.get("prefix_truncate_trigger"),
            }
            if prefix_end < num_frames
            else None
        ),
        "static_shorten": (
            {"removed_frames": int(static.size), "source_ranges": _runs(static)} if static.size else None
        ),
        "tail_repeat_trim": (
            {"from_source_frame": int(tail[0]), "removed_frames": int(tail.size)} if tail.size else None
        ),
        # Exported row followed by a splice, and how many source frames are missing there.
        "splices": [
            {"after_row": int(j), "missing_source_frames": int(kept[j + 1] - kept[j] - 1)} for j in gaps
        ],
    }


def frame_removal_declaration(pcfg: Any) -> dict[str, Any]:
    """``info.json::frame_removal`` for a Stage-B clean export (``pcfg``: quality PipelineConfig)."""
    rules: dict[str, Any] = {
        "prefix_truncate": (
            "the first frame flagged by stage1 (sudden change / hard limit) or stage3 "
            "(extreme value) and every later frame are dropped"
        ),
    }
    margin = int(getattr(pcfg, "prefix_truncate_margin_frames", 0) or 0)
    if margin > 0:
        rules["prefix_truncate"] += (
            f"; so are the {margin} frame(s) before it, whose recorded action targets the "
            "dropped abnormal frame (frame 0 is kept)"
        )
    stage4 = getattr(pcfg, "stage4", None)
    if stage4 is not None and stage4.enabled:
        rules["static_shorten"] = (
            f"inside the kept prefix, each static run (state and action unchanged) keeps its "
            f"first {stage4.resolved_max_static_steps} frames "
            f"({stage4.max_static_duration_sec:g} s at {stage4.fps:g} fps); the rest are dropped"
        )
    if getattr(pcfg, "trim_tail_repeat_frames", False):
        rules["tail_repeat_trim"] = (
            "trailing rows whose source clock repeats (extra_info.timestamps dt == 0) are dropped; "
            "the row before them is kept"
        )
    return {
        "policy": "drop frames and splice the rest; nothing is interpolated",
        "rules": rules,
        "first_frame": "never dropped: frame_index 0 is source frame 0, so the episode frame is unchanged",
        **(
            {"min_episode_length": int(pcfg.min_episode_length)}
            if getattr(pcfg, "discard_short_prefix", True)
            else {}
        ),
        "frame_index": (
            "renumbered 0..L-1 over kept frames; timestamp = frame_index / fps; "
            "extra_info.timestamps (when the source has a clock) keeps it, so each splice shows as a jump there"
        ),
        "action": (
            "state-derived action.eef.* (Stage-A audit action_eef_fill_tail) recomputed on kept rows: "
            "action[t] = state[min(t + lag, L - 1)] (tail repeats); recorded action rows "
            "(joint, gripper, source action.eef) are kept as recorded"
        ),
        "episodes": f"meta/{FRAME_REMOVAL_FILE}",
    }


def write_frame_removal_json(
    meta_dir: Path,
    episodes: Iterable[tuple[int, Any, dict[str, Any] | None]],
    *,
    total_episodes: int,
) -> Path:
    """Write ``meta/frame_removal.json`` from ``(episode_index, source_episode_index, record)``."""
    records = [
        {"episode_index": int(ep_i), "source_episode_index": src, **rec}
        for ep_i, src, rec in episodes
        if rec
    ]
    records.sort(key=lambda r: r["episode_index"])

    def _total(key: str) -> int:
        return int(sum((r.get(key) or {}).get("removed_frames", 0) for r in records))

    doc = {
        "schema": FRAME_REMOVAL_SCHEMA,
        "frame_indices": "source_* = input (Stage A) episode frame index; after_row = exported frame_index",
        "summary": {
            "total_episodes": int(total_episodes),
            "episodes_with_removal": len(records),
            "removed_frames": int(sum(r["removed_frames"] for r in records)),
            "prefix_truncate_frames": _total("prefix_truncate"),
            "static_shorten_frames": _total("static_shorten"),
            "tail_repeat_trim_frames": _total("tail_repeat_trim"),
            "splices": int(sum(len(r["splices"]) for r in records)),
        },
        "episodes": records,
    }
    path = Path(meta_dir) / FRAME_REMOVAL_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(doc, ensure_ascii=False) + "\n", encoding="utf-8")
    return path


def load_frame_removal_records(meta_dir: Path) -> dict[int, dict[str, Any]]:
    """``episode_index -> record`` from a ``meta/frame_removal.json`` (empty if absent)."""
    path = Path(meta_dir) / FRAME_REMOVAL_FILE
    if not path.exists():
        return {}
    doc = json.loads(path.read_text(encoding="utf-8"))
    return {
        int(r["episode_index"]): {k: v for k, v in r.items() if k not in ("episode_index", "source_episode_index")}
        for r in doc.get("episodes") or []
    }


# --- Whole-episode discards (HB-L2) -------------------------------------------------------

EPISODE_DISCARD_FILE = "episode_discard.json"
EPISODE_DISCARD_SCHEMA = "episode_discard"
# P6 discard reason prefixes of dataset episode_geometry modules (InternData IA-R2 / IA-R4).
REFERENCE_CAMERA_MOVING = "reference_camera_moving"
REFERENCE_CAMERA_STEP = "reference_camera_step"
CAMERA_POSE_HOLD_TOO_LONG = "camera_pose_hold_too_long"
TASK_TEXT = "task_text"
# Adapter load failure: source meta points an episode at a data file that lacks it (InternData).
SOURCE_META_MISMATCH = "source_meta_data_file_mismatch"


_REASON_NAME = re.compile(r"[:\s]")


def episode_discard_rule(reasons: list[str]) -> str:
    """Primary rule behind a whole-episode discard, from quality / export reasons."""
    rs = [str(r) for r in reasons if not str(r).startswith("first_removed_frame=")]
    for r in rs:
        if r.startswith(REFERENCE_CAMERA_MOVING):
            return REFERENCE_CAMERA_MOVING
        if r.startswith(REFERENCE_CAMERA_STEP):
            return REFERENCE_CAMERA_STEP
        if r.startswith(CAMERA_POSE_HOLD_TOO_LONG):
            return CAMERA_POSE_HOLD_TOO_LONG
        if r.startswith(TASK_TEXT + ":"):
            return TASK_TEXT
        if SOURCE_META_MISMATCH in r:
            return SOURCE_META_MISMATCH
    for r in rs:
        if r.startswith("gripper_quick_reversal"):
            return "stage1_gripper_quick_reversal"
        if r.startswith("eef_position_spike"):
            return "stage1_eef_position_spike"
    for r in rs:
        if r.startswith("stage2:"):
            return "stage2_trend_alignment"
    for r in rs:
        if r.startswith(("hard_limit_", "sudden_change_cluster")):
            return "stage1_hard_limit" if r.startswith("hard_limit_") else "stage1_sudden_change_cluster"
    for r in rs:
        if r.startswith("stage3:"):
            return "stage3_extreme_value"
    for r in rs:
        if r.startswith("kept_frames="):
            return "too_short_after_frame_removal"
    # Group by the reason name only (``too_short:37<60frames`` -> ``export:too_short``,
    # ``fps 24.00 outside ...`` -> ``export:fps``); the full reason stays in ``reasons``.
    return "export:" + _REASON_NAME.split(rs[0], maxsplit=1)[0] if rs else "export:unknown"


def episode_discard_declaration(pcfg: Any) -> dict[str, Any]:
    """``info.json::episode_discard`` for a Stage-B clean export."""
    rules: dict[str, Any] = {
        "stage2_trend_alignment": "state/action direction agreement (DA) below threshold, or state leads action",
        "stage1_hard_limit": "stage1 hard limit violated",
        "stage1_sudden_change_cluster": "a long run of stage1 sudden-change frames",
        "stage3_extreme_value": "stage3 episode-level discard",
    }
    grip = getattr(getattr(pcfg, "stage1", None), "gripper_fault", None)
    if grip is not None and getattr(grip, "quick_reversal_discard", False):
        rules["stage1_gripper_quick_reversal"] = (
            f"gripper closedness reverses within {int(grip.quick_reversal_gap)} frames with both "
            f"opposite steps >= {float(grip.quick_reversal_min_step):g} (steps above "
            f"{float(grip.quick_reversal_step_floor):g} count as moves)"
        )
    spike = getattr(getattr(pcfg, "stage1", None), "eef_spike", None)
    if spike is not None and getattr(spike, "enabled", False):
        rules["stage1_eef_position_spike"] = (
            f"state or action EEF position (per-arm xyz) jumps >= {float(spike.min_jump):g} m into one "
            f"frame and back on the next (|p[t+1]-p[t-1]| < {float(spike.return_ratio):g} x smaller jump)"
        )
    if getattr(pcfg, "discard_short_prefix", True):
        rules["too_short_after_frame_removal"] = (
            f"fewer than {int(pcfg.min_episode_length)} frames left after frame removal"
        )
    rules["export:*"] = "dropped at export (frame alignment / missing video / processing failure)"
    return {
        "policy": "whole input episodes left out of the output; see rules",
        "rules": rules,
        "episodes": f"meta/{EPISODE_DISCARD_FILE}",
    }


def align_episode_discard_declaration() -> dict[str, Any]:
    """``info.json::episode_discard`` for a Stage-A align export (no quality discard)."""
    return {
        "policy": "whole input episodes left out of the output; see rules",
        "rules": {
            REFERENCE_CAMERA_MOVING: (
                "the reference (head) camera moves beyond "
                "camera_geometry.reference_camera.max_translation_m / max_rotation_deg relative to "
                "frame 0, so the identity T_Episode_CameraReference cannot describe it"
            ),
            REFERENCE_CAMERA_STEP: (
                "the reference (head) camera pose jumps in one frame (tracking relocalization / recording "
                "break): per-frame speed between real pose updates above the configured translation / "
                "rotation floor and many times the surrounding updates (episode_geometry.extrinsics."
                "reference_step; robot_data_processing.reference_camera_step)"
            ),
            CAMERA_POSE_HOLD_TOO_LONG: (
                "the reference (head) camera pose is held (repeats the previous frame) for more than "
                "episode_geometry.extrinsics.max_hold_frames consecutive frames, too long to interpolate "
                "between real pose updates"
            ),
            TASK_TEXT: (
                "no usable instruction: an empty object slot in a multi-object template "
                "(empty_object_slot), or a truncated label that is not the prefix of exactly one "
                "instruction of its source leaf (truncated_no_candidate / ambiguous_truncated)"
            ),
            SOURCE_META_MISMATCH: (
                "source meta/episodes names a data file that holds no rows of the episode, "
                "so the episode cannot be read"
            ),
            "export:*": "dropped at P6 / export (missing extrinsic or video, frame alignment, processing failure)",
        },
        "not_listed": "episodes dropped by the video resolution plan before any processing (info.video_resolution)",
        "episodes": f"meta/{EPISODE_DISCARD_FILE}",
    }


def episode_discard_record(
    source_episode_index: int,
    reasons: list[str],
    *,
    dataset_root: str | None = None,
    rel_path: str | None = None,
) -> dict[str, Any]:
    """``rel_path`` (with its ``dataset_root``) names the source episode when the input
    index is not stable on its own (multi-leaf roots such as InternData)."""
    reasons = [str(r) for r in reasons]
    rec: dict[str, Any] = {
        "source_episode_index": int(source_episode_index),
        "rule": episode_discard_rule(reasons),
        "reasons": reasons,
    }
    if rel_path:
        rec["dataset_root"] = str(dataset_root) if dataset_root is not None else None
        rec["rel_path"] = str(rel_path)
    return rec


def write_episode_discard_json(
    meta_dir: Path,
    records: Iterable[dict[str, Any]],
    *,
    input_episodes: int,
) -> Path:
    """Write ``meta/episode_discard.json`` (``source_episode_index`` = input episode index)."""
    recs = sorted(records, key=lambda r: int(r["source_episode_index"]))
    by_rule: dict[str, int] = {}
    for r in recs:
        by_rule[r["rule"]] = by_rule.get(r["rule"], 0) + 1
    doc = {
        "schema": EPISODE_DISCARD_SCHEMA,
        "source_episode_index": "input (Stage A) episode index",
        "summary": {
            "input_episodes": int(input_episodes),
            "discarded_episodes": len(recs),
            "by_rule": dict(sorted(by_rule.items())),
        },
        "episodes": recs,
    }
    path = Path(meta_dir) / EPISODE_DISCARD_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(doc, ensure_ascii=False) + "\n", encoding="utf-8")
    return path


def load_episode_discard_records(meta_dir: Path) -> list[dict[str, Any]] | None:
    """Records of a ``meta/episode_discard.json``, or None if absent."""
    path = Path(meta_dir) / EPISODE_DISCARD_FILE
    if not path.exists():
        return None
    return list(json.loads(path.read_text(encoding="utf-8")).get("episodes") or [])
