"""ABC-130k (YAM bimanual MCAP + official EEF parquet) → Dataclean standard adapter.

Oneshot MCAP path (layout=mcap), root = ``ABC-130k-EEF``:
  - ``load_raw`` / ``load_quality_arrays`` — sync streams + ``eef_pose.parquet``
  - ``resolve_source_video`` — extract Annex-B → cached mp4 for P7

Packed Stage A quality 14d: L6 + Lg + R6 + Rg (closedness = 1 − aperture).
~60 Hz sources are stride-2 downsampled to 30 Hz on proprio only. Cached video is
the native-order Annex-B bitstream (P7 trims / resamples to T at 30 Hz).
EEF xyz + rotation_6d is exported (left_arm_base) but not packed into Stage A quality.
Stage B identity P1 packs 32d via ``pack_standard_joint_eef_quality``.
"""
from __future__ import annotations

import json
import os
import shutil
import socket
import tempfile
import threading
from pathlib import Path
from typing import Any

import numpy as np

from robot_data_processing.normalize.standard_types import EpisodeRef, StandardEpisode
from robot_data_processing.normalize.transforms_standard import (
    as_col,
    matrices_to_position_rotation_6d,
)

PRIMARY_CAMERA = "observation.images.top"
AUX_CAMERAS = (
    "observation.images.left_wrist",
    "observation.images.right_wrist",
)
STATE_KEY = "observation.state"
ACTION_KEY = "action"

# Mixed-source FPS: ~60 Hz episodes keep every 2nd frame → 30 Hz. Native ~30 stays.
TARGET_FPS = 30.0
HIGH_FPS = 60.0
FPS_REL_TOL = 0.15
PFS_TMP_DEFAULT = Path("/mnt/pfs/Data/liuweilin/Dataclean/tmp")


def _pfs_tmp_root() -> Path:
    raw = os.environ.get("ABC130K_PFS_TMP", "").strip()
    root = Path(raw) if raw else PFS_TMP_DEFAULT
    root.mkdir(parents=True, exist_ok=True)
    return root


def _ensure_pfs_tmpdir() -> Path:
    """Keep tempfile / ffmpeg scratch off the node-local /tmp."""
    root = _pfs_tmp_root()
    cur = (os.environ.get("TMPDIR") or "").strip()
    if not cur or cur == "/tmp" or cur.startswith("/tmp/"):
        os.environ["TMPDIR"] = str(root)
    os.environ.setdefault("TEMP", os.environ["TMPDIR"])
    os.environ.setdefault("TMP", os.environ["TMPDIR"])
    for key in ("TEMP", "TMP"):
        val = os.environ.get(key, "")
        if val == "/tmp" or val.startswith("/tmp/"):
            os.environ[key] = os.environ["TMPDIR"]
    tempfile.tempdir = os.environ["TMPDIR"]
    return root


def _relink_tmp_cache_to_pfs(path: Path) -> Path:
    """``/tmp/...`` → symlink onto shared Dataclean/tmp (per-host, no local-disk fill)."""
    path = Path(path)
    try:
        under_tmp = path.is_relative_to(Path("/tmp"))
    except (AttributeError, ValueError):
        text = str(path)
        under_tmp = text == "/tmp" or text.startswith("/tmp/")
    if not under_tmp:
        return path
    host = (socket.gethostname() or "host").split(".")[0]
    target = _pfs_tmp_root() / "abc130k_video_cache" / f"{host}_{path.name}"
    target.mkdir(parents=True, exist_ok=True)
    try:
        if path.is_symlink() or path.exists():
            if path.is_symlink() and path.resolve() == target.resolve():
                return path
            if path.is_dir() and not path.is_symlink():
                shutil.rmtree(path)
            else:
                path.unlink()
        path.symlink_to(target)
        return path
    except OSError as exc:
        print(f"[abc_130k] /tmp cache symlink failed ({exc}); using {target}", flush=True)
        return target


def frame_stride_for_source_fps(src_fps: float) -> int:
    """Return 2 if source is in the 60 Hz band, else 1."""
    fps = float(src_fps)
    if fps <= 0:
        return 1
    if abs(fps - HIGH_FPS) / HIGH_FPS <= FPS_REL_TOL:
        return 2
    return 1


def _stride_time_major(raw: dict[str, np.ndarray], stride: int, length: int) -> dict[str, np.ndarray]:
    if stride <= 1:
        return raw
    out: dict[str, np.ndarray] = {}
    for key, val in raw.items():
        arr = np.asarray(val)
        out[key] = arr[::stride] if arr.shape[0] == length else arr
    return out

_EEF_PREFIXES = (
    ("observation.state.eef.left", "state_eef_left"),
    ("observation.state.eef.right", "state_eef_right"),
    ("action.eef.left", "action_eef_left"),
    ("action.eef.right", "action_eef_right"),
)


def _as_tx14(arr: np.ndarray, name: str) -> np.ndarray:
    out = np.asarray(arr, dtype=np.float64)
    if out.ndim != 2 or out.shape[1] < 14:
        raise ValueError(f"{name} must be Tx14, got {out.shape}")
    return out[:, :14]


def _filter_episode(raw: dict[str, np.ndarray], episode_index: int) -> dict[str, np.ndarray]:
    if "episode_index" not in raw:
        return raw
    ei = np.asarray(raw["episode_index"]).reshape(-1).astype(np.int64)
    mask = ei == int(episode_index)
    if mask.all():
        return raw
    out: dict[str, np.ndarray] = {}
    for k, v in raw.items():
        arr = np.asarray(v)
        out[k] = arr[mask] if arr.shape[0] == mask.shape[0] else arr
    return out


def _pack_14_to_fields(arr14: np.ndarray, *, prefix: str) -> dict[str, np.ndarray]:
    return {
        f"{prefix}.arm.left.joint_position": arr14[:, 0:6].astype(np.float32),
        f"{prefix}.gripper.left.closedness": as_col(arr14[:, 6]),
        f"{prefix}.arm.right.joint_position": arr14[:, 7:13].astype(np.float32),
        f"{prefix}.gripper.right.closedness": as_col(arr14[:, 13]),
    }


class Abc130kMcapAdapter:
    def __init__(self, package_dir: Path):
        self.package_dir = Path(package_dir)
        keymap_path = self.package_dir / "keymap.json"
        self.keymap = json.loads(keymap_path.read_text(encoding="utf-8")) if keymap_path.exists() else {}
        _ensure_pfs_tmpdir()
        env_cache = os.environ.get("ABC130K_VIDEO_CACHE", "").strip()
        self.video_cache_root = _relink_tmp_cache_to_pfs(
            Path(env_cache) if env_cache else (self.package_dir / ".cache" / "videos")
        )
        self._index_by_root: dict[str, list[Any]] = {}
        self._video_locks: dict[int, threading.Lock] = {}
        self._video_locks_guard = threading.Lock()
        self._intrinsic_by_ep: dict[int, dict[str, Any]] = {}
        self._eef_frame_by_ep: dict[int, str] = {}
        self._stride_by_ep: dict[int, int] = {}
        self._source_fps_by_ep: dict[int, float] = {}

    def _root_key(self, root: Path) -> str:
        try:
            return str(Path(root).resolve())
        except OSError:
            return str(root)

    def _infos(self, dataset_root: Path):
        from mcap_io import load_or_build_index

        key = self._root_key(dataset_root)
        if key not in self._index_by_root:
            splits_env = os.environ.get("ABC130K_SPLITS", "train").strip()
            splits = tuple(s.strip() for s in splits_env.split(",") if s.strip()) or ("train",)
            self._index_by_root[key] = load_or_build_index(Path(dataset_root), splits=splits)
        return self._index_by_root[key]

    def episode_ref(self, dataset_root: Path, episode_index: int) -> EpisodeRef:
        """Map contiguous episode_index → MCAP EpisodeRef (for quality hook)."""
        infos = self._infos(dataset_root)
        ep = int(episode_index)
        if ep < 0 or ep >= len(infos):
            raise IndexError(f"episode_index {ep} out of range [0, {len(infos)})")
        info = infos[ep]
        return EpisodeRef(
            episode_index=ep,
            dataset_root=Path(dataset_root),
            part=info.split,
            task=info.task,
            rel_path=info.rel_path,
        )

    def camera_name_map(self) -> dict[str, str]:
        return {
            PRIMARY_CAMERA: "camera_top",
            "observation.images.camera_top": "camera_top",
            "camera_top": "camera_top",
            "observation.images.left_wrist": "camera_wrist_left",
            "observation.images.right_wrist": "camera_wrist_right",
        }

    def camera_name_map_from_root(self, dataset_root: Path) -> dict[str, str]:
        """Stage B: keep export names (camera_reference / aux_*), do not remap to camera_top."""
        info_path = Path(dataset_root) / "meta" / "info.json"
        if not info_path.exists():
            return self.camera_name_map()
        info = json.loads(info_path.read_text(encoding="utf-8"))
        out: dict[str, str] = {}
        for key, spec in (info.get("features") or {}).items():
            if not (isinstance(spec, dict) and spec.get("dtype") == "video"):
                continue
            short = key.split(".")[-1] if key.startswith("observation.images.") else key
            out[key] = short
        return out or self.camera_name_map()

    def load_raw(self, ref: EpisodeRef) -> dict[str, np.ndarray]:
        """Load one MCAP episode into packed arrays (layout=mcap)."""
        from mcap_io import (
            McapEpisodeInfo,
            camera_info_to_intrinsic,
            load_synced_episode,
            pack_state_action_14d,
        )

        if not ref.rel_path:
            ref = self.episode_ref(ref.dataset_root, ref.episode_index)
        ep_dir = Path(ref.dataset_root) / ref.rel_path
        info = McapEpisodeInfo(
            episode_dir=ep_dir,
            split=ref.part or "train",
            task=ref.task or ep_dir.parent.name,
            episode_id=ep_dir.name.removeprefix("episode_"),
            rel_path=ref.rel_path,
        )
        synced = load_synced_episode(info)
        state, action = pack_state_action_14d(synced)
        T = int(state.shape[0])
        src_fps = float(synced.fps)
        stride = frame_stride_for_source_fps(src_fps)
        ts = (np.arange(T, dtype=np.float64) / max(src_fps, 1e-6)).astype(np.float32)
        out: dict[str, np.ndarray] = {
            STATE_KEY: state,
            ACTION_KEY: action,
            "timestamp": ts,
            "frame_index": np.arange(T, dtype=np.int64),
            "episode_index": np.full(T, int(ref.episode_index), dtype=np.int64),
            "language_persistent": np.array([synced.task_name] * T, dtype=object),
        }
        for prefix, attr in _EEF_PREFIXES:
            mats = getattr(synced, attr)
            if mats is None or np.asarray(mats).shape[0] != T:
                raise ValueError(f"EEF {attr} length {np.asarray(mats).shape} != T={T}")
            pos, rot6d = matrices_to_position_rotation_6d(mats, name=prefix)
            out[f"{prefix}.position"] = pos
            out[f"{prefix}.rotation_6d"] = rot6d
        if stride > 1:
            out = _stride_time_major(out, stride, T)
            T = int(np.asarray(out[STATE_KEY]).shape[0])
        out["timestamp"] = (np.arange(T, dtype=np.float64) / TARGET_FPS).astype(np.float32)
        out["frame_index"] = np.arange(T, dtype=np.int64)
        out["episode_index"] = np.full(T, int(ref.episode_index), dtype=np.int64)
        ep_i = int(ref.episode_index)
        self._intrinsic_by_ep[ep_i] = camera_info_to_intrinsic(synced.camera_info)
        self._eef_frame_by_ep[ep_i] = str(synced.eef_frame)
        self._stride_by_ep[ep_i] = int(stride)
        self._source_fps_by_ep[ep_i] = src_fps
        return out

    def load_quality_arrays(
        self,
        root: Path,
        episode_index: int,
        schema: Any = None,
        **kwargs: Any,
    ) -> dict[str, np.ndarray]:
        """Stats/quality hook: MCAP → packed state/action without parquet.

        A LeRobot export root (Stage B input) returns None so the engine reads its parquet.
        """
        del schema, kwargs
        if (Path(root) / "meta" / "info.json").exists():
            return None
        ref = self.episode_ref(root, episode_index)
        raw = self.load_raw(ref)
        standard = self.to_standard_episode(ref, raw, {"dataset_root": str(root)})
        state, action = self.to_quality_arrays(standard)
        return {"state": state, "action": action}

    def _ep_lock(self, episode_index: int) -> threading.Lock:
        with self._video_locks_guard:
            lock = self._video_locks.get(int(episode_index))
            if lock is None:
                lock = threading.Lock()
                self._video_locks[int(episode_index)] = lock
            return lock

    def _video_path(self, episode_index: int, video_key: str, *, stride: int = 1) -> Path:
        chunk = int(episode_index) // 1000
        tag = f"s{int(stride)}"
        return (
            self.video_cache_root
            / f"chunk-{chunk:03d}"
            / video_key
            / f"episode_{int(episode_index):06d}_{tag}.mp4"
        )

    def _find_cached_video(self, episode_index: int, video_key: str) -> Path | None:
        for stride in (2, 1):
            path = self._video_path(episode_index, video_key, stride=stride)
            if path.exists():
                return path
        return None

    def _materialize_videos(self, dataset_root: Path, episode_index: int) -> None:
        from mcap_io import (
            _estimate_fps,
            camera_export_map_from_topics,
            load_native_camera_video,
            write_annexb_video,
        )

        ref = self.episode_ref(dataset_root, episode_index)
        keys = [PRIMARY_CAMERA, *AUX_CAMERAS]
        for stride in (2, 1):
            if all(self._video_path(episode_index, k, stride=stride).exists() for k in keys):
                self._stride_by_ep[int(episode_index)] = stride
                return
        native, codecs, ts_by_cam, ref_cam = load_native_camera_video(
            Path(dataset_root) / ref.rel_path
        )
        cam_map = camera_export_map_from_topics(native, reference_camera=ref_cam)
        src_fps = float(self._source_fps_by_ep.get(int(episode_index)) or 0.0)
        if src_fps <= 0 and ref_cam in ts_by_cam:
            src_fps = _estimate_fps(ts_by_cam[ref_cam])
        if src_fps <= 0:
            src_fps = TARGET_FPS
        stride = frame_stride_for_source_fps(src_fps)
        self._stride_by_ep[int(episode_index)] = stride
        self._source_fps_by_ep[int(episode_index)] = src_fps
        # Native camera order at the camera's own rate — never NN-sync or
        # stride Annex-B (that starts mid-GOP and drops the only SPS/PPS).
        for topic, video_key in cam_map.items():
            dst = self._video_path(episode_index, video_key, stride=stride)
            if dst.exists():
                continue
            frames = list(native.get(topic) or [])
            if not frames:
                continue
            cam_fps = _estimate_fps(ts_by_cam[topic]) if topic in ts_by_cam else src_fps
            codec = codecs.get(topic, "h264")
            try:
                write_annexb_video(frames, dst, codec=codec, fps=cam_fps)
            except Exception as exc:
                print(
                    f"[abc_130k] mux failed ep={episode_index} cam={video_key}: {exc}",
                    flush=True,
                )
                dst.unlink(missing_ok=True)

    def resolve_source_video(
        self,
        dataset_root: Path,
        episode_index: int,
        source_video_key: str,
    ) -> Path | None:
        """P7 hook: ensure cached mp4 exists; return path for normalize/export.

        A LeRobot export root (Stage B input) returns None: the engine resolves its v3 layout.
        """
        if (Path(dataset_root) / "meta" / "info.json").exists():
            return None
        key = source_video_key
        if not key.startswith("observation.images."):
            rev = {v: k for k, v in self.camera_name_map().items() if k.startswith("observation.images.")}
            if key in ("camera_top", "camera_reference"):
                key = PRIMARY_CAMERA
            elif "wrist_left" in key or key == "camera_aux_0":
                key = "observation.images.left_wrist"
            elif "wrist_right" in key or key == "camera_aux_1":
                key = "observation.images.right_wrist"
            elif key in rev:
                key = rev[key]
            else:
                key = f"observation.images.{key}"

        with self._ep_lock(episode_index):
            self._materialize_videos(Path(dataset_root), int(episode_index))
        stride = int(self._stride_by_ep.get(int(episode_index), 1))
        path = self._video_path(int(episode_index), key, stride=stride)
        if path.exists():
            return path
        return self._find_cached_video(int(episode_index), key)

    def to_standard_episode(
        self, ref: EpisodeRef, raw: dict[str, np.ndarray], meta: dict[str, Any]
    ) -> StandardEpisode:
        raw = _filter_episode(raw, ref.episode_index)
        if STATE_KEY not in raw:
            raise KeyError(f"missing {STATE_KEY}")
        state14 = _as_tx14(raw[STATE_KEY], STATE_KEY)
        action14 = _as_tx14(raw.get(ACTION_KEY, state14), ACTION_KEY)
        T = int(state14.shape[0])
        fields: dict[str, np.ndarray] = {}
        fields.update(_pack_14_to_fields(state14, prefix="observation.state"))
        fields.update(_pack_14_to_fields(action14, prefix="action"))
        for prefix, _attr in _EEF_PREFIXES:
            pos_key = f"{prefix}.position"
            rot_key = f"{prefix}.rotation_6d"
            if pos_key not in raw or rot_key not in raw:
                raise KeyError(f"missing EEF {pos_key} / {rot_key} (need ABC-130k-EEF parquet)")
            fields[pos_key] = np.asarray(raw[pos_key], dtype=np.float32)
            fields[rot_key] = np.asarray(raw[rot_key], dtype=np.float32)

        for k, v in raw.items():
            if str(k).startswith("extra_info."):
                fields[str(k)] = np.asarray(v)

        tasks = meta.get("tasks") or ([ref.task] if ref.task else [])
        if "language_persistent" in raw:
            try:
                lang = raw["language_persistent"]
                sample = lang[0] if hasattr(lang, "__getitem__") else lang
                if isinstance(sample, (bytes, str)) and sample:
                    tasks = [sample.decode() if isinstance(sample, bytes) else str(sample)]
            except Exception:
                pass

        return StandardEpisode(
            episode_index=ref.episode_index,
            num_frames=T,
            fields=fields,
            camera_keys=[PRIMARY_CAMERA, *AUX_CAMERAS],
            intrinsic=dict(self._intrinsic_by_ep.get(int(ref.episode_index)) or {}),
            extrinsic={},
            meta={
                **meta,
                "tasks": tasks,
                "source_episode_index": ref.episode_index,
                "dataset_root": meta.get("dataset_root", str(ref.dataset_root)),
                "task": ref.task,
                "part": ref.part,
                "rel_path": ref.rel_path,
                "eef_frame": self._eef_frame_by_ep.get(int(ref.episode_index), "left_arm_base"),
                "source_fps": self._source_fps_by_ep.get(int(ref.episode_index)),
                "frame_stride": self._stride_by_ep.get(int(ref.episode_index), 1),
                "export_fps": TARGET_FPS,
            },
            has_camera_top=True,
        )

    def to_standard_episode_from_aligned(
        self, ref: EpisodeRef, raw: dict[str, np.ndarray], meta: dict[str, Any]
    ) -> StandardEpisode:
        """Identity P1 for Stage A parquet: no MCAP remap / no P6.

        MCAP ``load_raw`` also emits standard EEF names, which trips the engine
        ``_looks_standard_raw`` heuristic. Packed 14d without arm/gripper splits
        is still source — unpack via ``to_standard_episode``.
        """
        if "observation.state.arm.left.joint_position" not in raw and STATE_KEY in raw:
            return self.to_standard_episode(ref, raw, meta)
        skip = {
            "timestamp",
            "frame_index",
            "episode_index",
            "index",
            "task_index",
            "step_validity_mask",
        }
        fields: dict[str, np.ndarray] = {}
        extrinsic: dict[str, np.ndarray] = {}
        T = None
        for key, val in raw.items():
            if key in skip:
                continue
            arr = np.asarray(val)
            if key.startswith("extrinsic."):
                name = key[len("extrinsic.") :]
                if arr.ndim == 3 and arr.shape[-2:] == (4, 4):
                    extrinsic[name] = arr.astype(np.float64)
                elif arr.ndim == 2 and arr.shape[-1] == 16:
                    extrinsic[name] = arr.reshape(arr.shape[0], 4, 4).astype(np.float64)
                else:
                    continue
                if T is None:
                    T = int(extrinsic[name].shape[0])
                continue
            if key.startswith("extra_info."):
                fields[key] = arr
                if T is None and arr.ndim >= 1:
                    T = int(arr.shape[0])
                continue
            if key.startswith("observation.images."):
                continue
            if key.startswith("observation.") or key.startswith("action."):
                fields[key] = arr.astype(np.float32) if arr.dtype != np.float32 else arr
                if T is None:
                    T = int(arr.shape[0])
        if T is None:
            ts = raw.get("timestamp")
            if ts is not None:
                T = int(np.asarray(ts).reshape(-1).shape[0])
        if T is None or T <= 0:
            raise ValueError("aligned parquet has no frames / standard fields")

        cams = [
            k if str(k).startswith("observation.images.") else f"observation.images.{k}"
            for k in (
                meta.get("camera_keys")
                or [
                    "observation.images.camera_reference",
                    "observation.images.camera_aux_0",
                    "observation.images.camera_aux_1",
                ]
            )
        ]
        seen: set[str] = set()
        cams_u: list[str] = []
        for c in cams:
            if c not in seen:
                seen.add(c)
                cams_u.append(c)
        ep_meta = {
            **meta,
            "tasks": meta.get("tasks") or ([ref.task] if ref.task else []),
            "source_episode_index": int(meta.get("source_episode_index", ref.episode_index)),
            "dataset_root": meta.get("dataset_root", str(ref.dataset_root)),
            "input_format": "pseudo_v3",
            "identity_p1": True,
        }
        camera_intrinsics = meta.get("camera_intrinsics")
        if camera_intrinsics is not None:
            ep_meta["camera_intrinsics"] = camera_intrinsics
        return StandardEpisode(
            episode_index=ref.episode_index,
            num_frames=int(T),
            fields=fields,
            camera_keys=cams_u,
            intrinsic=dict(meta.get("intrinsic") or {}),
            extrinsic=extrinsic,
            meta=ep_meta,
            has_camera_top=True,
        )

    def to_quality_arrays(self, standard: StandardEpisode) -> tuple[np.ndarray, np.ndarray]:
        """Stage A: 14d L6+Lg+R6+Rg. Stage B: 32d xyz+rot6d (InternData/humanoid canvas)."""
        fmt = str(standard.meta.get("input_format") or "").lower()
        if fmt in ("pseudo_v3", "v3", "standard", "aggregated_v3") or standard.meta.get(
            "identity_p1"
        ):
            from robot_data_processing.loader import pack_standard_joint_eef_quality

            return pack_standard_joint_eef_quality(standard.fields, standard.num_frames)

        T = standard.num_frames
        state = np.zeros((T, 14), dtype=np.float64)
        action = np.zeros((T, 14), dtype=np.float64)
        state[:, 0:6] = standard.fields["observation.state.arm.left.joint_position"]
        state[:, 6] = standard.fields["observation.state.gripper.left.closedness"].reshape(-1)
        state[:, 7:13] = standard.fields["observation.state.arm.right.joint_position"]
        state[:, 13] = standard.fields["observation.state.gripper.right.closedness"].reshape(-1)
        action[:, 0:6] = standard.fields["action.arm.left.joint_position"]
        action[:, 6] = standard.fields["action.gripper.left.closedness"].reshape(-1)
        action[:, 7:13] = standard.fields["action.arm.right.joint_position"]
        action[:, 13] = standard.fields["action.gripper.right.closedness"].reshape(-1)
        return state, action


def build_adapter(package_dir: Path) -> Abc130kMcapAdapter:
    return Abc130kMcapAdapter(package_dir)
