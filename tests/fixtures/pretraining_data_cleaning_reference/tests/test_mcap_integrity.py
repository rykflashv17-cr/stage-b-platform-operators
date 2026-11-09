"""Full-container admission, including chunks not used by quality arrays."""
import struct

import pytest
from mcap.opcode import Opcode
from mcap.writer import CompressionType, IndexType, Writer

from stage_b_mcap_v4.integrity import validate_mcap
from test_p0_input_contract import input_runtime
from test_serial_pipeline import numerical_engine, refs
from test_mcap_integration import write_marvin_mcap
from stage_b_mcap_v4 import operators
from stage_b_mcap_v4.delivery import McapDeliveryOperator


def records(data):
    position = 8
    while position < len(data) - 8:
        opcode, length = struct.unpack_from('<BQ', data, position)
        yield opcode, position, length
        position += 9 + length


def make_file(path, **options):
    with path.open('wb') as stream:
        writer = Writer(stream, **options)
        writer.start()
        robot = writer.register_channel('/robot', 'raw', 0)
        video = writer.register_channel('/video', 'raw', 0)
        writer.add_message(robot, 1, struct.pack('<d', 0.2), 1)
        writer.add_message(video, 2, b'opaque-video-content', 2)
        writer.finish()
    return path.read_bytes()


@pytest.mark.parametrize('options', [
    {},
    {'compression': CompressionType.NONE},
    {'index_types': IndexType.NONE},
    {'use_chunking': False, 'enable_data_crcs': True},
    {'enable_crcs': False, 'enable_data_crcs': False},
    {'use_chunking': False, 'index_types': IndexType.NONE,
     'repeat_channels': False, 'repeat_schemas': False, 'use_statistics': False,
     'use_summary_offsets': False},
])
def test_legal_container_variants(tmp_path, options):
    path = tmp_path / 'valid.mcap'
    original = make_file(path, **options)
    assert validate_mcap(path) is None
    assert path.read_bytes() == original


@pytest.mark.parametrize('options', [{}, {'index_types': IndexType.NONE}])
def test_crc_failure_detected_without_relying_on_indexes(tmp_path, options):
    path = tmp_path / 'bad.mcap'
    data = bytearray(make_file(path, **options))
    _, start, _ = next(record for record in records(data) if record[0] == Opcode.CHUNK)
    data[start + 9 + 24] ^= 1
    path.write_bytes(data)
    with pytest.raises(ValueError, match='crc|CRC'):
        validate_mcap(path)


def test_separate_video_only_chunk_is_validated(tmp_path):
    path = tmp_path / 'video.mcap'
    data = bytearray(make_file(path, compression=CompressionType.NONE, chunk_size=1))
    chunks = [record for record in records(data) if record[0] == Opcode.CHUNK]
    assert len(chunks) == 2
    _, second, length = chunks[1]
    assert b'opaque-video-content' in data[second:second + 9 + length]
    data[second + 9 + 24] ^= 1
    path.write_bytes(data)
    with pytest.raises(ValueError, match='crc|CRC'):
        validate_mcap(path)


def test_nonchunked_payload_is_checked_by_data_section_crc(tmp_path):
    path = tmp_path / 'data_crc.mcap'
    data = bytearray(make_file(path, use_chunking=False, enable_data_crcs=True))
    _, start, _ = next(record for record in records(data) if record[0] == Opcode.MESSAGE)
    struct.pack_into('<d', data, start + 9 + 22, 0.25)
    path.write_bytes(data)
    with pytest.raises(ValueError, match='data section CRC'):
        validate_mcap(path)


def test_attachment_crc_is_checked_even_without_data_section_crc(tmp_path):
    path = tmp_path / 'attachment.mcap'
    with path.open('wb') as stream:
        writer = Writer(stream, enable_data_crcs=False)
        writer.start()
        writer.add_attachment(0, 0, 'a', 'application/octet-stream', b'ATTACHMENT_BLOB')
        writer.finish()
    validate_mcap(path)
    data = bytearray(path.read_bytes())
    data[data.index(b'ATTACHMENT_BLOB')] ^= 1
    path.write_bytes(data)
    with pytest.raises(ValueError, match='attachment CRC'):
        validate_mcap(path)


def test_summary_crc_is_checked(tmp_path):
    path = tmp_path / 'summary.mcap'
    data = bytearray(make_file(path))
    _, start, _ = next(record for record in records(data) if record[0] == Opcode.FOOTER)
    data[start + 9 + 16] ^= 1
    path.write_bytes(data)
    with pytest.raises(ValueError, match='summary CRC'):
        validate_mcap(path)


@pytest.mark.parametrize('damage', ['leading_magic', 'trailing_magic', 'truncation', 'extra_bytes', 'record_length'])
def test_envelope_corruption_rejected(tmp_path, damage):
    path = tmp_path / 'malformed.mcap'
    data = bytearray(make_file(path))
    if damage == 'leading_magic':
        data[0] ^= 1
    elif damage == 'trailing_magic':
        data[-1] ^= 1
    elif damage == 'truncation':
        del data[-12:]
    elif damage == 'extra_bytes':
        data.extend(b'trailing junk')
    else:
        struct.pack_into('<Q', data, 9, 2**63)
    path.write_bytes(data)
    with pytest.raises(ValueError):
        validate_mcap(path)


def test_nested_field_cannot_read_into_next_record(tmp_path):
    path = tmp_path / 'field.mcap'
    data = bytearray(make_file(path, enable_crcs=False))
    # Header's profile string claims bytes outside its own record.
    _, _, header_length = next(records(data))
    struct.pack_into('<I', data, 17, header_length + 20)
    path.write_bytes(data)
    with pytest.raises(ValueError, match='record boundary'):
        validate_mcap(path)


def test_inner_record_cannot_read_past_chunk(tmp_path):
    path = tmp_path / 'inner.mcap'
    data = bytearray(make_file(path, compression=CompressionType.NONE, enable_crcs=False))
    _, start, length = next(record for record in records(data) if record[0] == Opcode.CHUNK)
    content = start + 9 + 40  # fixed Chunk fields with an empty compression string
    struct.pack_into('<Q', data, content + 1, length + 100)
    path.write_bytes(data)
    with pytest.raises(ValueError, match='chunk boundary'):
        validate_mcap(path)


@pytest.mark.parametrize('damage', ['crc', 'finite_payload'])
def test_b01_rejects_review_crc_cases_before_statistics_or_delivery(numerical_engine, monkeypatch, tmp_path, damage):
    import test_mcap_integration as fixture
    spec, cfg, adapter, _, _, _ = numerical_engine
    monkeypatch.setattr(fixture, 'Writer', lambda stream: Writer(stream, compression=CompressionType.NONE))
    path = tmp_path / 'corrupt.mcap'
    data = bytearray(write_marvin_mcap(path))
    _, start, length = next(record for record in records(data) if record[0] == Opcode.CHUNK)
    if damage == 'crc':
        data[start + 9 + 24] ^= 1
    else:
        where = data.find(struct.pack('<d', 0.2), start + 9 + 40, start + 9 + length)
        assert where >= 0
        struct.pack_into('<d', data, where, 0.25)
    path.write_bytes(data)
    monkeypatch.setattr(operators.GlobalStatsOperator, '_fit', lambda *a, **kw: pytest.fail('corrupt input must not fit'))
    batch = operators.McapReadOperator().run_batch(refs([path]), spec, cfg, adapter)
    assert batch.records[0].input_error and 'crc' in batch.records[0].input_error.lower()
    context = operators.GlobalStatsOperator().run(batch, spec, cfg)
    assert context.status == 'empty'
    judged = operators.QualityDetectionOperator().run_batch(context, spec, cfg)
    result = McapDeliveryOperator().run(judged, mode='run')
    assert result.exit_code == 1
    assert result.summary['failed_episodes'] == 1
    assert result.summary['exported_files'] == result.summary['deleted_files'] == 0
    assert path.read_bytes() == data


@pytest.mark.parametrize('codec', ['zstd', 'zstd_unknown_size', 'lz4'])
def test_actual_codec_expansion_cannot_exceed_declared_size(codec):
    from mcap.records import Chunk
    from stage_b_mcap_v4.integrity import _chunk_data
    raw = b'x' * 8192
    if codec.startswith('zstd'):
        import zstandard
        encoded = zstandard.ZstdCompressor(write_content_size=codec == 'zstd').compress(raw)
        compression = 'zstd'
    else:
        import lz4.frame
        encoded = lz4.frame.compress(raw)
        compression = 'lz4'
    chunk = Chunk(compression, encoded, 0, 0, 0, 1)
    with pytest.raises(Exception):
        _chunk_data(chunk)


@pytest.mark.parametrize('codec', ['zstd', 'zstd_unknown_size', 'lz4'])
@pytest.mark.parametrize('size', [0, 8192])
def test_exact_codec_expansion_is_accepted(codec, size):
    from mcap.records import Chunk
    from stage_b_mcap_v4.integrity import _chunk_data
    import zlib
    raw = b'x' * size
    if codec.startswith('zstd'):
        import zstandard
        encoded = zstandard.ZstdCompressor(write_content_size=codec == 'zstd', write_checksum=True).compress(raw)
        compression = 'zstd'
    else:
        import lz4.frame
        encoded = lz4.frame.compress(raw, content_checksum=True)
        compression = 'lz4'
    chunk = Chunk(compression, encoded, 0, 0, zlib.crc32(raw), size)
    stream, actual = _chunk_data(chunk)
    assert actual == size and stream.read(actual) == raw
    chunk.data = encoded[:-1]
    with pytest.raises(Exception):
        _chunk_data(chunk)


@pytest.mark.parametrize('codec', ['zstd', 'zstd_unknown_size', 'lz4'])
@pytest.mark.parametrize('size', [0, 8192])
def test_chunk_codec_rejects_concatenated_frames_and_trailing_junk(codec, size):
    from mcap.records import Chunk
    from stage_b_mcap_v4.integrity import _chunk_data
    raw = b'x' * size
    if codec.startswith('zstd'):
        import zstandard
        encoded = zstandard.ZstdCompressor(write_content_size=codec == 'zstd').compress(raw)
        compression = 'zstd'
    else:
        import lz4.frame
        encoded = lz4.frame.compress(raw)
        compression = 'lz4'
    for suffix in (encoded, b'trailing junk'):
        with pytest.raises(Exception):
            _chunk_data(Chunk(compression, encoded + suffix, 0, 0, 0, size))
