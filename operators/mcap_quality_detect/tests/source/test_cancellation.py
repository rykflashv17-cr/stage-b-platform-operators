from types import SimpleNamespace
import pytest

def test_quality_checks_cancellation_outside_per_record_diagnostics(tmp_path):
    from mcap_quality_detect.core import QualityDetectionOperator
    def cancelled():raise RuntimeError("cancel requested")
    context=SimpleNamespace(batch=SimpleNamespace(records=[object()]),coverage_policy=None)
    with pytest.raises(RuntimeError,match="cancel requested"):
        QualityDetectionOperator().run_batch(context,SimpleNamespace(gripper_gap=None),None,check_cancelled=cancelled)
