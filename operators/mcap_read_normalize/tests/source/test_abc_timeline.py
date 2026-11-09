from pathlib import Path
import numpy as np
import pytest
from tests.support.pretraining_samples import write_abc

@pytest.mark.parametrize("period,stride",[(33333333,1),(16666667,2)])
def test_abc_exact_source_timeline_follows_adapter_rows(tmp_path,period,stride):
    from pretraining_data_cleaning_common.handoff import make_spec
    from pretraining_data_cleaning_common.spec import quality_config
    from pretraining_data_cleaning_common.contracts import McapEpisodeRef
    from pretraining_data_cleaning_common.storage import load_episode
    from mcap_read_normalize.core import McapReadOperator
    from mcap_read_normalize.source_adapters import build_adapter
    root=tmp_path / "source"
    source,sidecar,times=write_abc(root,period=period,start=2**53+101)
    before=(source.read_bytes(),sidecar.read_bytes())
    spec=make_spec("abc_130k",root,tmp_path / "output",1)
    ref=McapEpisodeRef(0,root,source,"data/train/pick/episode_000","train","pick")
    batch=McapReadOperator().run_batch([ref],spec,quality_config(spec),build_adapter(spec))
    episode=load_episode(batch.records[0])
    np.testing.assert_array_equal(episode.frame_times_ns,times[::stride])
    assert episode.state.shape==episode.action.shape==(len(times[::stride]),32)
    assert (source.read_bytes(),sidecar.read_bytes())==before

def test_abc_timeline_is_filtered_with_episode_rows(tmp_path):
    from pretraining_data_cleaning_common.handoff import make_spec
    from mcap_read_normalize.source_adapters import build_adapter
    from robot_data_processing.normalize.standard_types import EpisodeRef
    root=tmp_path / "source";source,sidecar,times=write_abc(root)
    spec=make_spec("abc_130k",root,tmp_path / "output",1)
    adapter=build_adapter(spec)
    ref=EpisodeRef(0,root,"train","pick","data/train/pick/episode_000")
    raw=adapter.load_raw(ref)
    raw["episode_index"][3]=42
    standard=adapter.to_standard_episode(ref,raw,{"input_format":"pseudo_v3","identity_p1":True})
    timeline=adapter.to_quality_timeline(standard)
    np.testing.assert_array_equal(timeline["frame_times_ns"],np.delete(times,3))
    assert len(adapter.to_quality_arrays(standard)[0])==19
