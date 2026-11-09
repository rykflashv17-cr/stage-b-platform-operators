"""Single-episode boundaries are enforced before costly staging or publishing."""
from types import SimpleNamespace

import pytest

from mcap_read_normalize.io import read_raw_files, write_normalized_episode_ref
from mcap_read_normalize.operator import McapReadOperator


@pytest.mark.parametrize("source,keys", [
    ("jindian_marvin", ["a.mcap", "b.mcap"]),
    ("abc_130k", ["a/episode.mcap", "a/eef_pose.parquet", "b/episode.mcap", "b/eef_pose.parquet"]),
    ("abc_130k", ["a/episode.mcap", "b/eef_pose.parquet"]),
])
def test_multiple_or_unpaired_episodes_fail_before_materialization(tmp_path, source, keys):
    def unexpected(*args, **kwargs):
        pytest.fail("invalid cardinality must fail before binding or copying file bytes")

    ctx = SimpleNamespace(
        inputs=SimpleNamespace(value=lambda name: keys, files=unexpected, file_origins=unexpected),
        parameters={"source": source, "workers": 1},
        execution={"mode": "pipeline"}, check_cancelled=lambda: None,
        workspace=SimpleNamespace(path=unexpected),
    )
    with pytest.raises(ValueError):
        read_raw_files(ctx, tmp_path / "normalization")
    assert not list(tmp_path.iterdir())


def test_business_wrapper_does_not_accept_multiple_refs():
    with pytest.raises(ValueError, match="exactly one episode"):
        McapReadOperator().normalize_batch([object(), object()], object(), object())


@pytest.mark.parametrize("count", [0, 2])
def test_output_ref_requires_one_record_before_packing(count):
    def unexpected(*args, **kwargs):
        pytest.fail("invalid output cardinality must fail before workspace or file I/O")

    ctx = SimpleNamespace(workspace=SimpleNamespace(path=unexpected))
    batch = SimpleNamespace(records=tuple(object() for _ in range(count)))
    metadata = {"members": [object()] * count, "_identities": [object()] * count}
    with pytest.raises(ValueError, match="exactly one episode record"):
        write_normalized_episode_ref(ctx, batch, metadata)


def test_old_python_writer_name_is_only_an_alias_for_new_output():
    from mcap_read_normalize.io import write_normalized_batch_ref
    assert write_normalized_batch_ref is write_normalized_episode_ref
