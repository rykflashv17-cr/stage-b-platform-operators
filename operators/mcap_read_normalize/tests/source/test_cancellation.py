from types import SimpleNamespace
import pytest

def test_normalization_checks_cancellation_between_members(tmp_path):
    from mcap_read_normalize.core import McapReadOperator
    from pretrain_cleaning_common.contracts import McapEpisodeRef
    def cancelled():raise RuntimeError("cancel requested")
    ref=McapEpisodeRef(0,tmp_path,tmp_path / "unread.mcap","unread.mcap")
    spec=SimpleNamespace(output_root=tmp_path / "out")
    with pytest.raises(RuntimeError,match="cancel requested"):
        McapReadOperator().run_batch([ref],spec,None,adapter=object(),check_cancelled=cancelled)
