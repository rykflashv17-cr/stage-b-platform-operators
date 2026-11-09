from pathlib import Path
import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq
from google.protobuf import descriptor_pb2,descriptor_pool,message_factory
from mcap.writer import Writer
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


def write_abc(root, *, frames=20, period=33333333, start=1000000000):
    directory = root / "data" / "train" / "pick" / "episode_000"
    directory.mkdir(parents=True)
    source = directory / "episode.mcap"
    classes, descriptor = _proto_types()
    arms = ("/left-arm-state", "/right-arm-state", "/left-arm-action", "/right-arm-action")
    grippers = ("/left-ee-state", "/right-ee-state", "/left-ee-action", "/right-ee-action")
    times = np.arange(frames, dtype=np.int64) * period + start
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
    return source,sidecar,times
