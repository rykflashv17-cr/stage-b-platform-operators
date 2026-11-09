#!/usr/bin/env python3
"""Scan ABC-130k-EEF ``episode.mcap`` camera FPS from MCAP message indexes.

Does **not** decode video. Uses the MCAP summary + per-chunk message indexes
(same estimator as P1: median positive dt → 1/dt).

Usage::

    # dry-run (16 shuffled eps, 8 threads) — sanity check
    python3 scan_mcap_fps.py --dry-run

    # full train (EEF index ~109k)
    python3 scan_mcap_fps.py --workers 32 \\
      --out /mnt/pfs/Data/liuweilin/Dataclean/tmp/abc130k_fps_scan.jsonl
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

import numpy as np
from mcap.data_stream import ReadDataStream
from mcap.opcode import Opcode
from mcap.reader import make_reader
from mcap.records import MessageIndex
from tqdm import tqdm

PACKAGE_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(PACKAGE_DIR))

from mcap_io import (  # noqa: E402
    OPTIONAL_TOP_RIGHT,
    TOP_CAMERA_TOPICS,
    WRIST_CAMERA_TOPICS,
)

DEFAULT_ROOT = Path("/mnt/pfs/datasets/ABC-130k-EEF")
DEFAULT_INDEX = PACKAGE_DIR / ".cache" / "episode_index_train_eef.json"
CAMERA_TOPICS = tuple(dict.fromkeys((*TOP_CAMERA_TOPICS, *WRIST_CAMERA_TOPICS, OPTIONAL_TOP_RIGHT)))
REF_CANDIDATES = TOP_CAMERA_TOPICS
MIN_DT_S = 1e-6
P1_REL_TOL = 0.15
DRY_RUN_N = 16


def _read_message_index(f, offset: int) -> MessageIndex:
    f.seek(offset)
    stream = ReadDataStream(f)
    opcode = stream.read1()
    stream.read8()
    if opcode != Opcode.MESSAGE_INDEX:
        raise RuntimeError(f"expected MESSAGE_INDEX at {offset}, got opcode={opcode}")
    return MessageIndex.read(stream)


def _fps_from_ns(ts_ns: list[int] | np.ndarray) -> dict[str, float | int] | None:
    a = np.asarray(ts_ns, dtype=np.int64)
    if a.size < 3:
        return None
    a.sort()
    dts = np.diff(a) / 1e9
    n_dt = int(dts.size)
    n_tiny = int((dts <= MIN_DT_S).sum())
    dts_pos = dts[dts > MIN_DT_S]
    if dts_pos.size == 0:
        return {
            "n": int(a.size),
            "median_fps": float("nan"),
            "span_fps": float("nan"),
            "dt_ms": float("nan"),
            "tiny_dt_frac": 1.0,
        }
    med = float(np.median(dts_pos))
    span = float((a.size - 1) / ((a[-1] - a[0]) / 1e9)) if a[-1] > a[0] else float("nan")
    return {
        "n": int(a.size),
        "median_fps": 1.0 / med,
        "span_fps": span,
        "dt_ms": med * 1000.0,
        "tiny_dt_frac": n_tiny / max(n_dt, 1),
    }


def _in_band(est: float, expected: float, rel_tol: float = P1_REL_TOL) -> bool:
    if not np.isfinite(est) or expected <= 0:
        return False
    return abs(est - expected) / expected <= rel_tol


def _band(est: float) -> str:
    if not np.isfinite(est):
        return "nan"
    hit30 = _in_band(est, 30.0)
    hit60 = _in_band(est, 60.0)
    if hit30 and hit60:
        return "30" if abs(est - 30.0) <= abs(est - 60.0) else "60"
    if hit60:
        return "60"
    if hit30:
        return "30"
    if est < 20:
        return "low"
    if est > 70:
        return "high"
    return "other"


def scan_one(root_s: str, rel_path: str) -> dict[str, Any]:
    """One episode: camera FPS from message indexes (no video decode)."""
    root = Path(root_s)
    mcap = root / rel_path / "episode.mcap"
    row: dict[str, Any] = {
        "rel_path": rel_path,
        "ok": False,
        "ref": None,
        "ref_fps": None,
        "band": "error",
        "pass30": False,
        "pass60": False,
        "cams": {},
    }
    if not mcap.is_file():
        row["error"] = "missing episode.mcap"
        return row
    try:
        with open(mcap, "rb") as f:
            summary = make_reader(f).get_summary()
            if summary is None or not summary.channels:
                row["error"] = "no mcap summary"
                return row
            topic_of = {cid: ch.topic for cid, ch in summary.channels.items()}
            cam_ids = {cid: t for cid, t in topic_of.items() if t in CAMERA_TOPICS}
            buckets: dict[str, list[int]] = {t: [] for t in cam_ids.values()}
            for chunk in summary.chunk_indexes:
                for cid, off in chunk.message_index_offsets.items():
                    if cid not in cam_ids:
                        continue
                    rec = _read_message_index(f, off)
                    topic = cam_ids[cid]
                    buckets[topic].extend(t for t, _ in rec.records)
        cams: dict[str, dict[str, float | int]] = {}
        for topic, vals in buckets.items():
            stat = _fps_from_ns(vals)
            if stat is not None:
                cams[topic] = stat
        row["cams"] = cams
        ref = next((t for t in REF_CANDIDATES if t in cams), None)
        if ref is None:
            row["error"] = f"no reference camera in {sorted(cams)}"
            return row
        est = float(cams[ref]["median_fps"])
        row["ok"] = True
        row["ref"] = ref
        row["ref_fps"] = est
        row["band"] = _band(est)
        row["pass30"] = _in_band(est, 30.0)
        row["pass60"] = _in_band(est, 60.0)
        row["tiny_dt_frac"] = float(cams[ref].get("tiny_dt_frac") or 0.0)
        row["n_frames"] = int(cams[ref]["n"])
        return row
    except Exception as exc:  # noqa: BLE001 — keep scan going
        row["error"] = f"{type(exc).__name__}: {exc}"
        return row


def _load_index(index_path: Path, root: Path) -> list[dict[str, str]]:
    if not index_path.is_file():
        raise SystemExit(
            f"missing index {index_path}. Build it via mcap_io.load_or_build_index "
            f"(root={root})."
        )
    rows = json.loads(index_path.read_text(encoding="utf-8"))
    if not rows:
        raise SystemExit(f"empty index {index_path}")
    return rows


def _already_done(jsonl_path: Path) -> set[str]:
    done: set[str] = set()
    if not jsonl_path.is_file():
        return done
    with jsonl_path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            rel = rec.get("rel_path")
            if rel:
                done.add(str(rel))
    return done


def _summarize(rows: list[dict[str, Any]]) -> dict[str, Any]:
    ok = [r for r in rows if r.get("ok")]
    fps = np.array([float(r["ref_fps"]) for r in ok], dtype=np.float64)
    bands = Counter(str(r.get("band")) for r in rows)
    refs = Counter(str(r.get("ref") or "none") for r in ok)
    out: dict[str, Any] = {
        "n_total": len(rows),
        "n_ok": len(ok),
        "n_error": len(rows) - len(ok),
        "n_pass30": int(sum(bool(r.get("pass30")) for r in ok)),
        "n_pass60": int(sum(bool(r.get("pass60")) for r in ok)),
        "bands": dict(bands),
        "ref_topics": dict(refs),
    }
    if fps.size:
        out["ref_fps"] = {
            "min": float(np.nanmin(fps)),
            "p05": float(np.nanpercentile(fps, 5)),
            "median": float(np.nanmedian(fps)),
            "p95": float(np.nanpercentile(fps, 95)),
            "max": float(np.nanmax(fps)),
        }
    return out


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--root", type=Path, default=DEFAULT_ROOT, help="ABC-130k-EEF root")
    p.add_argument("--index", type=Path, default=DEFAULT_INDEX, help="episode index JSON")
    p.add_argument("--out", type=Path, default=None, help="jsonl output (one row per episode)")
    p.add_argument("--summary", type=Path, default=None, help="summary JSON (default: <out>.summary.json)")
    p.add_argument("--workers", type=int, default=16)
    p.add_argument("--limit", type=int, default=None, help="max episodes (after shuffle if set)")
    p.add_argument("--shuffle", action="store_true", help="uniform shuffle before --limit")
    p.add_argument("--seed", type=int, default=20260922)
    p.add_argument("--resume", action="store_true", help="skip rel_paths already in --out")
    p.add_argument(
        "--dry-run",
        action="store_true",
        help=f"shuffle + first {DRY_RUN_N} episodes; write under package .cache/",
    )
    return p.parse_args()


def main() -> int:
    args = parse_args()
    root = args.root.expanduser().resolve()
    index_path = args.index.expanduser().resolve()
    rows = _load_index(index_path, root)
    universe = len(rows)

    if args.dry_run:
        args.shuffle = True
        if args.limit is None:
            args.limit = DRY_RUN_N
        if args.out is None:
            args.out = PACKAGE_DIR / ".cache" / "fps_scan_dryrun.jsonl"

    if args.shuffle:
        rng = np.random.default_rng(int(args.seed))
        order = rng.permutation(len(rows))
        rows = [rows[int(i)] for i in order]
    if args.limit is not None:
        rows = rows[: max(int(args.limit), 0)]

    if args.out is None:
        args.out = Path("abc130k_fps_scan.jsonl").resolve()
    else:
        args.out = args.out.expanduser().resolve()
    args.out.parent.mkdir(parents=True, exist_ok=True)
    if args.summary is None:
        args.summary = args.out.with_suffix(args.out.suffix + ".summary.json")
    else:
        args.summary = args.summary.expanduser().resolve()

    done: set[str] = _already_done(args.out) if args.resume else set()
    jobs = [(str(root), r["rel_path"]) for r in rows if r.get("rel_path") not in done]
    mode = "dry-run" if args.dry_run else "full"
    print(
        f"[fps-scan] mode={mode} universe={universe} queued={len(jobs)} "
        f"skip_resume={len(done)} workers={args.workers} root={root}",
        flush=True,
    )
    print(f"[fps-scan] out={args.out}", flush=True)

    collected: list[dict[str, Any]] = []
    workers = max(1, int(args.workers))
    write_mode = "a" if args.resume and args.out.exists() else "w"
    with args.out.open(write_mode, encoding="utf-8") as fout, ThreadPoolExecutor(
        max_workers=workers
    ) as pool:
        futs = {pool.submit(scan_one, root_s, rel): rel for root_s, rel in jobs}
        for fut in tqdm(
            as_completed(futs),
            total=len(futs),
            desc="scan mcap fps",
            unit="ep",
            smoothing=0.05,
        ):
            rec = fut.result()
            collected.append(rec)
            fout.write(json.dumps(rec, ensure_ascii=False) + "\n")
            if len(collected) % 2000 == 0:
                fout.flush()

    # If resume, re-read jsonl so summary covers the whole file.
    if args.resume and done:
        all_rows: list[dict[str, Any]] = []
        with args.out.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    all_rows.append(json.loads(line))
    else:
        all_rows = collected

    summary = _summarize(all_rows)
    summary.update(
        {
            "mode": mode,
            "root": str(root),
            "index": str(index_path),
            "universe": universe,
            "out": str(args.out),
            "workers": workers,
            "seed": int(args.seed) if args.shuffle else None,
            "p1_rel_tol": P1_REL_TOL,
            "estimator": "median_positive_dt_from_mcap_message_index",
        }
    )
    args.summary.write_text(json.dumps(summary, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(json.dumps(summary, indent=2, ensure_ascii=False), flush=True)
    print(f"[fps-scan] summary → {args.summary}", flush=True)
    if args.dry_run:
        print(
            "[fps-scan] full run:\n"
            f"  python3 {Path(__file__).name} --workers {max(workers, 32)} "
            f"--out /mnt/pfs/Data/liuweilin/Dataclean/tmp/abc130k_fps_scan.jsonl --resume",
            flush=True,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
