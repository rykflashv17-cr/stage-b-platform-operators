"""Batch MCAP quality checking and audited whole-file rejection."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import time


from .contracts import McapEpisodeRef
from .reporting import json_safe
from .operators import GlobalStatsOperator, McapReadOperator, QualityDetectionOperator
from .delivery import McapDeliveryOperator
from .spec import discover, load_spec, quality_config


REPORT_NAMES = ("decisions.jsonl", "quality_summary.json", "quality_batch.json", "delivery_plan.json",
                "delivery_manifest.json", "deletion_plan.json", "deletion_events.jsonl",
                "run_summary.json", "statistics_context.json", "accepted")


def _select_refs(args, spec) -> list[McapEpisodeRef]:
    if args.mcap:
        refs = []
        selected = args.mcap[:args.sample_size] if args.sample_size else args.mcap
        for index, raw_path in enumerate(selected):
            path = raw_path.expanduser()
            path = path if path.is_absolute() else Path.cwd() / path
            try:
                rel = Path(os.path.normpath(path.parent)).relative_to(Path(os.path.normpath(spec.input_root)))
            except ValueError as exc:
                raise ValueError(f"--mcap must be below --input-root: {path}") from exc
            refs.append(McapEpisodeRef(
                index, spec.input_root, path, rel.as_posix(),
                rel.parts[1] if len(rel.parts) > 1 else "",
                rel.parts[2] if len(rel.parts) > 2 else "",
            ))
    else:
        refs = discover(spec, limit=args.sample_size)
    seen: set[Path] = set()
    seen_ids: set[int] = set()
    for ref in refs:
        source = ref.mcap_path.expanduser()
        source = source if source.is_absolute() else Path.cwd() / source
        try:
            normalized = Path(os.path.normpath(source))
            normalized.relative_to(Path(os.path.normpath(spec.input_root)))
        except ValueError as exc:
            raise ValueError(f"MCAP must be below --input-root: {source}") from exc
        if source.suffix != ".mcap" or not source.is_file():
            raise ValueError(f"MCAP must point to an existing file: {source}")
        if normalized in seen:
            raise ValueError(f"MCAP selected more than once: {source}")
        if ref.episode_index in seen_ids:
            raise ValueError(f"Episode index appears more than once: {ref.episode_index}")
        seen.add(normalized)
        seen_ids.add(ref.episode_index)
    if not refs:
        raise ValueError(f"No MCAP episodes found under {spec.input_root}")
    return refs


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Stage B v4: reject a whole original MCAP on any quality failure")
    parser.add_argument("command", choices=("stats", "quality", "run"))
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--input-root", type=Path)
    parser.add_argument("--output-root", type=Path)
    parser.add_argument("--repo-root", type=Path)
    parser.add_argument("--package-dir", type=Path)
    parser.add_argument("--quality-yaml", type=Path)
    parser.add_argument("--sample-size", type=int)
    parser.add_argument("--mcap", type=Path, action="append", help="Select source MCAP files directly; repeat for multiple files")
    parser.add_argument("--recompute-stats", action="store_true")
    args = parser.parse_args(argv)
    if args.sample_size is not None and args.sample_size < 1:
        parser.error("--sample-size must be positive")
    started = time.perf_counter()
    spec = load_spec(
        args.config, input_root=args.input_root, output_root=args.output_root,
        repo_root=args.repo_root, package_dir=args.package_dir,
        quality_yaml=args.quality_yaml,
    )
    spec.output_root.mkdir(parents=True, exist_ok=True)
    existing = [spec.output_root / name for name in REPORT_NAMES if (spec.output_root / name).exists() or (spec.output_root / name).is_symlink()]
    if existing or (spec.output_root / "normalized").exists():
        raise FileExistsError("Use a fresh --output-root; normalized data and previous reports will not be overwritten")
    refs = _select_refs(args, spec)
    cfg = quality_config(spec)
    normalized = McapReadOperator().run_batch(refs, spec, cfg)
    context = GlobalStatsOperator().run(normalized, spec, cfg, recompute=args.recompute_stats)
    if args.command == "stats":
        print(json.dumps({"mode": "stats", "episodes": len(refs), "status": context.status,
                          "statistics_error": context.error,
                          "frames": context.statistics.global_stats.num_frames if context.statistics else 0,
                          "normalized_manifest": str(normalized.manifest_path),
                          "elapsed_seconds": time.perf_counter() - started}, ensure_ascii=False))
        return 1 if context.status == "failed" or any(r.input_error for r in normalized.records) else 0
    judged = QualityDetectionOperator().run_batch(context, spec, cfg, started=started)
    result = McapDeliveryOperator().run(judged, mode=args.command)
    print(json.dumps(json_safe(result.summary), ensure_ascii=False, allow_nan=False))
    return result.exit_code


if __name__ == "__main__":
    raise SystemExit(main())
