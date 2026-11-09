from __future__ import annotations

from pathlib import Path

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq
from google.protobuf import descriptor_pb2, descriptor_pool, message_factory
from mcap.writer import Writer

from stage_b_mcap_v4.contracts import McapEpisodeRef
from stage_b_mcap_v4.operators import GlobalStatsOperator, McapReadOperator, QualityDetectionOperator
from stage_b_mcap_v4.spec import build_adapter, load_spec, quality_config
from stage_b_mcap_v4.storage import load_episode
from stage_b_mcap_v4.delivery import McapDeliveryOperator


def _proto_types():
    file_desc = descriptor_pb2.FileDescriptorProto()
    file_desc.name, file_desc.package, file_desc.syntax = "v4_test.proto", "v4_test", "proto3"
    state = file_desc.message_type.add()
    state.name = "RobotState"
    field = state.field.add()
    field.name, field.number, field.type, field.label = "position", 1, field.TYPE_DOUBLE, field.LABEL_REPEATED
    video = file_desc.message_type.add()
    video.name = "CompressedVideo"
    field = video.field.add()
    field.name, field.number, field.type = "data", 1, field.TYPE_BYTES
    field = video.field.add()
    field.name, field.number, field.type = "format", 2, field.TYPE_STRING
    pool = descriptor_pool.DescriptorPool()
    pool.Add(file_desc)
    classes = {
        name: message_factory.GetMessageClass(pool.FindMessageTypeByName(f"v4_test.{name}"))
        for name in ("RobotState", "CompressedVideo")
    }
    descriptor_set = descriptor_pb2.FileDescriptorSet()
    descriptor_set.file.add().CopyFrom(file_desc)
    return classes, descriptor_set.SerializeToString()


def test_abc_32d_adapter_stats_and_whole_rejection_preserve_sidecar(
    tmp_path: Path, upstream_repo: Path,
) -> None:
    root = tmp_path / "source"
    directory = root / "data" / "train" / "pick" / "episode_000"
    directory.mkdir(parents=True)
    source = directory / "episode.mcap"
    classes, descriptor = _proto_types()
    arms = ("/left-arm-state", "/right-arm-state", "/left-arm-action", "/right-arm-action")
    grippers = ("/left-ee-state", "/right-ee-state", "/left-ee-action", "/right-ee-action")
    times = np.arange(20, dtype=np.int64) * 33_333_333 + 1_000_000_000
    with source.open("wb") as stream:
        writer = Writer(stream)
        writer.start()
        state_schema = writer.register_schema("v4_test.RobotState", "protobuf", descriptor)
        video_schema = writer.register_schema("v4_test.CompressedVideo", "protobuf", descriptor)
        channels = {
            topic: writer.register_channel(topic, "protobuf", video_schema if topic == "/top-camera" else state_schema)
            for topic in (*arms, *grippers, "/top-camera")
        }
        for i, timestamp in enumerate(times):
            for topic in (*arms, *grippers):
                positions = [0.2 + 0.01 * i] * 6 if topic in arms else [0.3]
                payload = classes["RobotState"](position=positions).SerializeToString()
                writer.add_message(channels[topic], int(timestamp), payload, int(timestamp))
            payload = classes["CompressedVideo"](data=b"synthetic", format="h264").SerializeToString()
            writer.add_message(channels["/top-camera"], int(timestamp), payload, int(timestamp))
        writer.finish()
    rows = [(topic, int(timestamp)) for topic in arms for timestamp in times]
    sidecar = directory / "eef_pose.parquet"
    pq.write_table(pa.table({
        "topic": [row[0] for row in rows], "timestamp_ns": [row[1] for row in rows],
        "pose": [np.eye(4).reshape(-1).tolist() for _ in rows],
    }), sidecar)
    original_sidecar = sidecar.read_bytes()
    config = Path(__file__).resolve().parents[1] / "configs" / "abc_130k.yaml"
    spec = load_spec(
        config, input_root=root, output_root=tmp_path / "reports", repo_root=upstream_repo,
        package_dir=upstream_repo / "datasets" / "abc_130k",
        quality_yaml=upstream_repo / "datasets" / "abc_130k" / "quality_stage_b.yaml",
    )
    cfg = quality_config(spec)
    ref = McapEpisodeRef(0, root, source, "data/train/pick/episode_000", "train", "pick")
    batch = McapReadOperator().run_batch([ref], spec, cfg, build_adapter(spec))
    episode = load_episode(batch.records[0])
    assert episode.state.shape == episode.action.shape == (20, 32)
    np.testing.assert_array_equal(episode.frame_times_ns, times)
    context = GlobalStatsOperator().run(batch, spec, cfg, recompute=True)
    assert context.status == "ready", context.error
    statistics = context.statistics
    detector = QualityDetectionOperator()
    decision = detector.run(episode, spec, cfg, statistics)
    assert decision.reject_file  # ABC Stage3 requires at least 120 frames.
    record = decision.to_record()
    assert record["effective_removed_ranges"][0]["end_frame_exclusive"] == 20
    judged = detector.run_batch(context, spec, cfg)
    result = McapDeliveryOperator().run(judged, mode="run")
    assert result.summary["deleted_files"] == 1
    assert not source.exists()
    assert sidecar.read_bytes() == original_sidecar
