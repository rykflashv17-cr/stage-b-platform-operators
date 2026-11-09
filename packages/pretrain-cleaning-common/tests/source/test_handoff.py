import numpy as np
import pytest


def test_fixed_profiles_and_stable_multi_directory_discovery(tmp_path):
    from pretrain_cleaning_common.handoff import discover_inputs, make_spec
    first, second = tmp_path / "b", tmp_path / "a"
    for directory in (first, second):
        directory.mkdir()
        (directory / "x.mcap").write_bytes(b"discovery only")
    spec, refs, selection = discover_inputs([first, second], "jindian_marvin", tmp_path / "output", 1)
    assert [r.mcap_path for r in refs] == [first / "x.mcap", second / "x.mcap"]
    assert [r.episode_index for r in refs] == [0, 1]
    assert [r.dataset_root for r in refs] == [first, second]
    assert len(selection["members"]) == 2
    assert all(m["source_episode_index"] == 0 for m in selection["members"])
    with pytest.raises(ValueError):
        make_spec("../untrusted", first, tmp_path / "other", 1)
    with pytest.raises(ValueError):
        discover_inputs([first, first], "jindian_marvin", tmp_path / "duplicate", 1)


def normalized(tmp_path):
    from pretrain_cleaning_common.contracts import McapEpisodeRef, NormalizedEpisode, NormalizedRecord, NormalizedBatchRef
    from pretrain_cleaning_common.storage import save_batch, save_episode
    folder=tmp_path / "normalized"; folder.mkdir()
    source=tmp_path / "input.mcap"; source.write_bytes(b"unchanged")
    ref=McapEpisodeRef(7,tmp_path,source,"input.mcap")
    path=folder / "episode_00000000.npz"
    times=np.array([2**53+1,2**53+101],dtype=np.int64)
    values=np.full((2,16),0.2); values[0,14]=np.nan
    save_episode(path,NormalizedEpisode(ref,values,values,times))
    batch=NormalizedBatchRef(folder / "manifest.json",(NormalizedRecord(ref,path),))
    save_batch(batch)
    return batch


@pytest.mark.parametrize("lag", [-2, 1])
def test_statistics_handoff_restores_exact_types_without_refit(tmp_path,monkeypatch,lag):
    from pretrain_cleaning_common.handoff import save_context, load_context, selection_metadata
    from pretrain_cleaning_common.contracts import StatisticsBundle, StatisticsContext
    from pretrain_cleaning_common.storage import load_episode
    from robot_data_processing.types import GlobalStats
    from robot_data_processing.stage1_stats import Stage1GlobalStats
    from robot_data_processing.stages.state_action_temporal_alignment import StateActionAlignStats
    batch=normalized(tmp_path)
    cache=tmp_path / "cache";cache.mkdir()
    global_stats=GlobalStats(*[np.arange(16,dtype=np.float64) for _ in range(8)],2,1)
    stage1=Stage1GlobalStats(*[np.arange(16,dtype=np.float64) for _ in range(3)],2,1,16)
    align=StateActionAlignStats(1,0,2,*[np.arange(14,dtype=np.int64) for _ in range(3)],1)
    bundle=StatisticsBundle(global_stats,stage1,align,lag,cache)
    context=StatisticsContext(batch,"ready",bundle,({"episode_index":7,"fit_status":"included","reason":None},),None)
    metadata=selection_metadata("jindian_marvin",[tmp_path],batch.records)
    destination=tmp_path / "handoff"
    save_context(context,destination,metadata)
    import robot_data_processing.stats as stats
    monkeypatch.setattr(stats,"load_or_compute_stats",lambda *a,**k: pytest.fail("restoration must not fit"))
    restored, meta=load_context(destination,"jindian_marvin")
    assert restored.statistics.alignment_lag == lag
    assert type(restored.statistics.global_stats) is GlobalStats
    assert type(restored.statistics.stage1_stats) is Stage1GlobalStats
    assert type(restored.statistics.alignment_stats) is StateActionAlignStats
    np.testing.assert_array_equal(restored.statistics.global_stats.state_q99,global_stats.state_q99)
    episode=load_episode(restored.batch.records[0])
    assert episode.frame_times_ns.dtype == np.int64
    assert episode.frame_times_ns[0] == 2**53+1
    assert np.isnan(episode.state[0,14])
    assert restored.fit_records == context.fit_records
    assert restored.batch.records[0].ref == batch.records[0].ref
    with pytest.raises(ValueError,match="source|profile"):
        load_context(destination,"abc_130k")
    target=destination / "cache/global_stats.npz"
    target.write_bytes(target.read_bytes()+b"tampered")
    with pytest.raises(ValueError,match="digest|checksum"):
        load_context(destination,"jindian_marvin")


@pytest.mark.parametrize("status",["empty","failed"])
def test_nonready_context_retains_every_error_and_policy(tmp_path,status):
    from pretrain_cleaning_common.handoff import save_context,load_context,selection_metadata
    from pretrain_cleaning_common.contracts import StatisticsContext
    batch=normalized(tmp_path)
    ctx=StatisticsContext(batch,status,None,({"episode_index":7,"fit_status":"excluded","reason":"input"},),("left","right",600000000),"failed" if status=="failed" else None)
    directory=tmp_path / "handoff"
    save_context(ctx,directory,selection_metadata("jindian_marvin",[tmp_path],batch.records))
    restored,_=load_context(directory,"jindian_marvin")
    assert restored.status == status and restored.statistics is None
    assert restored.error == ctx.error and restored.coverage_policy == ctx.coverage_policy
    assert len(restored.batch.records) == 1
