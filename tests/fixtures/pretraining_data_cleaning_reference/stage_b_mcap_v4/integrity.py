"""Sequential MCAP structure/CRC admission; no topic filtering or pixel decoding.

The seek-based reader can skip chunks, and StreamReader does not check attachment
or summary CRCs. Scan record framing once, reuse the MCAP record parsers and chunk
codec, and check every checksum that is present. Memory is bounded by one record
or decompressed chunk (plus the small schema/channel registry), not the file.
"""
from __future__ import annotations

from io import BytesIO
import os
from pathlib import Path
import struct
import zlib

from mcap.data_stream import ReadDataStream
from mcap.opcode import Opcode
from mcap.records import (
    AttachmentIndex, Channel, Chunk, ChunkIndex, DataEnd, Footer, Header,
    MessageIndex, Metadata, MetadataIndex, Schema, Statistics, SummaryOffset,
)


_MAGIC = b"\x89MCAP0\r\n"
_BLOCK = 1024 * 1024
# Same default record ceiling as mcap.StreamReader; also bound declared expansion.
_LIMIT = 4 * 2**30
_PARSERS = {
    Opcode.ATTACHMENT_INDEX: AttachmentIndex, Opcode.CHANNEL: Channel,
    Opcode.CHUNK: Chunk, Opcode.CHUNK_INDEX: ChunkIndex, Opcode.DATA_END: DataEnd,
    Opcode.FOOTER: Footer, Opcode.HEADER: Header, Opcode.MESSAGE_INDEX: MessageIndex,
    Opcode.METADATA: Metadata, Opcode.METADATA_INDEX: MetadataIndex,
    Opcode.SCHEMA: Schema, Opcode.STATISTICS: Statistics, Opcode.SUMMARY_OFFSET: SummaryOffset,
}


def _exact(stream, size):
    if size < 0:
        raise ValueError("MCAP contains a negative field length")
    data = stream.read(size) if size else b""
    if len(data) != size:
        raise ValueError("truncated MCAP record")
    return data


class _Body:
    """A declared record boundary also bounds every nested field allocation."""
    def __init__(self, stream, size, update=None):
        self.stream, self.remaining, self.update = stream, size, update
        self.crc = 0

    def read(self, size):
        if size < 0 or size > self.remaining:
            raise ValueError("MCAP field exceeds its record boundary")
        data = _exact(self.stream, size)
        self.remaining -= size
        self.crc = zlib.crc32(data, self.crc)
        if self.update is not None:
            self.update(data)
        return data

    def drain(self, size=None):
        left = self.remaining if size is None else size
        if left < 0 or left > self.remaining:
            raise ValueError("MCAP field exceeds its record boundary")
        while left:
            amount = min(left, _BLOCK)
            self.read(amount)
            left -= amount


def _crc(expected, actual, label):
    # MCAP uses zero to mean the optional checksum was not supplied.
    if expected and expected != actual:
        raise ValueError(f"MCAP {label} CRC mismatch: expected {expected}, calculated {actual}")


def _chunk_data(chunk):
    """Bound actual codec output; a size argument alone does not bound zstd."""
    expected = chunk.uncompressed_size
    if expected > _LIMIT:
        raise ValueError("MCAP uncompressed chunk exceeds validation size limit")
    if chunk.compression == "":
        data = chunk.data
    elif chunk.compression == "zstd":
        import zstandard
        frame_size = zstandard.frame_content_size(chunk.data)
        # zstd ignores max_output_size when its frame carries a content size.
        # Reject a contradictory frame size before allocating the output.
        if frame_size >= 0 and frame_size != expected:
            raise ValueError("MCAP zstd frame size disagrees with chunk size")
        if expected == 0:
            # The one-shot API can return immediately for a known empty frame
            # without checking its trailer. Feed at most one byte at a time;
            # reject immediately if it produces any unexpected payload.
            decoder = zstandard.ZstdDecompressor().decompressobj()
            for position in range(len(chunk.data)):
                if decoder.decompress(chunk.data[position:position + 1]):
                    raise ValueError("MCAP zstd expansion exceeds declared empty chunk")
                if decoder.eof:
                    if position != len(chunk.data) - 1 or decoder.unused_data:
                        raise ValueError("MCAP zstd chunk contains trailing data")
                    break
            if not decoder.eof:
                raise ValueError("MCAP zstd chunk is truncated")
            data = b""
        else:
            data = zstandard.ZstdDecompressor().decompress(
                chunk.data, max_output_size=expected, allow_extra_data=False,
            )
    elif chunk.compression == "lz4":
        from lz4.frame import LZ4FrameDecompressor
        decoder = LZ4FrameDecompressor()
        data = decoder.decompress(chunk.data, max_length=expected + 1)
        if len(data) > expected:
            raise ValueError("MCAP lz4 expansion exceeds declared chunk size")
        if not decoder.eof or decoder.unused_data:
            raise ValueError("MCAP lz4 chunk is truncated or contains trailing data")
    else:
        raise ValueError(f"unsupported MCAP compression: {chunk.compression}")
    if len(data) != expected:
        raise ValueError("MCAP chunk uncompressed size mismatch")
    _crc(chunk.uncompressed_crc, zlib.crc32(data), "chunk")
    return ReadDataStream(BytesIO(data)), len(data)


def _record(opcode, body, channels, schemas):
    stream = ReadDataStream(body)
    if opcode == Opcode.MESSAGE:
        channel = stream.read2()
        if channel not in channels or (channels[channel] and channels[channel] not in schemas):
            raise ValueError("MCAP message references an undefined channel or schema")
        _exact(stream, 20)  # sequence, log_time, publish_time; payload is opaque here.
        body.drain()
        return None
    if opcode == Opcode.ATTACHMENT:
        _exact(stream, 16)  # log_time, create_time
        stream.read_prefixed_string()
        stream.read_prefixed_string()
        size = stream.read8()
        if size != body.remaining - 4:
            raise ValueError("MCAP attachment length does not match record")
        body.drain(size)
        actual = body.crc
        _crc(stream.read4(), actual, "attachment")
        return None
    parser = _PARSERS.get(opcode)
    record = parser.read(stream) if parser else None
    if isinstance(record, Schema):
        schemas.add(record.id)
    elif isinstance(record, Channel):
        channels[record.id] = record.schema_id
    elif isinstance(record, Chunk):
        expanded, length = _chunk_data(record)
        while expanded.count < length:
            header = _exact(expanded, 9)
            inner_opcode, size = struct.unpack("<BQ", header)
            if size > length - expanded.count:
                raise ValueError("MCAP inner record exceeds chunk boundary")
            if inner_opcode in set(Opcode) - {Opcode.SCHEMA, Opcode.CHANNEL, Opcode.MESSAGE}:
                raise ValueError("MCAP chunk contains a forbidden record type")
            inner = _Body(expanded, size)
            _record(inner_opcode, inner, channels, schemas)
            inner.drain()
    body.drain()  # Preserve MCAP's allowance for unknown fields/record types.
    return record


def validate_mcap(path: Path) -> None:
    """Reject malformed files or supplied CRC mismatches before normalization.

    Valid files without optional checksums are allowed. This checks container
    integrity, not video content, robot kinematics, or whether optional indexes
    accurately summarize every message.
    """
    channels, schemas = {}, set()
    with Path(path).open("rb") as raw:
        file_size = os.fstat(raw.fileno()).st_size
        if _exact(raw, 8) != _MAGIC:
            raise ValueError("invalid MCAP leading magic")
        data_crc, summary_crc = zlib.crc32(_MAGIC), 0
        data_ended, first_record = False, True
        summary_start, first_offset = None, None

        def update(data):
            nonlocal data_crc, summary_crc
            if data_ended:
                summary_crc = zlib.crc32(data, summary_crc)
            else:
                data_crc = zlib.crc32(data, data_crc)

        while True:
            position = raw.tell()
            header = _exact(raw, 9)
            opcode, size = struct.unpack("<BQ", header)
            if size > _LIMIT or size > file_size - raw.tell() - 8:
                raise ValueError("MCAP record length exceeds file or validation limit")
            if first_record != (opcode == Opcode.HEADER):
                raise ValueError("MCAP must contain exactly one leading Header")
            first_record = False
            if opcode == Opcode.DATA_END:
                if data_ended or size != 4:
                    raise ValueError("invalid or duplicate MCAP DataEnd")
                body = _Body(raw, size)
                record = _record(opcode, body, channels, schemas)
                _crc(record.data_section_crc, data_crc, "data section")
                data_ended, summary_start = True, raw.tell()
            elif opcode == Opcode.FOOTER:
                if not data_ended or size != 20:
                    raise ValueError("MCAP Footer requires a preceding DataEnd")
                record = _record(opcode, _Body(raw, size), channels, schemas)
                expected_start = summary_start if position > summary_start else 0
                if record.summary_start != expected_start:
                    raise ValueError("MCAP footer summary offset does not match structure")
                if first_offset is not None:
                    if record.summary_offset_start != first_offset:
                        raise ValueError("MCAP footer summary-offset section is misplaced")
                elif record.summary_offset_start not in {0, position}:
                    raise ValueError("MCAP footer points to missing summary-offset section")
                footer_prefix = header + struct.pack("<QQ", record.summary_start, record.summary_offset_start)
                _crc(record.summary_crc, zlib.crc32(footer_prefix, summary_crc), "summary")
                if _exact(raw, 8) != _MAGIC or raw.read(1):
                    raise ValueError("invalid MCAP trailing magic or trailing bytes")
                return
            else:
                if data_ended and opcode in {Opcode.MESSAGE, Opcode.CHUNK, Opcode.ATTACHMENT, Opcode.METADATA}:
                    raise ValueError("MCAP data record appears after DataEnd")
                if opcode == Opcode.SUMMARY_OFFSET:
                    if not data_ended:
                        raise ValueError("MCAP SummaryOffset appears before DataEnd")
                    if first_offset is None:
                        first_offset = position
                update(header)
                _record(opcode, _Body(raw, size, update), channels, schemas)
