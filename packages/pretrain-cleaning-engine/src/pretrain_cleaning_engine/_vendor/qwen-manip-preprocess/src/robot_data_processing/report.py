from __future__ import annotations

import json
from collections import Counter
from pathlib import Path
from typing import Any

from robot_data_processing.types import EpisodeResult


def write_exclusion_log(path: Path, results: list[EpisodeResult]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for r in results:
            f.write(json.dumps(r.to_dict(), ensure_ascii=False) + "\n")


def _episode_optimal_lag_row(r: EpisodeResult) -> dict[str, Any]:
    """One episode's Stage2 per-dim lags + unified episode lag used for DA."""
    raw_lags = list(r.stage2_lags) if r.stage2_lags is not None else []
    dim_names = list(r.metadata.get("stage2_dim_names") or [])
    # Only dimensions that actually contributed a lag (active samples enough)
    computed = [int(x) for x in raw_lags if x is not None]
    lag_mean = r.metadata.get("stage2_lag_mean")
    if lag_mean is None and computed:
        lag_mean = float(sum(computed) / len(computed))
    return {
        "episode_index": int(r.episode_index),
        "discard": bool(r.discard),
        "stage2_da_mean": r.stage2_da_mean,
        "dim_names": dim_names,
        "lags": [None if x is None else int(x) for x in raw_lags],
        "lag_mean": float(lag_mean) if lag_mean is not None else None,
        "lag_mode": r.metadata.get("stage2_lag_mode"),
        "episode_lag": r.metadata.get("stage2_episode_lag"),
        "fill_lags": dict(r.metadata.get("stage2_fill_lags") or {}),
        "global_alignment_lag": r.metadata.get("state_action_alignment_lag"),
    }


def build_optimal_lags_payload(results: list[EpisodeResult]) -> dict[str, Any]:
    """Aggregate + per-episode optimal lags from Stage2 lag consensus."""
    rows = [_episode_optimal_lag_row(r) for r in results]
    all_lags = [lag for row in rows for lag in row["lags"] if lag is not None]
    ep_lags = [row["episode_lag"] for row in rows if row.get("episode_lag") is not None]
    ep_means = [row["lag_mean"] for row in rows if row["lag_mean"] is not None]

    dim_acc: dict[str, list[int]] = {}
    for row in rows:
        names = row["dim_names"]
        lags = row["lags"]
        if len(names) != len(lags):
            continue
        for name, lag in zip(names, lags):
            if lag is None:
                continue
            dim_acc.setdefault(str(name), []).append(int(lag))
    per_dim_mean = {
        name: float(sum(vs) / len(vs)) for name, vs in sorted(dim_acc.items()) if vs
    }

    hist = Counter(all_lags)
    ep_hist = Counter(int(x) for x in ep_lags)
    return {
        "summary": {
            "num_episodes": len(rows),
            "num_lag_samples": len(all_lags),
            "lag_mean_over_dims": float(sum(all_lags) / len(all_lags)) if all_lags else None,
            "lag_mean_over_episodes": (
                float(sum(ep_means) / len(ep_means)) if ep_means else None
            ),
            "episode_lag_mean": (
                float(sum(ep_lags) / len(ep_lags)) if ep_lags else None
            ),
            "lag_min": int(min(all_lags)) if all_lags else None,
            "lag_max": int(max(all_lags)) if all_lags else None,
            "lag_histogram": {str(k): int(v) for k, v in sorted(hist.items())},
            "episode_lag_histogram": {str(k): int(v) for k, v in sorted(ep_hist.items())},
            "per_dim_lag_mean": per_dim_mean,
        },
        "episodes": rows,
    }


def write_optimal_lags(path: Path, results: list[EpisodeResult]) -> None:
    """Write Stage2 optimal lags for all processed episodes to one JSON file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = build_optimal_lags_payload(results)
    with path.open("w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, ensure_ascii=False)
        f.write("\n")


def build_quality_report(
    results: list[EpisodeResult],
    stats_meta: dict,
    config_summary: dict,
) -> dict:
    total = len(results)
    discarded = [r for r in results if r.discard]
    kept = [r for r in results if not r.discard]

    reason_counter: Counter = Counter()
    for r in discarded:
        for reason in r.discard_reasons:
            reason_counter[reason.split("=")[0]] += 1

    total_frames = sum(r.num_frames for r in results)
    kept_frames = sum(r.kept_frames for r in results)
    stage1_flagged = sum(r.stage1_flagged_frames for r in results)
    stage3_excluded = sum(r.stage3_excluded_frames for r in results)
    stage4_removed = sum(r.stage4_removed_frames for r in results)

    da_values = [r.stage2_da_mean for r in results if r.stage2_da_mean is not None]
    low_prefix = sum(1 for r in results if r.metadata.get("low_valid_prefix"))

    report = {
        "summary": {
            "total_episodes": total,
            "discarded_episodes": len(discarded),
            "kept_episodes": len(kept),
            "discard_rate": len(discarded) / total if total else 0.0,
            "low_valid_prefix_episodes": low_prefix,
            "total_frames": total_frames,
            "kept_frames": kept_frames,
            "valid_prefix_frame_rate": kept_frames / total_frames if total_frames else 0.0,
            "stage1_flagged_frames": stage1_flagged,
            "stage3_excluded_frames": stage3_excluded,
            "stage4_removed_frames": stage4_removed,
        },
        "stage2_da": {
            "mean": float(sum(da_values) / len(da_values)) if da_values else None,
            "min": float(min(da_values)) if da_values else None,
            "max": float(max(da_values)) if da_values else None,
            "below_0.7": sum(1 for v in da_values if v < 0.7),
        },
        "discard_reasons": dict(reason_counter),
        "stats_meta": stats_meta,
        "config": config_summary,
    }
    return report


def write_quality_report(path: Path, report: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        json.dump(report, f, indent=2, ensure_ascii=False)
