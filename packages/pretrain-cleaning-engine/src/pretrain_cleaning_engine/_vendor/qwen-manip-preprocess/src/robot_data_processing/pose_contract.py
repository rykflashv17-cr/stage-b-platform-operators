"""``info.json`` pose / calibration / EEF-reference declarations.

Implements EPISODE_CAMERA_GEOMETRY_PREPROCESSING.md §3.3.3, §5 and §6.2:

* ``pose_geometry`` is declared for every dataset (storage frame E and pose
  encoding), independent of camera calibration.
* ``camera_calibration`` is ``available`` only when real per-frame reference
  extrinsics are exported; ``partial`` when only some evidenced parameters
  (e.g. intrinsics) remain; otherwise ``unavailable`` and ``camera_geometry``
  is dropped.
* ``eef_reference`` is declared per actual role. Dataset configs provide the
  audited values under ``geometry.eef_reference``; roles without a config
  entry are declared with unknown reference point and ``unavailable`` axis
  alignment. ``R_source_canonical`` is never defaulted to identity.
"""
from __future__ import annotations

import copy
from typing import Any

import numpy as np

POSE_GEOMETRY_SCHEMA = "episode_pose_geometry"
TRANSFORM_CONVENTION = "T_A_B maps coordinates from B to A"
EEF_POSITION_FORMAT = "absolute xyz in meters"
EEF_ROTATION_FORMAT = "absolute Rotation 6D, first two matrix rows flattened"
CANONICAL_AXES = "gripper_motion_aligned"
ALIGNMENT_STATUSES = ("verified", "unverified", "unavailable")

# Keys a Stage-B identity pass must carry over from the Stage-A info.json.
CONTRACT_INFO_KEYS = (
    "pose_geometry",
    "camera_calibration",
    "camera_geometry",
    "camera_mapping",
    "camera_roles",
    "camera_view_direction",
    "eef_reference",
    "action_target",
    "intrinsic",
    "optional_features",
    "timestamp_source",
    "quarantine",
    # Stage B passes Stage-A videos through; the plan still describes how they were made.
    "video_resolution",
)


def eef_roles(features: dict[str, Any]) -> list[str]:
    """Roles that actually exported an EEF state or action position."""
    roles: set[str] = set()
    for key in features:
        for prefix in ("observation.state.eef.", "action.eef."):
            if key.startswith(prefix) and key.endswith(".position"):
                roles.add(key[len(prefix) : -len(".position")])
    return sorted(roles)


def validate_canonical_rotation(matrix: Any, *, name: str) -> list[list[float]]:
    """``R_source_canonical`` must be a proper rotation (no reflection)."""
    c = np.asarray(matrix, dtype=np.float64)
    if c.shape != (3, 3):
        raise ValueError(f"{name}: R_source_canonical must be [3,3], got {c.shape}.")
    if not np.allclose(c.T @ c, np.eye(3), atol=1e-6) or not np.isclose(np.linalg.det(c), 1.0):
        raise ValueError(f"{name}: R_source_canonical must satisfy C.T@C=I and det(C)=+1.")
    return c.tolist()


def _unknown_eef_reference(role: str) -> dict[str, Any]:
    return {
        "reference_point": "unknown",
        "axes": "source_convention",
        "alignment_status": "unavailable",
        "reference_posture": "unknown",
        "positive_side": "unknown",
        "negative_side": "unknown",
        "source": {
            "state": f"observation.state.eef.{role}.*",
            "action": f"action.eef.{role}.*",
            "axis_evidence": "none recorded by the adapter",
        },
        "validation": "not performed",
    }


def _normalize_role_reference(role: str, entry: dict[str, Any]) -> dict[str, Any]:
    out = _unknown_eef_reference(role)
    out.update(copy.deepcopy(entry))
    status = out.get("alignment_status")
    if status not in ALIGNMENT_STATUSES:
        raise ValueError(f"eef_reference.{role}.alignment_status must be one of {ALIGNMENT_STATUSES}.")
    if "R_source_canonical" in out:
        if out["R_source_canonical"] is None:
            out.pop("R_source_canonical")
        else:
            out["R_source_canonical"] = validate_canonical_rotation(
                out["R_source_canonical"], name=f"eef_reference.{role}"
            )
    if out.get("axes") == CANONICAL_AXES and status != "verified":
        raise ValueError(
            f"eef_reference.{role}: axes={CANONICAL_AXES} requires alignment_status=verified."
        )
    if status == "verified" and out.get("axes") != CANONICAL_AXES:
        raise ValueError(f"eef_reference.{role}: verified alignment must declare axes={CANONICAL_AXES}.")
    return out


def resolve_eef_reference(
    features: dict[str, Any], configured: dict[str, Any] | None
) -> dict[str, Any]:
    """Per-role declaration; config may use a role name or ``default``."""
    configured = configured or {}
    default = configured.get("default") if isinstance(configured.get("default"), dict) else None
    out: dict[str, Any] = {}
    for role in eef_roles(features):
        entry = configured.get(role)
        if not isinstance(entry, dict):
            entry = default or {}
        out[role] = _normalize_role_reference(role, entry)
    return out


def _has_reference_extrinsic(features: dict[str, Any], geometry: dict[str, Any]) -> bool:
    keys = [str(k) for k in (geometry.get("per_frame_extrinsic_keys") or [])]
    return any(k in features for k in keys)


def finalize_pose_contract(info: dict[str, Any], *, episode_frame: str | None = None) -> None:
    """Add ``pose_geometry``, ``camera_calibration`` and ``eef_reference`` in place."""
    features = info.get("features") if isinstance(info.get("features"), dict) else {}
    camera_geometry = info.get("camera_geometry") if isinstance(info.get("camera_geometry"), dict) else None
    extrinsics_ok = bool(camera_geometry) and _has_reference_extrinsic(features, camera_geometry)

    pose = dict(info.get("pose_geometry") or {})
    frame = (
        pose.get("episode_frame")
        or episode_frame
        or (camera_geometry or {}).get("episode_frame")
    )
    if not frame:
        raise ValueError("pose_geometry.episode_frame is required; declare the storage frame E.")
    if camera_geometry and not extrinsics_ok and frame == (camera_geometry or {}).get("episode_frame"):
        # E was built from extrinsics that are not published; say so explicitly.
        pose.setdefault(
            "episode_frame_note",
            "E was derived from source extrinsics that are not published as trusted camera geometry.",
        )
    pose.update(
        {
            "schema": POSE_GEOMETRY_SCHEMA,
            "episode_frame": str(frame),
            "transform_convention": TRANSFORM_CONVENTION,
            "translation_unit": "meter",
            "eef_position_format": EEF_POSITION_FORMAT,
            "eef_rotation_format": EEF_ROTATION_FORMAT,
        }
    )
    info["pose_geometry"] = pose

    if extrinsics_ok:
        if info.get("camera_calibration") == "unavailable":
            raise ValueError("camera_calibration=unavailable but reference extrinsics are exported.")
        info["camera_calibration"] = info.get("camera_calibration") or "available"
        camera_geometry = dict(camera_geometry)
        if camera_geometry.get("episode_frame") != pose["episode_frame"]:
            raise ValueError("camera_geometry.episode_frame contradicts pose_geometry.episode_frame.")
        info["camera_geometry"] = camera_geometry
    elif camera_geometry and info.get("camera_calibration") in (None, "partial"):
        # Only evidenced parameters (e.g. intrinsics) remain; never label this "available".
        info["camera_calibration"] = "partial"
        camera_geometry = dict(camera_geometry)
        camera_geometry["per_frame_extrinsic_keys"] = []
        camera_geometry["extrinsics"] = "unavailable_or_untrusted"
        info["camera_geometry"] = camera_geometry
    else:
        info["camera_calibration"] = "unavailable"
        info.pop("camera_geometry", None)

    info["eef_reference"] = resolve_eef_reference(features, info.get("eef_reference"))


def contract_extras_from_config(geometry_cfg: dict[str, Any] | None) -> dict[str, Any]:
    """Dataset-level declarations taken from ``config.yaml::geometry``."""
    cfg = geometry_cfg or {}
    out: dict[str, Any] = {}
    if isinstance(cfg.get("eef_reference"), dict):
        out["eef_reference"] = copy.deepcopy(cfg["eef_reference"])
    if isinstance(cfg.get("pose_geometry"), dict):
        out["pose_geometry"] = copy.deepcopy(cfg["pose_geometry"])
    if cfg.get("episode_frame"):
        out.setdefault("pose_geometry", {}).setdefault("episode_frame", str(cfg["episode_frame"]))
    if cfg.get("camera_calibration") in ("available", "partial", "unavailable"):
        out["camera_calibration"] = cfg["camera_calibration"]
    if isinstance(cfg.get("camera_roles"), dict):
        out["camera_roles"] = copy.deepcopy(cfg["camera_roles"])
    if isinstance(cfg.get("action_target"), dict):
        out["action_target"] = copy.deepcopy(cfg["action_target"])
    if cfg.get("timestamp_source"):
        out["timestamp_source"] = copy.deepcopy(cfg["timestamp_source"])
    return out
