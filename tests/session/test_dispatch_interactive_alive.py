"""Post-turn status: every clean agent turn ENDS the session.

The old "interactive sessions stay WAITING after a clean turn" downgrade is
gone (it hung one-shot callers forever). ``_post_turn_status`` now ends a
clean turn regardless of autonomy; a NEW message to an ENDED session reopens
it (``wake_session``'s ENDED branch). The executor-set WAITING (the
assistant-asked-a-question heuristic / ``max_tokens``) is a distinct,
legitimate wait and is preserved.
"""

from primer.model.workspace_session import SessionStatus
from primer.session.dispatch import _post_turn_status


def test_clean_stop_ends():
    status, reason = _post_turn_status("stop", None)
    assert status == SessionStatus.ENDED
    assert reason == "completed"


def test_default_ends():
    status, reason = _post_turn_status(None, None)
    assert status == SessionStatus.ENDED
    assert reason == "completed"


def test_error_ends():
    status, reason = _post_turn_status("error", None)
    assert status == SessionStatus.ENDED
    assert reason == "failed"


def test_tool_use_keeps_running():
    status, _ = _post_turn_status("tool_use", None)
    assert status == SessionStatus.RUNNING


def test_executor_set_waiting_is_preserved():
    # Assistant-asked-a-question heuristic: the executor set the AgentSession
    # to WAITING. That legitimate wait must survive (it is NOT the removed
    # clean-completion downgrade).
    status, reason = _post_turn_status("stop", SessionStatus.WAITING)
    assert status == SessionStatus.WAITING
    assert reason is None


def test_max_tokens_waits():
    status, _ = _post_turn_status("max_tokens", None)
    assert status == SessionStatus.WAITING
