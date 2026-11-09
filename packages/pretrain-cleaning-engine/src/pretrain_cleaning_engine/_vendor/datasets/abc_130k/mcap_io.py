"""ABC-130k MCAP I/O: discover episodes, sync streams, extract video."""
from __future__ import annotations

import json
import os
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

import numpy as np

TOP_CAMERA_TOPICS = ("/top-camera", "/top-left-camera")
WRIST_CAMERA_TOPICS = ("/left-wrist-camera", "/right-wrist-camera")
OPTIONAL_TOP_RIGHT = "/top-right-camera"

ARM_STATE = ("/left-arm-state", "/right-arm-state")
ARM_ACTION = ("/left-arm-action", "/right-arm-action")
EE_STATE = ("/left-ee-state", "/right-ee-state")
EE_ACTION = ("/left-ee-action", "/right-ee-action")

# Official-repo FK dump under ABC-130k-EEF (row-major 4×4, frame=left_arm_base).
EEF_PARQUET_NAME = "eef_pose.parquet"
EEF_POSE_FRAME = "left_arm_base"
EEF_TOPIC_ATTR = {
    "/left-arm-state": "state_eef_left",
    "/right-arm-state": "state_eef_right",
    "/left-arm-action": "action_eef_left",
    "/right-arm-action": "action_eef_right",
}
CAMERA_INFO_SHORT = {
    "/top-camera": "camera_top",
    "/top-left-camera": "camera_top",
    "/left-wrist-camera": "camera_wrist_left",
    "/right-wrist-camera": "camera_wrist_right",
}


@dataclass
class McapEpisodeInfo:
    episode_dir: Path
    split: str
    task: str
    episode_id: str
    rel_path: str

    @property
    def episode_mcap(self) -> Path:
        return self.episode_dir / "episode.mcap"

    @property
    def annotation_mcap(self) -> Path:
        return self.episode_dir / "annotation.mcap"


@dataclass
class SyncedEpisode:
    """Streams resampled onto the reference camera timeline."""

    timestamps_ns: np.ndarray  # (T,)
    fps: float
    task_name: str
    station: str  # realsense | zed
    metadata: dict[str, str] = field(default_factory=dict)
    # Tx6 joints
    state_arm_left: np.ndarray = field(default_factory=lambda: np.zeros((0, 6)))
    state_arm_right: np.ndarray = field(default_factory=lambda: np.zeros((0, 6)))
    action_arm_left: np.ndarray = field(default_factory=lambda: np.zeros((0, 6)))
    action_arm_right: np.ndarray = field(default_factory=lambda: np.zeros((0, 6)))
    # Tx1 gripper aperture in source (0=closed, 1=open)
    state_grip_left: np.ndarray = field(default_factory=lambda: np.zeros((0, 1)))
    state_grip_right: np.ndarray = field(default_factory=lambda: np.zeros((0, 1)))
    action_grip_left: np.ndarray = field(default_factory=lambda: np.zeros((0, 1)))
    action_grip_right: np.ndarray = field(default_factory=lambda: np.zeros((0, 1)))
    # camera topic → list of Annex-B frame bytes (length T)
    video_frames: dict[str, list[bytes]] = field(default_factory=dict)
    video_codec: dict[str, str] = field(default_factory=dict)
    camera_info: dict[str, dict[str, Any]] = field(default_factory=dict)
    reference_camera: str = "/top-camera"
    # Tx4x4 EEF in left_arm_base (from eef_pose.parquet, NN-synced to camera)
    state_eef_left: np.ndarray = field(default_factory=lambda: np.zeros((0, 4, 4)))
    state_eef_right: np.ndarray = field(default_factory=lambda: np.zeros((0, 4, 4)))
    action_eef_left: np.ndarray = field(default_factory=lambda: np.zeros((0, 4, 4)))
    action_eef_right: np.ndarray = field(default_factory=lambda: np.zeros((0, 4, 4)))
    eef_frame: str = EEF_POSE_FRAME

    @property
    def num_frames(self) -> int:
        return int(self.timestamps_ns.shape[0])


def _require_mcap():
    try:
        from mcap.reader import make_reader  # noqa: F401
        from mcap_protobuf.decoder import DecoderFactory  # noqa: F401
    except ImportError as exc:
        raise ImportError(
            "Reading ABC-130k MCAP requires `mcap` and `mcap-protobuf-support`. "
            "Install with: pip install mcap mcap-protobuf-support"
        ) from exc


def iter_episode_dirs(
    dataset_root: Path,
    *,
    splits: Iterable[str] = ("train", "val"),
) -> list[McapEpisodeInfo]:
    """Enumerate episode directories under ``data/{split}/{task}/episode_*``."""
    root = Path(dataset_root)
    data = root / "data"
    out: list[McapEpisodeInfo] = []
    for split in splits:
        split_dir = data / split
        if not split_dir.is_dir():
            continue
        for task_dir in sorted(p for p in split_dir.iterdir() if p.is_dir() and not p.name.startswith(".")):
            for ep_dir in sorted(
                p for p in task_dir.iterdir() if p.is_dir() and p.name.startswith("episode_")
            ):
                if not (ep_dir / "episode.mcap").exists():
                    continue
                if not (ep_dir / EEF_PARQUET_NAME).is_file():
                    continue
                ep_id = ep_dir.name.removeprefix("episode_")
                rel = f"data/{split}/{task_dir.name}/{ep_dir.name}"
                out.append(
                    McapEpisodeInfo(
                        episode_dir=ep_dir,
                        split=split,
                        task=task_dir.name,
                        episode_id=ep_id,
                        rel_path=rel,
                    )
                )
    return out


def load_or_build_index(
    dataset_root: Path,
    *,
    splits: Iterable[str] = ("train",),
    cache_path: Path | None = None,
    rebuild: bool = False,
) -> list[McapEpisodeInfo]:
    """Cached discover (full tree walk on PFS is slow).

    Default cache is under the package ``.cache/`` (source ``meta/`` is often read-only).
    """
    root = Path(dataset_root)
    splits_t = tuple(splits)
    if cache_path is None:
        pkg_cache = Path(__file__).resolve().parent / ".cache"
        cache = pkg_cache / f"episode_index_{'_'.join(splits_t)}_eef.json"
    else:
        cache = Path(cache_path)
    if cache.exists() and not rebuild:
        rows = json.loads(cache.read_text(encoding="utf-8"))
        infos = []
        for r in rows:
            ep_dir = root / r["rel_path"]
            infos.append(
                McapEpisodeInfo(
                    episode_dir=ep_dir,
                    split=r["split"],
                    task=r["task"],
                    episode_id=r["episode_id"],
                    rel_path=r["rel_path"],
                )
            )
        return infos
    infos = iter_episode_dirs(root, splits=splits_t)
    payload = [
        {
            "split": i.split,
            "task": i.task,
            "episode_id": i.episode_id,
            "rel_path": i.rel_path,
        }
        for i in infos
    ]
    try:
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_text(json.dumps(payload), encoding="utf-8")
    except OSError as exc:
        print(f"[mcap_io] warn: could not write index cache {cache}: {exc}", flush=True)
    return infos


def _nearest_indices(src_ts: np.ndarray, query_ts: np.ndarray) -> np.ndarray:
    """For each query timestamp, index of nearest src timestamp."""
    src = np.asarray(src_ts, dtype=np.int64)
    query = np.asarray(query_ts, dtype=np.int64)
    if src.size == 0:
        raise ValueError("empty source timestamps for nearest-neighbor sync")
    idx = np.searchsorted(src, query, side="left")
    idx = np.clip(idx, 0, len(src) - 1)
    left = np.clip(idx - 1, 0, len(src) - 1)
    choose_left = np.abs(src[left] - query) <= np.abs(src[idx] - query)
    return np.where(choose_left, left, idx).astype(np.int64)


def _estimate_fps(timestamps_ns: np.ndarray) -> float:
    ts = np.asarray(timestamps_ns, dtype=np.float64)
    if ts.size < 2:
        return 30.0
    dts = np.diff(ts) / 1e9
    dts = dts[dts > 1e-6]
    if dts.size == 0:
        return 30.0
    med = float(np.median(dts))
    if med <= 0:
        return 30.0
    return float(1.0 / med)


def _read_mcap_streams(mcap_path: Path) -> tuple[dict[str, Any], dict[str, str]]:
    """Return topic→{ts, payloads...} and metadata dict."""
    _require_mcap()
    from mcap.reader import make_reader
    from mcap_protobuf.decoder import DecoderFactory

    streams: dict[str, dict[str, Any]] = {}
    metadata: dict[str, str] = {}

    with open(mcap_path, "rb") as f:
        reader = make_reader(f, decoder_factories=[DecoderFactory()])
        for meta in reader.iter_metadata():
            metadata.update({str(k): str(v) for k, v in meta.metadata.items()})

    with open(mcap_path, "rb") as f:
        reader = make_reader(f, decoder_factories=[DecoderFactory()])
        for _schema, channel, message, decoded in reader.iter_decoded_messages():
            topic = channel.topic
            bucket = streams.setdefault(
                topic,
                {"ts": [], "position": [], "pose": [], "data": [], "format": None, "info": None},
            )
            bucket["ts"].append(int(message.log_time))
            name = getattr(_schema, "name", "") or ""
            if "RobotState" in name or "GripperState" in name:
                bucket["position"].append(list(decoded.position) if decoded.position is not None else [])
                pose = list(decoded.pose) if getattr(decoded, "pose", None) else []
                bucket["pose"].append(pose)
            elif "CompressedVideo" in name:
                bucket["data"].append(bytes(decoded.data) if decoded.data is not None else b"")
                if bucket["format"] is None:
                    bucket["format"] = str(getattr(decoded, "format", "") or "h264")
            elif "CameraCalibration" in name:
                bucket["info"] = {
                    "width": int(decoded.width),
                    "height": int(decoded.height),
                    "K": list(decoded.K) if decoded.K is not None else [],
                    "distortion_model": str(getattr(decoded, "distortion_model", "") or ""),
                    "D": list(decoded.D) if getattr(decoded, "D", None) else [],
                }
            elif "Instructions" in name:
                bucket["text"] = str(getattr(decoded, "data", "") or "")

    for topic, bucket in streams.items():
        bucket["ts"] = np.asarray(bucket["ts"], dtype=np.int64)
    return streams, metadata


def _pick_reference_camera(topics: set[str]) -> str:
    for cand in TOP_CAMERA_TOPICS:
        if cand in topics:
            return cand
    raise KeyError(f"no top camera topic in {sorted(topics)}")


def eef_parquet_path(episode_dir: Path) -> Path:
    return Path(episode_dir) / EEF_PARQUET_NAME


def load_eef_pose_by_topic(parquet_path: Path) -> dict[str, tuple[np.ndarray, np.ndarray]]:
    """Read official FK dump: topic → ``(timestamp_ns (N,), pose_4x4 (N,4,4))``.

    Pose is row-major 4×4 in ``left_arm_base`` (see ABC-130k-EEF ``extraction_config.json``).
    """
    import pyarrow.compute as pc
    import pyarrow.parquet as pq

    path = Path(parquet_path)
    if not path.is_file():
        raise FileNotFoundError(f"missing EEF parquet: {path}")
    table = pq.read_table(path, columns=["topic", "timestamp_ns", "pose"])
    out: dict[str, tuple[np.ndarray, np.ndarray]] = {}
    for topic in EEF_TOPIC_ATTR:
        sub = table.filter(pc.equal(table.column("topic"), topic))
        if sub.num_rows == 0:
            raise KeyError(f"{path} has no rows for topic {topic}")
        ts = np.asarray(sub.column("timestamp_ns").to_numpy(), dtype=np.int64)
        pose16 = np.vstack(
            [np.asarray(x.as_py(), dtype=np.float64) for x in sub.column("pose")]
        )
        if pose16.shape[1] != 16:
            raise ValueError(f"{path} {topic} pose dim {pose16.shape} expected Nx16")
        out[topic] = (ts, pose16.reshape(-1, 4, 4))
    return out


def _sync_eef_mats(
    ts: np.ndarray,
    mats: np.ndarray,
    query_ts: np.ndarray,
) -> np.ndarray:
    idx = _nearest_indices(ts, query_ts)
    return np.asarray(mats, dtype=np.float64)[idx]


def camera_info_to_intrinsic(camera_info: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """MCAP CameraCalibration → ``{camera_top.matrix, ...}`` for P6 scaling."""
    intrinsic: dict[str, Any] = {}
    for topic, info in camera_info.items():
        short = CAMERA_INFO_SHORT.get(topic)
        if not short or not info:
            continue
        k = np.asarray(info.get("K") or [], dtype=np.float64).reshape(-1)
        if k.size == 9:
            matrix = k.reshape(3, 3)
        elif k.size == 0:
            continue
        else:
            continue
        dist = list(info.get("D") or [0.0] * 5)
        intrinsic[f"{short}.matrix"] = matrix.astype(np.float64)
        intrinsic[f"{short}.dist_coeffs"] = dist
        if info.get("width") and info.get("height"):
            intrinsic[f"{short}.width"] = int(info["width"])
            intrinsic[f"{short}.height"] = int(info["height"])
    return intrinsic


def load_synced_episode(episode_dir: Path | McapEpisodeInfo) -> SyncedEpisode:
    """Load ``episode.mcap`` and sync all streams onto the reference camera timeline."""
    if isinstance(episode_dir, McapEpisodeInfo):
        mcap_path = episode_dir.episode_mcap
        default_task = episode_dir.task.replace("_", " ")
    else:
        mcap_path = Path(episode_dir) / "episode.mcap"
        default_task = Path(episode_dir).parent.name.replace("_", " ")

    streams, metadata = _read_mcap_streams(mcap_path)
    topics = set(streams)
    ref_cam = _pick_reference_camera(topics)
    ref_ts = streams[ref_cam]["ts"]
    if ref_ts.size == 0:
        raise RuntimeError(f"empty reference camera stream: {ref_cam}")
    fps = _estimate_fps(ref_ts)
    T = int(ref_ts.shape[0])

    def sync_vec(topic: str, dim: int) -> np.ndarray:
        if topic not in streams:
            return np.zeros((T, dim), dtype=np.float64)
        ts = streams[topic]["ts"]
        pos = streams[topic]["position"]
        if len(pos) == 0:
            return np.zeros((T, dim), dtype=np.float64)
        idx = _nearest_indices(ts, ref_ts)
        out = np.zeros((T, dim), dtype=np.float64)
        for i, j in enumerate(idx):
            row = pos[int(j)]
            n = min(dim, len(row))
            if n:
                out[i, :n] = row[:n]
        return out

    def sync_scalar(topic: str) -> np.ndarray:
        return sync_vec(topic, 1)

    video_frames: dict[str, list[bytes]] = {}
    video_codec: dict[str, str] = {}
    camera_info: dict[str, dict[str, Any]] = {}

    cam_topics = [ref_cam, *WRIST_CAMERA_TOPICS]
    if OPTIONAL_TOP_RIGHT in topics:
        cam_topics.append(OPTIONAL_TOP_RIGHT)
    for cam in cam_topics:
        if cam not in streams:
            continue
        ts = streams[cam]["ts"]
        frames = streams[cam]["data"]
        idx = _nearest_indices(ts, ref_ts)
        video_frames[cam] = [frames[int(j)] for j in idx]
        video_codec[cam] = streams[cam]["format"] or "h264"
        info_topic = f"{cam}-info"
        if info_topic in streams and streams[info_topic]["info"] is not None:
            camera_info[cam] = streams[info_topic]["info"]

    task_name = metadata.get("task_name") or metadata.get("instruction")
    if not task_name and "/instruction" in streams:
        task_name = streams["/instruction"].get("text") or default_task
    task_name = task_name or default_task

    station = "zed" if "/top-left-camera" in topics else "realsense"

    ep_dir = mcap_path.parent
    eef_path = eef_parquet_path(ep_dir)
    eef_by_topic = load_eef_pose_by_topic(eef_path)
    eef_synced: dict[str, np.ndarray] = {}
    for topic, attr in EEF_TOPIC_ATTR.items():
        ts, mats = eef_by_topic[topic]
        eef_synced[attr] = _sync_eef_mats(ts, mats, ref_ts)

    return SyncedEpisode(
        timestamps_ns=ref_ts,
        fps=fps,
        task_name=str(task_name),
        station=station,
        metadata=metadata,
        state_arm_left=sync_vec("/left-arm-state", 6),
        state_arm_right=sync_vec("/right-arm-state", 6),
        action_arm_left=sync_vec("/left-arm-action", 6),
        action_arm_right=sync_vec("/right-arm-action", 6),
        state_grip_left=sync_scalar("/left-ee-state"),
        state_grip_right=sync_scalar("/right-ee-state"),
        action_grip_left=sync_scalar("/left-ee-action"),
        action_grip_right=sync_scalar("/right-ee-action"),
        video_frames=video_frames,
        video_codec=video_codec,
        camera_info=camera_info,
        reference_camera=ref_cam,
        state_eef_left=eef_synced["state_eef_left"],
        state_eef_right=eef_synced["state_eef_right"],
        action_eef_left=eef_synced["action_eef_left"],
        action_eef_right=eef_synced["action_eef_right"],
        eef_frame=EEF_POSE_FRAME,
    )


def aperture_to_closedness(aperture: np.ndarray) -> np.ndarray:
    """Source gripper aperture is 0=closed, 1=open → Dataclean closedness 1=closed."""
    arr = np.asarray(aperture, dtype=np.float64)
    return np.clip(1.0 - arr, 0.0, 1.0)


def pack_state_action_14d(ep: SyncedEpisode) -> tuple[np.ndarray, np.ndarray]:
    """Pack L6+Lg+R6+Rg (closedness) into Tx14 state/action."""
    sg_l = aperture_to_closedness(ep.state_grip_left).reshape(-1, 1)
    sg_r = aperture_to_closedness(ep.state_grip_right).reshape(-1, 1)
    ag_l = aperture_to_closedness(ep.action_grip_left).reshape(-1, 1)
    ag_r = aperture_to_closedness(ep.action_grip_right).reshape(-1, 1)
    state = np.concatenate(
        [ep.state_arm_left, sg_l, ep.state_arm_right, sg_r], axis=1
    ).astype(np.float32)
    action = np.concatenate(
        [ep.action_arm_left, ag_l, ep.action_arm_right, ag_r], axis=1
    ).astype(np.float32)
    return state, action


def camera_export_map_from_topics(
    topics: Iterable[str],
    *,
    reference_camera: str | None = None,
) -> dict[str, str]:
    """MCAP topic → LeRobot video key (observation.images.*)."""
    topic_set = set(topics)
    ref = reference_camera if reference_camera in topic_set else _pick_reference_camera(topic_set)
    mapping: dict[str, str] = {}
    if ref in topic_set:
        mapping[ref] = "observation.images.top"
    if "/left-wrist-camera" in topic_set:
        mapping["/left-wrist-camera"] = "observation.images.left_wrist"
    if "/right-wrist-camera" in topic_set:
        mapping["/right-wrist-camera"] = "observation.images.right_wrist"
    return mapping


def camera_export_map(ep: SyncedEpisode) -> dict[str, str]:
    """MCAP topic → LeRobot video key (observation.images.*)."""
    return camera_export_map_from_topics(ep.video_frames, reference_camera=ep.reference_camera)


def load_native_camera_video(
    episode_dir: Path | McapEpisodeInfo,
) -> tuple[dict[str, list[bytes]], dict[str, str], dict[str, np.ndarray], str]:
    """Read original-order Annex-B per camera (no NN sync onto the reference timeline).

    NN-sync / stride on H264 starts mid-GOP and drops the only SPS/PPS (usually on
    the first IDR), which makes ffmpeg ``non-existing PPS 0 referenced``.
    """
    if isinstance(episode_dir, McapEpisodeInfo):
        mcap_path = episode_dir.episode_mcap
    else:
        mcap_path = Path(episode_dir) / "episode.mcap"
    _require_mcap()
    from mcap.reader import make_reader
    from mcap_protobuf.decoder import DecoderFactory

    cam_topics = (*TOP_CAMERA_TOPICS, *WRIST_CAMERA_TOPICS, OPTIONAL_TOP_RIGHT)
    frames: dict[str, list[bytes]] = {}
    codecs: dict[str, str] = {}
    ts: dict[str, list[int]] = {}
    with open(mcap_path, "rb") as f:
        reader = make_reader(f, decoder_factories=[DecoderFactory()])
        for schema, channel, message, decoded in reader.iter_decoded_messages(topics=list(cam_topics)):
            name = getattr(schema, "name", "") or ""
            if "CompressedVideo" not in name:
                continue
            topic = channel.topic
            frames.setdefault(topic, []).append(bytes(decoded.data) if decoded.data is not None else b"")
            ts.setdefault(topic, []).append(int(message.log_time))
            if topic not in codecs:
                codecs[topic] = str(getattr(decoded, "format", "") or "h264")
    ts_arr = {k: np.asarray(v, dtype=np.int64) for k, v in ts.items()}
    ref_cam = _pick_reference_camera(set(frames))
    return frames, codecs, ts_arr, ref_cam


_START_CODE_4 = b"\x00\x00\x00\x01"
_START_CODE_3 = b"\x00\x00\x01"


def _iter_annexb_nals(data: bytes):
    if not data:
        return
    starts: list[int] = []
    i = 0
    n = len(data)
    while i < n:
        if i + 3 < n and data[i : i + 4] == _START_CODE_4:
            starts.append(i)
            i += 4
            continue
        if i + 2 < n and data[i : i + 3] == _START_CODE_3:
            starts.append(i)
            i += 3
            continue
        i += 1
    for k, start in enumerate(starts):
        end = starts[k + 1] if k + 1 < len(starts) else n
        if end > start:
            yield data[start:end]


def _nal_payload(nal: bytes) -> bytes:
    if nal.startswith(_START_CODE_4):
        return nal[4:]
    if nal.startswith(_START_CODE_3):
        return nal[3:]
    return nal


def _is_hevc_codec(codec: str) -> bool:
    return (codec or "").lower() in ("h265", "hevc")


def _h264_nal_type(nal: bytes) -> int:
    payload = _nal_payload(nal)
    return (payload[0] & 0x1F) if payload else -1


def _hevc_nal_type(nal: bytes) -> int:
    payload = _nal_payload(nal)
    return ((payload[0] >> 1) & 0x3F) if payload else -1


def _frame_has_param_sets(frame: bytes, codec: str) -> bool:
    hevc = _is_hevc_codec(codec)
    has_sps = has_pps = False
    for nal in _iter_annexb_nals(frame):
        if hevc:
            t = _hevc_nal_type(nal)
            has_sps = has_sps or t == 33
            has_pps = has_pps or t == 34
        else:
            t = _h264_nal_type(nal)
            has_sps = has_sps or t == 7
            has_pps = has_pps or t == 8
        if has_sps and has_pps:
            return True
    return False


def _collect_param_sets(frames: list[bytes], codec: str) -> bytes:
    hevc = _is_hevc_codec(codec)
    vps = sps = pps = b""
    for fr in frames:
        for nal in _iter_annexb_nals(fr):
            if hevc:
                t = _hevc_nal_type(nal)
                if t == 32 and not vps:
                    vps = nal
                elif t == 33 and not sps:
                    sps = nal
                elif t == 34 and not pps:
                    pps = nal
            else:
                t = _h264_nal_type(nal)
                if t == 7 and not sps:
                    sps = nal
                elif t == 8 and not pps:
                    pps = nal
        if (hevc and vps and sps and pps) or (not hevc and sps and pps):
            break
    return b"".join(x for x in (vps, sps, pps) if x)


def ensure_leading_param_sets(frames: list[bytes], codec: str = "h264") -> list[bytes]:
    """Prepend SPS/PPS (and HEVC VPS) when the first AU does not carry them."""
    if not frames or _frame_has_param_sets(frames[0], codec):
        return frames
    prefix = _collect_param_sets(frames, codec)
    if not prefix:
        return frames
    out = list(frames)
    out[0] = prefix + frames[0]
    return out


def _clip_ffmpeg_err(*chunks: str, limit: int = 400) -> str:
    text = " | ".join(c.strip() for c in chunks if c and c.strip())
    text = " ".join(text.split())
    return text[:limit]


def _mp4_has_video(path: Path) -> bool:
    if not path.exists() or path.stat().st_size <= 0:
        return False
    proc = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-select_streams",
            "v:0",
            "-show_entries",
            "stream=codec_type",
            "-of",
            "csv=p=0",
            str(path),
        ],
        capture_output=True,
        text=True,
    )
    return proc.returncode == 0 and "video" in (proc.stdout or "").lower()


def write_annexb_video(
    frames: list[bytes],
    dst: Path,
    *,
    codec: str = "h264",
    fps: float = 30.0,
) -> None:
    """Mux Annex-B elementary frames into an mp4 via ffmpeg stream copy."""
    dst = Path(dst)
    dst.parent.mkdir(parents=True, exist_ok=True)
    if not frames:
        raise ValueError(f"no frames to write: {dst}")
    codec = (codec or "h264").lower()
    frames = ensure_leading_param_sets(list(frames), codec)
    raw_suffix = ".h265" if codec in ("h265", "hevc") else ".h264"
    demux = "hevc" if codec in ("h265", "hevc") else "h264"

    fd, raw_name = tempfile.mkstemp(suffix=raw_suffix)
    os.close(fd)
    raw_path = Path(raw_name)
    try:
        with open(raw_path, "wb") as f:
            for fr in frames:
                f.write(fr)
        common = [
            "ffmpeg",
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-fflags",
            "+genpts+discardcorrupt",
            "-err_detect",
            "ignore_err",
            "-f",
            demux,
            "-r",
            f"{float(fps):.6f}",
            "-i",
            str(raw_path),
        ]
        cmd = [*common, "-c", "copy", "-an", str(dst)]
        proc = subprocess.run(cmd, capture_output=True, text=True)
        if proc.returncode != 0 or not _mp4_has_video(dst):
            cmd_re = [
                *common,
                "-c:v",
                "libx264",
                "-pix_fmt",
                "yuv420p",
                "-an",
                str(dst),
            ]
            proc2 = subprocess.run(cmd_re, capture_output=True, text=True)
            if proc2.returncode != 0 or not _mp4_has_video(dst):
                err = _clip_ffmpeg_err(proc2.stderr or "", proc.stderr or "")
                raise RuntimeError(f"ffmpeg failed for {dst}: {err}")
    finally:
        raw_path.unlink(missing_ok=True)
