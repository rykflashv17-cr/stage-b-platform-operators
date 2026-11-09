#!/usr/bin/env python3
"""Convert ABC-130k MCAP episodes → LeRobot v2.1 tree (packed 14d + cameras).

Output layout (single_root, compatible with Stage A ``input_format: raw``)::

    <out>/
      meta/info.json
      meta/episodes.jsonl
      meta/tasks.jsonl
      data/chunk-XXX/episode_NNNNNN.parquet
      videos/chunk-XXX/observation.images.{top,left_wrist,right_wrist}/episode_NNNNNN.mp4

Usage::

    python convert.py --output-dir /path/to/lerobot --sample-size 10
    python convert.py --output-dir /path/to/lerobot --splits train --workers 8
"""
from __future__ import annotations

import argparse
import json
import sys
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path
from typing import Any

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq

PACKAGE_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(PACKAGE_DIR))

from mcap_io import (  # noqa: E402
    camera_export_map,
    load_or_build_index,
    load_synced_episode,
    pack_state_action_14d,
    write_annexb_video,
)

DEFAULT_ROOT = Path("/mnt/pfs/datasets/ABC-130k/ABC-130k")


def _write_episode_parquet(
    path: Path,
    *,
    episode_index: int,
    state: np.ndarray,
    action: np.ndarray,
    fps: float,
    task_index: int,
    task_name: str,
    global_index_start: int,
) -> int:
    T = int(state.shape[0])
    frame_index = np.arange(T, dtype=np.int64)
    timestamp = (frame_index / float(fps)).astype(np.float32)
    table = pa.table(
        {
            "observation.state": list(state.astype(np.float32)),
            "action": list(action.astype(np.float32)),
            "timestamp": timestamp,
            "frame_index": frame_index,
            "episode_index": np.full(T, episode_index, dtype=np.int64),
            "index": np.arange(global_index_start, global_index_start + T, dtype=np.int64),
            "task_index": np.full(T, task_index, dtype=np.int64),
            "language_persistent": np.array([task_name] * T, dtype=object),
        }
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(table, path)
    return T


def _convert_one(args: tuple[Any, ...]) -> dict[str, Any]:
    (
        out_root_s,
        ep_index,
        rel_path,
        split,
        task,
        episode_id,
        task_index,
        global_index_start,
        dataset_root_s,
    ) = args
    out_root = Path(out_root_s)
    dataset_root = Path(dataset_root_s)
    ep_dir = dataset_root / rel_path
    synced = load_synced_episode(ep_dir)
    state, action = pack_state_action_14d(synced)
    T = int(state.shape[0])
    fps = float(synced.fps)
    chunk = ep_index // 1000

    parquet_path = out_root / "data" / f"chunk-{chunk:03d}" / f"episode_{ep_index:06d}.parquet"
    _write_episode_parquet(
        parquet_path,
        episode_index=ep_index,
        state=state,
        action=action,
        fps=fps,
        task_index=task_index,
        task_name=synced.task_name,
        global_index_start=global_index_start,
    )

    cam_map = camera_export_map(synced)
    for topic, video_key in cam_map.items():
        frames = synced.video_frames[topic]
        codec = synced.video_codec.get(topic, "h264")
        dst = (
            out_root
            / "videos"
            / f"chunk-{chunk:03d}"
            / video_key
            / f"episode_{ep_index:06d}.mp4"
        )
        write_annexb_video(frames, dst, codec=codec, fps=fps)

    return {
        "episode_index": ep_index,
        "length": T,
        "tasks": [synced.task_name],
        "task_index": task_index,
        "split": split,
        "task_folder": task,
        "episode_id": episode_id,
        "rel_path": rel_path,
        "fps": fps,
        "station": synced.station,
        "reference_camera": synced.reference_camera,
        "global_index_start": global_index_start,
        "global_index_end": global_index_start + T,
    }


def _feature_spec() -> dict[str, Any]:
    joint_names_l = [f"left_joint_{i}_rad" for i in range(1, 7)]
    joint_names_r = [f"right_joint_{i}_rad" for i in range(1, 7)]
    state_names = joint_names_l + ["left_closedness"] + joint_names_r + ["right_closedness"]
    video = {"dtype": "video", "shape": [480, 640, 3], "names": ["height", "width", "channel"]}
    return {
        "observation.state": {
            "dtype": "float32",
            "shape": [14],
            "names": state_names,
        },
        "action": {
            "dtype": "float32",
            "shape": [14],
            "names": state_names,
        },
        "observation.images.top": dict(video),
        "observation.images.left_wrist": dict(video),
        "observation.images.right_wrist": dict(video),
        "timestamp": {"dtype": "float32", "shape": [1]},
        "frame_index": {"dtype": "int64", "shape": [1]},
        "episode_index": {"dtype": "int64", "shape": [1]},
        "index": {"dtype": "int64", "shape": [1]},
        "task_index": {"dtype": "int64", "shape": [1]},
        "language_persistent": {"dtype": "string", "shape": [1]},
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Convert ABC-130k MCAP → LeRobot v2.1")
    parser.add_argument("--dataset-root", type=Path, default=DEFAULT_ROOT)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--splits", nargs="+", default=["train"])
    parser.add_argument("--sample-size", type=int, default=None)
    parser.add_argument("--episode-offset", type=int, default=0, help="Skip first N episodes")
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--rebuild-index", action="store_true")
    parser.add_argument(
        "--index-cache",
        type=Path,
        default=None,
        help="Episode index JSON cache (default: <root>/meta/dataclean_episode_index_*.json)",
    )
    args = parser.parse_args()

    root = args.dataset_root.resolve()
    out = args.output_dir.resolve()
    out.mkdir(parents=True, exist_ok=True)

    infos = load_or_build_index(
        root,
        splits=tuple(args.splits),
        cache_path=args.index_cache,
        rebuild=bool(args.rebuild_index),
    )
    if args.episode_offset:
        infos = infos[int(args.episode_offset) :]
    if args.sample_size is not None:
        infos = infos[: int(args.sample_size)]
    if not infos:
        raise SystemExit("No episodes to convert")

    # Stable task index map
    task_names: list[str] = []
    task_to_index: dict[str, int] = {}
    jobs: list[tuple[Any, ...]] = []
    global_index = 0
    # Pre-pass lengths unknown; assign global_index after convert sequentially in results.
    # For parallel convert, compute placeholder 0 and rewrite indices in a final pass — simpler:
    # run conversion without global index first, then rewrite. For v2.1 Stage A, global index
    # continuity matters less than episode_index. Use running estimate after serial first ep.
    print(f"[convert] episodes={len(infos)} workers={args.workers} out={out}", flush=True)

    # Task indices from folder names (instruction string filled per-ep in parquet)
    for i, info in enumerate(infos):
        folder_task = info.task.replace("_", " ")
        if folder_task not in task_to_index:
            task_to_index[folder_task] = len(task_names)
            task_names.append(folder_task)
        jobs.append(
            (
                str(out),
                i,
                info.rel_path,
                info.split,
                info.task,
                info.episode_id,
                task_to_index[folder_task],
                0,  # filled after we know prior lengths — use two-phase
                str(root),
            )
        )

    # Phase 1: convert videos+parquet with temporary global_index=0; collect lengths
    results: list[dict[str, Any] | None] = [None] * len(jobs)
    if args.workers <= 1:
        for job in jobs:
            r = _convert_one(job)
            results[r["episode_index"]] = r
            if (r["episode_index"] + 1) % 10 == 0 or r["episode_index"] == 0:
                print(f"  [{r['episode_index']+1}/{len(jobs)}] len={r['length']} {r['rel_path']}", flush=True)
    else:
        with ProcessPoolExecutor(max_workers=int(args.workers)) as pool:
            futs = {pool.submit(_convert_one, job): job[1] for job in jobs}
            done = 0
            for fut in as_completed(futs):
                r = fut.result()
                results[r["episode_index"]] = r
                done += 1
                if done % 10 == 0 or done == len(jobs):
                    print(f"  progress {done}/{len(jobs)}", flush=True)

    # Phase 2: rewrite global index columns for contiguous index
    cursor = 0
    episodes_jsonl: list[dict[str, Any]] = []
    for r in results:
        assert r is not None
        ep = int(r["episode_index"])
        chunk = ep // 1000
        path = out / "data" / f"chunk-{chunk:03d}" / f"episode_{ep:06d}.parquet"
        table = pq.read_table(path)
        T = table.num_rows
        index = np.arange(cursor, cursor + T, dtype=np.int64)
        # replace index column
        idx = table.column_names.index("index")
        table = table.set_column(idx, "index", pa.array(index))
        pq.write_table(table, path)
        r["global_index_start"] = cursor
        r["global_index_end"] = cursor + T
        cursor += T
        episodes_jsonl.append(
            {
                "episode_index": ep,
                "tasks": r["tasks"],
                "length": r["length"],
                "source_split": r["split"],
                "source_task": r["task_folder"],
                "source_episode_id": r["episode_id"],
                "source_rel_path": r["rel_path"],
                "station": r["station"],
                "fps": r["fps"],
            }
        )

    meta = out / "meta"
    meta.mkdir(parents=True, exist_ok=True)
    (meta / "episodes.jsonl").write_text(
        "\n".join(json.dumps(e) for e in episodes_jsonl) + "\n", encoding="utf-8"
    )
    tasks_jsonl = [{"task_index": i, "task": name} for i, name in enumerate(task_names)]
    (meta / "tasks.jsonl").write_text(
        "\n".join(json.dumps(t) for t in tasks_jsonl) + "\n", encoding="utf-8"
    )

    total_frames = cursor
    info = {
        "codebase_version": "v2.1",
        "robot_type": "yam_bimanual",
        "embodiment": "abc_130k",
        "total_episodes": len(results),
        "total_frames": total_frames,
        "total_tasks": len(task_names),
        "total_videos": len(results) * 3,
        "total_chunks": (len(results) + 999) // 1000,
        "chunks_size": 1000,
        "fps": 30,
        "splits": {"train": f"0:{len(results)}"},
        "data_path": "data/chunk-{episode_chunk:03d}/episode_{episode_index:06d}.parquet",
        "video_path": "videos/chunk-{episode_chunk:03d}/{video_key}/episode_{episode_index:06d}.mp4",
        "features": _feature_spec(),
        "source": {
            "format": "mcap",
            "root": str(root),
            "splits": list(args.splits),
            "gripper": "closedness = 1 - aperture (source aperture 0=closed,1=open)",
        },
    }
    (meta / "info.json").write_text(json.dumps(info, indent=2), encoding="utf-8")
    print(
        f"[convert] done episodes={len(results)} frames={total_frames} → {out}",
        flush=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
