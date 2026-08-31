"""Post-turn status: a clean agent turn rests the session PARKED.

01a0518a: ``_CLEAN_TURN_RESTS_PARKED`` now defaults True - a clean
stop/end_turn/stop_sequence leaves the session WAITING (served as
session_state="parked", see ``WorkspaceSession.session_state``) rather
than ENDING it, matching how a yielding-tool park already behaves. A NEW
message to a resting session resumes it in place (``wake_session``'s
``_RESUMABLE`` set already includes WAITING); a genuinely ENDED session
still reopens via ``wake_session``'s ENDED branch. The executor-set
WAITING (the assistant-asked-a-question heuristic / ``max_tokens``) is a
distinct, legitimate wait and is preserved - both read the same either
way. The flag stays in place as a test seam (see ``TestCleanTurnEndsWhenFlagOff``
below for the pre-flip behavior).
"""

from primer.model.workspace_session import SessionStatus
from primer.session import dispatch
from primer.session.dispatch import _post_turn_status


def test_clean_stop_parks():
    for reason in ("stop", "end_turn", "stop_sequence"):
        status, ended_reason = _post_turn_status(reason, None)
        assert status == SessionStatus.WAITING, reason
        assert ended_reason is None, reason


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
    """01a0518a: USER-CONFIRMED, _CLEAN_TURN_RESTS_PARKED now defaults
    True (every test above this class runs with the real module default
    and asserts the parked behavior). These tests pin the flag's own
    default and re-confirm the opt-in branch's edges explicitly (not
    just via the ambient default), so a future change to the default
    doesn't silently drop coverage of the branch itself."""

    def test_flag_defaults_true(self):
        assert dispatch._CLEAN_TURN_RESTS_PARKED is True

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


class TestCleanTurnEndsWhenFlagOff:
    """The pre-01a0518a behavior, preserved as an explicit opt-out seam:
    with the flag monkeypatched False, a clean stop ENDS the session
    again (the "one-shot caller never hangs" shape). Proves the seam
    still works in both directions, not just the now-default one."""

    def test_flag_off_clean_stop_ends(self, monkeypatch):
        monkeypatch.setattr(dispatch, "_CLEAN_TURN_RESTS_PARKED", False)
        for reason in ("stop", "end_turn", "stop_sequence"):
            status, ended_reason = _post_turn_status(reason, None)
            assert status == SessionStatus.ENDED, reason
            assert ended_reason == "completed", reason


class TestAutonomousSessionsStillEnd:
    """01a0518a follow-up (edge #3 sweep finding): an autonomous session
    (a graph, or an agent explicitly marked autonomous=True - trigger/
    webhook-fired one-shot sessions, see agent_fresh_session.py) has no
    interactive human to resume it, so it must still END on a clean turn
    even with the parked flip on. Without this exemption, a
    parallelism="skip" trigger subscription's skip-gate (keyed on "any
    non-ENDED session with turn_no > 0") wedges permanently closed after
    its first successful fire - reproducing, at unbounded scale, the
    documented 14h-stuck-cron incident StuckSessionSweeper's own
    docstring describes."""

    def test_autonomous_clean_stop_still_ends(self):
        for reason in ("stop", "end_turn", "stop_sequence"):
            status, ended_reason = _post_turn_status(
                reason, None, autonomous=True,
            )
            assert status == SessionStatus.ENDED, reason
            assert ended_reason == "completed", reason

    def test_interactive_default_still_parks(self):
        # autonomous defaults False - every call site above this class
        # (and the real dispatch.py call site for an ordinary agent
        # session) relies on this default, not an explicit kwarg.
        status, ended_reason = _post_turn_status("stop", None)
        assert status == SessionStatus.WAITING
        assert ended_reason is None

    def test_autonomous_does_not_override_an_executor_ended(self):
        status, reason = _post_turn_status(
            "stop", SessionStatus.ENDED, autonomous=True,
        )
        assert status == SessionStatus.ENDED
        assert reason == "completed"

    def test_autonomous_does_not_touch_other_reasons(self):
        status, reason = _post_turn_status("error", None, autonomous=True)
        assert status == SessionStatus.ENDED
        assert reason == "failed"
        status, _ = _post_turn_status("tool_use", None, autonomous=True)
        assert status == SessionStatus.RUNNING
        status, _ = _post_turn_status("max_tokens", None, autonomous=True)
        assert status == SessionStatus.WAITING
