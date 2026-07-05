from primer.model.workspace_session import SessionStatus
from primer.session.dispatch import _post_turn_status


def test_interactive_clean_stop_stays_waiting():
    status, reason = _post_turn_status("stop", None, is_autonomous=False)
    assert status == SessionStatus.WAITING
    assert reason is None


def test_interactive_default_stays_waiting():
    status, reason = _post_turn_status(None, None, is_autonomous=False)
    assert status == SessionStatus.WAITING


def test_autonomous_clean_stop_ends():
    status, reason = _post_turn_status("stop", None, is_autonomous=True)
    assert status == SessionStatus.ENDED
    assert reason == "completed"


def test_error_ends_for_both():
    for auto in (True, False):
        status, reason = _post_turn_status("error", None, is_autonomous=auto)
        assert status == SessionStatus.ENDED
        assert reason == "failed"


def test_tool_use_keeps_running_for_both():
    for auto in (True, False):
        status, _ = _post_turn_status("tool_use", None, is_autonomous=auto)
        assert status == SessionStatus.RUNNING
