"""Post-turn status: every clean agent turn ENDS the session.

The old "interactive sessions stay WAITING after a clean turn" downgrade is
gone (it hung one-shot callers forever). ``_post_turn_status`` now ends a
clean turn regardless of autonomy; a NEW message to an ENDED session reopens
it (``wake_session``'s ENDED branch). The executor-set WAITING (the
assistant-asked-a-question heuristic / ``max_tokens``) is a distinct,
legitimate wait and is preserved.
"""

from primer.model.workspace_session import SessionStatus
from primer.session import dispatch
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


class TestCleanTurnRestsParkedFlag:
    """01a04d91-a7a0 PHASE 1 item 3: the flag-gated seam for "clean stop
    rests the session PARKED (resumable) instead of ENDED". PENDING USER
    CONFIRMATION - _CLEAN_TURN_RESTS_PARKED must default False (every
    test above this class runs with the real module default and asserts
    the unchanged ENDED behavior); these tests exercise ONLY the
    opt-in branch, monkeypatched on, to prove the seam itself works
    without touching the default any test above relies on."""

    def test_flag_defaults_false(self):
        assert dispatch._CLEAN_TURN_RESTS_PARKED is False

    def test_flag_on_clean_stop_waits_instead_of_ends(self, monkeypatch):
        monkeypatch.setattr(dispatch, "_CLEAN_TURN_RESTS_PARKED", True)
        for reason in ("stop", "end_turn", "stop_sequence"):
            status, ended_reason = _post_turn_status(reason, None)
            assert status == SessionStatus.WAITING, reason
            assert ended_reason is None, reason

    def test_flag_on_does_not_touch_other_reasons(self, monkeypatch):
        monkeypatch.setattr(dispatch, "_CLEAN_TURN_RESTS_PARKED", True)
        status, reason = _post_turn_status("error", None)
        assert status == SessionStatus.ENDED
        assert reason == "failed"
        status, _ = _post_turn_status("tool_use", None)
        assert status == SessionStatus.RUNNING
        status, _ = _post_turn_status("max_tokens", None)
        assert status == SessionStatus.WAITING

    def test_flag_on_executor_set_ended_still_wins(self, monkeypatch):
        # A definitive internal-error ENDED from the executor must not be
        # downgraded to WAITING just because the flag is on and the last
        # stop reason happens to be "stop" - the flag only changes the
        # PLAIN clean-stop case, per _post_turn_status's own docstring.
        monkeypatch.setattr(dispatch, "_CLEAN_TURN_RESTS_PARKED", True)
        status, reason = _post_turn_status("stop", SessionStatus.ENDED)
        assert status == SessionStatus.ENDED
        assert reason == "completed"
