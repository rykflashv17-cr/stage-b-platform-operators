"""Optional per-dataset adapter I/O hooks (quality load + source video resolve).

Packages may implement on their adapter:

- ``load_quality_arrays(root, episode_index, *, schema=None, **kwargs) -> dict``
- or ``load_raw(ref)`` + ``to_quality_arrays(standard)`` (+ optional ``episode_ref``)
- ``resolve_source_video(root, episode_index, source_video_key) -> Path | None``

Call ``install_adapter_hooks(package_dir)`` once in the parent process before
stats/export ProcessPools (fork inherits the registry).
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any

import numpy as np

_PACKAGE_DIR: Path | None = None
_ADAPTER: Any = None


def _load_module(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"Cannot load module from {path}")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def clear_adapter_hooks() -> None:
    global _PACKAGE_DIR, _ADAPTER
    _PACKAGE_DIR = None
    _ADAPTER = None


def install_adapter_hooks(package_dir: Path | str | None) -> Any:
    """Load ``adapter.py`` from a dataset package and register it for I/O hooks."""
    clear_adapter_hooks()
    if package_dir is None:
        return None
    package_dir = Path(package_dir)
    adapter_path = package_dir / "adapter.py"
    if not adapter_path.exists():
        return None
    if str(package_dir) not in sys.path:
        sys.path.insert(0, str(package_dir))
    mod = _load_module(adapter_path, f"dataclean_adapter_hooks_{package_dir.name}")
    adapter = None
    if hasattr(mod, "build_adapter"):
        adapter = mod.build_adapter(package_dir)
    else:
        for cls_name in (
            "Abc130kMcapAdapter",
            "HumanoidAdapter",
            "EgoDexAdapter",
            "EgoStandardAdapter",
            "EgoVerseAdapter",
            "RobomindAdapter",
            "AgilexH5Adapter",
        ):
            if hasattr(mod, cls_name):
                adapter = getattr(mod, cls_name)(package_dir)
                break
    if adapter is None:
        return None
    global _PACKAGE_DIR, _ADAPTER
    _PACKAGE_DIR = package_dir
    _ADAPTER = adapter
    return adapter


def get_adapter() -> Any:
    return _ADAPTER


def has_adapter_quality_loader() -> bool:
    ad = _ADAPTER
    if ad is None:
        return False
    if hasattr(ad, "load_quality_arrays"):
        return True
    return hasattr(ad, "load_raw") and hasattr(ad, "to_quality_arrays")


def has_adapter_video_resolver() -> bool:
    return _ADAPTER is not None and hasattr(_ADAPTER, "resolve_source_video")


def try_adapter_load_quality(
    root: Path,
    episode_index: int,
    schema: Any = None,
    **kwargs: Any,
) -> dict[str, np.ndarray] | None:
    """Return quality ``{state, action}`` from adapter, or None to use parquet."""
    ad = _ADAPTER
    if ad is None:
        return None
    root = Path(root)
    ep = int(episode_index)
    if hasattr(ad, "load_quality_arrays"):
        out = ad.load_quality_arrays(root, ep, schema=schema, **kwargs)
        if out is None:
            return None
        return {
            "state": np.asarray(out["state"], dtype=np.float64),
            "action": np.asarray(out["action"], dtype=np.float64),
        }
    if hasattr(ad, "load_raw") and hasattr(ad, "to_quality_arrays"):
        if hasattr(ad, "episode_ref"):
            ref = ad.episode_ref(root, ep)
        else:
            from robot_data_processing.normalize.standard_types import EpisodeRef

            ref = EpisodeRef(episode_index=ep, dataset_root=root)
        raw = ad.load_raw(ref)
        meta = {"fps": kwargs.get("fps", 30), "dataset_root": str(root)}
        standard = ad.to_standard_episode(ref, raw, meta)
        state, action = ad.to_quality_arrays(standard)
        return {
            "state": np.asarray(state, dtype=np.float64),
            "action": np.asarray(action, dtype=np.float64),
        }
    return None


def try_adapter_resolve_video(
    dataset_root: Path,
    episode_index: int,
    source_video_key: str,
) -> Path | None:
    """Return mp4 path from adapter, or None to use default layout resolve."""
    ad = _ADAPTER
    if ad is None or not hasattr(ad, "resolve_source_video"):
        return None
    path = ad.resolve_source_video(Path(dataset_root), int(episode_index), source_video_key)
    if path is None:
        return None
    path = Path(path)
    return path if path.exists() else None
