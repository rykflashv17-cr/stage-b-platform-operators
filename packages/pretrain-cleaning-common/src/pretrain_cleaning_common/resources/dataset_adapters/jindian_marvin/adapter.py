"""Read Jindian Marvin joint and gripper CDR topics for Stage B quality.

The quality canvas is 16d: seven left joints, seven right joints, and two
gripper widths. TCP pose and camera topics stay in the source MCAP and are
outside this profile's numerical quality checks. v4 retains or deletes the
whole source MCAP; it does not export a new MCAP.
"""

from __future__ import annotations

import struct
from pathlib import Path

import numpy as np

from robot_data_processing.normalize.standard_types import StandardEpisode

STATE = "/raw/marvin/joint_state"
TARGET = "/command/marvin/joint_target"
LEFT_GRIP = "/raw/das/left/state"
RIGHT_GRIP = "/raw/das/right/state"
GRIP_TARGET = "/command/das/target"
TOPICS = (STATE, TARGET, LEFT_GRIP, RIGHT_GRIP, GRIP_TARGET)
JOINT_NAMES = tuple(f"Joint{i}_{side}" for side in ("L", "R") for i in range(1, 8))
GRIPPER_FIELDS = {LEFT_GRIP: "left_gripper_width", RIGHT_GRIP: "right_gripper_width"}


class Cdr:
    def __init__(self, data: bytes):
        if len(data) < 4 or data[1] not in (0, 1):
            raise ValueError("invalid CDR encapsulation")
        self.data = data
        self.pos = 4
        self.endian = "<" if data[1] == 1 else ">"

    def read(self, fmt: str):
        size = struct.calcsize(fmt)
        self.pos = 4 + ((self.pos - 4 + size - 1) // size) * size
        value = struct.unpack_from(self.endian + fmt, self.data, self.pos)[0]
        self.pos += size
        return value

    def string(self) -> str:
        size = self.read("I")
        if size < 1 or self.pos + size > len(self.data):
            raise ValueError("invalid CDR string length")
        raw = self.data[self.pos:self.pos + size - 1]
        self.pos += size
        return raw.decode("utf-8")

    def strings(self) -> list[str]:
        return [self.string() for _ in range(self.read("I"))]

    def doubles(self) -> np.ndarray:
        count = self.read("I")
        self.pos = 4 + ((self.pos - 4 + 7) // 8) * 8
        end = self.pos + count * 8
        if end > len(self.data):
            raise ValueError("invalid CDR double array length")
        values = np.frombuffer(self.data, dtype=self.endian + "f8", count=count, offset=self.pos).copy()
        self.pos = end
        return values

    def header(self) -> None:
        self.read("i")
        self.read("I")
        self.string()


def _named_positions(data: bytes, *, trajectory: bool) -> dict[str, float]:
    cdr = Cdr(data)
    cdr.header()
    names = cdr.strings()
    if trajectory and cdr.read("I") < 1:
        return {}
    positions = cdr.doubles()
    if len(names) != len(positions):
        raise ValueError("joint names and positions have different lengths")
    return dict(zip(names, positions))


def _align_nearest(reference: np.ndarray, samples: list[tuple[int, object]], *,
                   max_gap_ns: int | None, label: str) -> list[object]:
    if not samples:
        raise ValueError(f"missing {label}")
    times = np.asarray([t for t, _ in samples], dtype=np.int64)
    at = np.searchsorted(times, reference)
    before = np.maximum(at - 1, 0)
    after = np.minimum(at, len(times) - 1)
    indices = np.where(abs(reference - times[before]) <= abs(reference - times[after]), before, after)
    if max_gap_ns is not None and np.any(abs(reference - times[indices]) > max_gap_ns):
        raise ValueError(f"{label} has a gap exceeding {max_gap_ns / 1e9:g}s")
    return [samples[int(i)][1] for i in indices]


def _find_mcap(ref) -> Path:
    direct = Path(ref.dataset_root) / ref.rel_path
    if direct.suffix == ".mcap" and direct.is_file():
        return direct
    files = sorted((Path(ref.dataset_root) / ref.rel_path).glob("*.mcap"))
    if len(files) != 1:
        raise ValueError(f"expected one MCAP in {ref.rel_path}, got {len(files)}")
    return files[0]


class JindianMarvinAdapter:
    def __init__(self, package_dir: Path):
        self.package_dir = Path(package_dir)

    def load_raw(self, ref) -> dict:
        from mcap.reader import make_reader

        samples: dict[str, list[tuple[int, object]]] = {topic: [] for topic in TOPICS}
        with _find_mcap(ref).open("rb") as stream:
            for _schema, channel, message in make_reader(stream).iter_messages(topics=TOPICS):
                topic = channel.topic
                value = _named_positions(message.data, trajectory=topic in (TARGET, GRIP_TARGET))
                if topic in GRIPPER_FIELDS:
                    # Structural validity belongs to input normalization. Gap
                    # B02 admits statistical inputs; B03 alone rejects files.
                    width = value.get(GRIPPER_FIELDS[topic])
                    if width is None or not np.isfinite(width):
                        continue
                if value:
                    samples[topic].append((int(message.log_time), value))
        if not samples[STATE]:
            raise ValueError("no Marvin joint states")
        # Robot state is 50 Hz; every second sample gives the 25 Hz quality
        # timeline, close to the delivered colour video cadence.
        times = np.asarray([t for t, _ in samples[STATE][::2]], dtype=np.int64)
        if np.any(np.diff(times) < 0):
            raise ValueError("joint state timestamps out of order")
        state_rows = [record for _, record in samples[STATE][::2]]
        target_rows = _align_nearest(times, samples[TARGET], max_gap_ns=100_000_000, label=TARGET)
        state = np.zeros((len(times), 16), dtype=np.float64)
        action = np.zeros_like(state)
        for col, name in enumerate(JOINT_NAMES):
            state[:, col] = [row[name] for row in state_rows]
            action[:, col] = [row[name] for row in target_rows]
        for col, topic, name in (
            (14, LEFT_GRIP, "left_gripper_width"),
            (15, RIGHT_GRIP, "right_gripper_width"),
        ):
            if not samples[topic]:
                # Preserve unknown values as facts. B02 excludes them from
                # statistics; B03 diagnoses coverage or records a numerical
                # error when coverage checking is disabled.
                state[:, col] = np.nan
                continue
            # The integrated quality detector checks measurement freshness.
            # Preserve the v3 numerical alignment; v4 changes its rejection
            # policy to whole-file rejection rather than section salvage.
            rows = _align_nearest(times, samples[topic], max_gap_ns=None, label=topic)
            state[:, col] = [row[name] for row in rows]
        # Gripper commands are event-like. Carry the last target; before the
        # first command, use measured width only for a complete array.
        action[:, 14:16] = state[:, 14:16]
        for command_time, values in samples[GRIP_TARGET]:
            at = int(np.searchsorted(times, command_time, side="left"))
            for col, name in ((14, "left_gripper_width"), (15, "right_gripper_width")):
                if name in values:
                    action[at:, col] = values[name]
        missing_columns = [col for col, topic in ((14, LEFT_GRIP), (15, RIGHT_GRIP)) if not samples[topic]]
        observed_columns = [col for col in range(16) if col not in missing_columns]
        if not np.isfinite(state[:, observed_columns]).all() or not np.isfinite(action[:, observed_columns]).all():
            raise ValueError("non-finite joint or gripper values")
        return {
            "state": state, "action": action, "time_ns": times,
            "valid_measurement_times_ns": {
                topic: np.asarray([t for t, _ in samples[topic]], dtype=np.int64)
                for topic in GRIPPER_FIELDS
            },
        }

    def to_standard_episode(self, ref, raw: dict, meta: dict) -> StandardEpisode:
        return StandardEpisode(
            episode_index=ref.episode_index,
            num_frames=len(raw["state"]),
            fields={"jindian.state": raw["state"], "jindian.action": raw["action"]},
            camera_keys=["observation.images.camera_reference"],
            meta={**meta, "quality_timeline": {
                "frame_times_ns": raw["time_ns"],
                "valid_measurement_times_ns": raw["valid_measurement_times_ns"],
            }},
            has_camera_top=True,
        )

    def to_quality_arrays(self, standard: StandardEpisode) -> tuple[np.ndarray, np.ndarray]:
        return standard.fields["jindian.state"], standard.fields["jindian.action"]

    def to_quality_timeline(self, standard: StandardEpisode) -> dict:
        """Metadata for exactly the rows returned by to_quality_arrays()."""
        return standard.meta["quality_timeline"]


def build_adapter(package_dir: Path) -> JindianMarvinAdapter:
    return JindianMarvinAdapter(package_dir)
