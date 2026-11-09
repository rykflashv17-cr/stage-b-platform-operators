"""P2 ManualGate: require approved review checklist before full export.

``eef_direction`` is first: confirm eef frame / +Z-forward by human review.
The pipeline does not apply Stage5 eef SE(3) transforms by default.

``extrinsic_confidence`` is a separate grade (trusted / untrusted). It does not
change P6 EEF alignment; it only controls whether ``extrinsic.*`` is exported.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

# Order matters: eef direction is reviewed before other slots / before quality stages.
REQUIRED_SLOTS = (
    "eef_direction",
    "gripper_closedness",
    "camera_naming",
    "fps_stable",
    "extrinsic_confidence",
)

# Backward-compatible alias: older checklists used coord_frame for the same gate.
_SLOT_ALIASES = {
    "eef_direction": ("eef_direction", "coord_frame"),
}

_APPROVED_SLOTS = {
    "eef_direction",
    "gripper_closedness",
    "camera_naming",
    "fps_stable",
}

TRUSTED_STATUSES = frozenset({"trusted", "可信"})
UNTRUSTED_STATUSES = frozenset({"untrusted", "不可信"})
EXTRINSIC_CONFIDENCE_RESOLVED = TRUSTED_STATUSES | UNTRUSTED_STATUSES


@dataclass
class GateResult:
    ok: bool
    pending: list[str]
    blocked: list[str]
    message: str


def load_checklist(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def _normalize_status(value: Any) -> str:
    return str(value if value is not None else "pending").strip().lower()


def _slot_entry(items: dict[str, Any], slot: str, *, checklist: dict[str, Any] | None = None) -> dict[str, Any]:
    for key in _SLOT_ALIASES.get(slot, (slot,)):
        if key in items and items[key] is not None:
            entry = items[key]
            return entry if isinstance(entry, dict) else {"status": entry}
    if checklist is not None:
        for key in _SLOT_ALIASES.get(slot, (slot,)):
            if key in checklist and key not in ("items", "dataset", "updated_at", "reviewer"):
                entry = checklist[key]
                return entry if isinstance(entry, dict) else {"status": entry}
    return {}


def evaluate_manual_gate(checklist: dict[str, Any]) -> GateResult:
    items = checklist.get("items") or {}
    pending: list[str] = []
    blocked: list[str] = []
    for slot in REQUIRED_SLOTS:
        entry = _slot_entry(items, slot, checklist=checklist)
        status = _normalize_status(entry.get("status", "pending"))
        if status == "blocked":
            blocked.append(slot)
            continue
        if slot == "extrinsic_confidence":
            if status not in EXTRINSIC_CONFIDENCE_RESOLVED:
                pending.append(slot)
            continue
        if slot in _APPROVED_SLOTS and status != "approved":
            pending.append(slot)
    ok = not pending and not blocked
    parts = []
    if blocked:
        parts.append(f"blocked={blocked}")
    if pending:
        parts.append(f"pending={pending}")
    message = "ManualGate OK" if ok else "ManualGate not cleared: " + ", ".join(parts)
    return GateResult(ok=ok, pending=pending, blocked=blocked, message=message)


def assert_gate_or_raise(
    checklist_path: Path,
    *,
    force_skip: bool = False,
    require_for_export: bool = True,
) -> GateResult:
    checklist = load_checklist(checklist_path)
    result = evaluate_manual_gate(checklist)
    if force_skip or not require_for_export:
        return result
    if not result.ok:
        raise RuntimeError(
            f"{result.message}. Approve items in {checklist_path} "
            f"(start with eef_direction; set extrinsic_confidence to trusted/untrusted) "
            f"or pass --force-skip-gate for experiments."
        )
    return result


def assert_eef_direction_or_raise(
    checklist_path: Path,
    *,
    force_skip: bool = False,
) -> GateResult:
    """Front-loaded check: only ``eef_direction`` must be approved before the run."""
    checklist = load_checklist(checklist_path)
    items = checklist.get("items") or {}
    entry = _slot_entry(items, "eef_direction", checklist=checklist)
    status = _normalize_status(entry.get("status", "pending"))
    if force_skip:
        return GateResult(ok=True, pending=[], blocked=[], message="eef_direction skipped")
    if status == "approved":
        return GateResult(ok=True, pending=[], blocked=[], message="eef_direction approved")
    if status == "blocked":
        raise RuntimeError(f"eef_direction is blocked in {checklist_path}")
    raise RuntimeError(
        f"eef_direction must be approved before pipeline run ({checklist_path}). "
        f"Human-confirm eef frame / +Z-forward; Stage5 eef transforms are off by default."
    )


def extrinsic_confidence_status(checklist: dict[str, Any] | Path) -> str:
    """Return ``trusted``, ``untrusted``, or ``pending``."""
    if isinstance(checklist, Path):
        checklist = load_checklist(checklist)
    items = checklist.get("items") or {}
    entry = _slot_entry(items, "extrinsic_confidence", checklist=checklist)
    status = _normalize_status(entry.get("status", "pending"))
    if status in TRUSTED_STATUSES:
        return "trusted"
    if status in UNTRUSTED_STATUSES:
        return "untrusted"
    return "pending"


def should_export_extrinsics(checklist: dict[str, Any] | Path) -> bool:
    """True only when the human grade is trusted. Pending does not export."""
    return extrinsic_confidence_status(checklist) == "trusted"
