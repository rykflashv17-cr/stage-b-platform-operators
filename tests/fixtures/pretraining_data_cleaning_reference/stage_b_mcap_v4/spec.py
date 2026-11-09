"""Configuration and adapter loading for whole-file MCAP filtering.

Source paths remain lexical absolute paths. In particular, resolving source
symlinks here would hide them from the source-access guard in B04.
"""

from __future__ import annotations

import importlib.util
import math
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .contracts import McapEpisodeRef


@dataclass(frozen=True)
class GripperGapConfig:
    left_topic: str
    right_topic: str
    max_nearest_gap_ns: int


@dataclass(frozen=True)
class McapSpec:
    repo_root: Path
    package_dir: Path
    quality_yaml: Path
    input_root: Path
    output_root: Path
    reference_topics: tuple[str, ...]
    static_topics: tuple[str, ...]
    splits: tuple[str, ...]
    workers: int
    quality_overrides: dict[str, Any] = field(default_factory=dict)
    gripper_gap: GripperGapConfig | None = None


def _absolute(base: Path, value: str | Path) -> Path:
    """Make a path absolute without dereferencing source symlinks."""
    path = Path(value).expanduser()
    # Keep even a symlink followed by ``..`` visible. os.path.abspath would
    # normalize that sequence before the deletion guard can inspect it.
    return path if path.is_absolute() else base / path


def _resolve(base: Path, value: str | Path) -> Path:
    return _absolute(base, value).resolve()


def load_spec(
    config_path: Path, *, input_root: Path | None = None, output_root: Path | None = None,
    repo_root: Path | None = None, package_dir: Path | None = None,
    quality_yaml: Path | None = None,
) -> McapSpec:
    import yaml

    path = Path(config_path).expanduser().resolve()
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    source = raw.get("source") or {}
    output = raw.get("output") or {}
    if source.get("format") != "mcap" or output.get("format") != "report":
        raise ValueError("v4 requires source.format=mcap and output.format=report")
    base = path.parent
    repo = _resolve(Path.cwd(), repo_root) if repo_root is not None else _resolve(base, raw["upstream_repo"])
    package = _resolve(Path.cwd(), package_dir) if package_dir is not None else _resolve(base, raw["dataset_package"])
    quality = _resolve(Path.cwd(), quality_yaml) if quality_yaml is not None else _resolve(base, raw["quality_yaml"])
    root = _absolute(Path.cwd(), input_root) if input_root is not None else _absolute(base, source["root"])
    out = _absolute(Path.cwd(), output_root) if output_root is not None else _absolute(base, output["root"])
    for required in (repo / "qwen-manip-preprocess" / "src", package / "adapter.py", quality):
        if not required.exists():
            raise FileNotFoundError(required)
    if not root.is_dir():
        raise FileNotFoundError(root)
    reference = tuple(source.get("reference_topics") or ())
    if not reference:
        raise ValueError("source.reference_topics must list at least one timestamp topic")
    workers = int(raw.get("workers", 1))
    if workers < 1:
        raise ValueError("workers must be positive")
    gap_raw = source.get("gripper_gap")
    gripper_gap = None
    if gap_raw is not None and gap_raw.get("enabled", True):
        seconds = float(gap_raw["max_nearest_gap_seconds"])
        if not math.isfinite(seconds) or seconds <= 0:
            raise ValueError("source.gripper_gap.max_nearest_gap_seconds must be positive and finite")
        left = str(gap_raw["left_topic"])
        right = str(gap_raw["right_topic"])
        if not left or not right or left == right:
            raise ValueError("source.gripper_gap needs two distinct nonempty topics")
        gripper_gap = GripperGapConfig(left, right, round(seconds * 1e9))
    return McapSpec(
        repo, package, quality, root, out, reference,
        tuple(source.get("static_topics") or ()),
        tuple(source.get("splits") or ("train",)), workers,
        dict(raw.get("quality_overrides") or {}), gripper_gap,
    )


def setup_imports(spec: McapSpec) -> None:
    for path in (spec.repo_root / "qwen-manip-preprocess" / "src", spec.package_dir):
        if str(path) not in sys.path:
            sys.path.insert(0, str(path))


def configure_source_environment(spec: McapSpec) -> None:
    """Keep ABC source scratch and caches beneath the v4 report root."""
    if spec.package_dir.name == "abc_130k":
        scratch = spec.output_root / ".tmp"
        scratch.mkdir(parents=True, exist_ok=True)
        os.environ["ABC130K_PFS_TMP"] = str(scratch)
        os.environ["ABC130K_VIDEO_CACHE"] = str(spec.output_root / "cache" / "source_videos")
        os.environ["ABC130K_SPLITS"] = ",".join(spec.splits)


def _load_module(path: Path, name: str) -> Any:
    module_spec = importlib.util.spec_from_file_location(name, path)
    if module_spec is None or module_spec.loader is None:
        raise ImportError(path)
    module = importlib.util.module_from_spec(module_spec)
    sys.modules[name] = module
    module_spec.loader.exec_module(module)
    return module


def build_adapter(spec: McapSpec) -> Any:
    setup_imports(spec)
    configure_source_environment(spec)
    mod = _load_module(spec.package_dir / "adapter.py", "stage_b_v4_source_adapter")
    if not hasattr(mod, "build_adapter"):
        raise TypeError(f"{spec.package_dir} requires build_adapter(package_dir)")
    return mod.build_adapter(spec.package_dir)


def discover(spec: McapSpec, *, limit: int | None = None) -> list[McapEpisodeRef]:
    """Preserve source paths while using each profile's stable discovery order."""
    setup_imports(spec)
    configure_source_environment(spec)
    custom = spec.package_dir / "discover_mcaps.py"
    if custom.is_file():
        mod = _load_module(custom, "stage_b_v4_source_discover_mcaps")
        paths = list(mod.discover_mcaps(spec.input_root))
        result: list[McapEpisodeRef] = []
        for index, mcap in enumerate(paths[:limit] if limit is not None else paths):
            mcap = _absolute(spec.input_root, mcap)
            rel = mcap.relative_to(spec.input_root)
            result.append(McapEpisodeRef(
                index, spec.input_root, mcap, rel.as_posix(), "", rel.parent.name,
            ))
        return result
    path = spec.package_dir / "discover.py"
    if not path.is_file():
        raise FileNotFoundError(f"MCAP package needs discover.py: {path}")
    mod = _load_module(path, "stage_b_v4_source_discover")
    index_cache = spec.output_root / "cache" / "source_episode_index.json"
    index_cache.parent.mkdir(parents=True, exist_ok=True)
    refs = mod.discover(
        spec.input_root, layout="mcap", splits=spec.splits,
        index_cache=index_cache, rebuild_index=True,
    )
    result: list[McapEpisodeRef] = []
    for ref in refs[:limit] if limit is not None else refs:
        rel = str(ref.rel_path)
        mcap = spec.input_root / rel / "episode.mcap"
        if not mcap.is_file():
            raise FileNotFoundError(mcap)
        result.append(McapEpisodeRef(
            int(ref.episode_index), spec.input_root, mcap, rel,
            str(ref.part or ""), str(ref.task or ""),
        ))
    return result


def quality_config(spec: McapSpec):
    setup_imports(spec)
    import yaml
    from robot_data_processing.pipeline import pipeline_config_from_yaml

    raw = yaml.safe_load(spec.quality_yaml.read_text(encoding="utf-8")) or {}
    raw.setdefault("dataset", {})["root"] = str(spec.input_root)
    overrides = dict(spec.quality_overrides)
    overrides.update({
        "dataset_root": str(spec.input_root),
        "output_dir": str(spec.output_root),
        "num_workers": spec.workers,
        "output_mode": "report",
    })
    return pipeline_config_from_yaml(raw, overrides=overrides)
