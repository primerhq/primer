"""binding_epoch fences terminal writes (S1 P3 T20, amendment M15).

A switch bumps the epoch. A turn that started under the old one is
describing work for a binding the session has left, so its terminal
write must not land: it would clobber the switch that replaced it.
"""

from datetime import UTC, datetime

from primer.model.workspace_session import (
    AgentSessionBinding,
    SessionStatus,
    WorkspaceSession,
)
from primer.session.dispatch import _transition_session_status


def _row(**kw):
    base = {
        "id": "s", "workspace_id": "w",
        "binding": AgentSessionBinding(agent_id="a"),
        "status": SessionStatus.RUNNING,
        "created_at": datetime.now(UTC),
    }
    base.update(kw)
    return WorkspaceSession(**base)


class _Sessions:
    def __init__(self, row):
        self.row = row
        self.updates = 0

    async def get(self, _sid):
        return self.row

    async def update(self, row):
        self.updates += 1
        self.row = row
        return row


async def test_matching_epoch_writes_the_terminal_status():
    sessions = _Sessions(_row(binding_epoch=2))
    await _transition_session_status(
        sessions, sessions.row,
        new_status=SessionStatus.ENDED, ended_reason="completed",
        expected_epoch=2,
    )
    assert sessions.row.status is SessionStatus.ENDED
    assert sessions.updates == 1


async def test_stale_epoch_voids_the_terminal_write():
    """The switch already moved the row; this write would undo it."""
    sessions = _Sessions(_row(binding_epoch=3))
    stale = sessions.row.model_copy(update={"binding_epoch": 2})
    await _transition_session_status(
        sessions, stale,
        new_status=SessionStatus.ENDED, ended_reason="completed",
        expected_epoch=2,
    )
    assert sessions.row.status is SessionStatus.RUNNING  # untouched
    assert sessions.updates == 0


async def test_no_expected_epoch_keeps_the_unfenced_behaviour():
    """Callers that cannot be stale opt out by passing nothing."""
    sessions = _Sessions(_row(binding_epoch=9))
    await _transition_session_status(
        sessions, sessions.row, new_status=SessionStatus.WAITING,
    )
    assert sessions.row.status is SessionStatus.WAITING


async def test_fence_is_checked_before_the_no_op_shortcircuit():
    """A stale write must be voided even when it looks like a no-op,
    so the log records the void rather than silently agreeing."""
    sessions = _Sessions(_row(binding_epoch=5, status=SessionStatus.ENDED))
    await _transition_session_status(
        sessions, sessions.row,
        new_status=SessionStatus.ENDED, ended_reason="completed",
        expected_epoch=1,
    )
    assert sessions.updates == 0


class TestParkedStateCarriesTheEpoch:
    """The capture half of M15: without it the resume cannot compare."""

    def _parked(self, **kw):
        from primer.model.yield_ import Yielded
        from primer.worker.yield_runtime import ParkedState

        base = {
            "yielded": Yielded(tool_name="ask_user", event_key="k"),
            "llm_messages": [],
            "turn_no": 1,
            "started_at": datetime.now(UTC),
        }
        base.update(kw)
        return ParkedState(**base)

    def test_epoch_round_trips_through_json(self):
        from primer.worker.yield_runtime import ParkedState

        blob = self._parked(binding_epoch=4).to_jsonable()
        assert blob["binding_epoch"] == 4
        assert ParkedState.from_jsonable(blob).binding_epoch == 4

    def test_pre_s1_blobs_without_the_field_still_load(self):
        """Optional on purpose, so no schema-version bump is needed."""
        from primer.worker.yield_runtime import ParkedState

        blob = self._parked(binding_epoch=4).to_jsonable()
        del blob["binding_epoch"]
        assert ParkedState.from_jsonable(blob).binding_epoch is None

    def test_dispatch_captures_the_epoch_at_park(self):
        import inspect

        from primer.session.dispatch import run_one_session_turn

        body = inspect.getsource(run_one_session_turn)
        assert "binding_epoch=session.binding_epoch," in body

