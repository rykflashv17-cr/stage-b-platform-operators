from types import SimpleNamespace
import pytest

def test_statistics_checks_cancellation_outside_business_error_capture(tmp_path):
    from mcap_stats_global.core import GlobalStatsOperator
    def cancelled():raise RuntimeError("cancel requested")
    with pytest.raises(RuntimeError,match="cancel requested"):
        GlobalStatsOperator().run(SimpleNamespace(records=[object()]),None,None,check_cancelled=cancelled)
