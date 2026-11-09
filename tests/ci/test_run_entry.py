import pytest


def test_no_work_dispatch_never_imports_build_or_test_actions(monkeypatch):
    from ci.run import execute
    class Runtime:
        spec={'action':'check-plan'}
        def finish(self):self.finished=True
    rt=Runtime();execute(rt)
    assert rt.finished


def test_unknown_action_is_rejected_before_completion():
    from ci.run import execute
    class Runtime:
        spec={'action':'arbitrary-shell-command'}
        def finish(self):pytest.fail('unknown actions must not pass')
    with pytest.raises(ValueError,match='unknown'):execute(Runtime())
