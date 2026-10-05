"""The session adapter's release writes only the fields it owns.

``SessionClaimAdapter.on_release`` reads the row and then writes it back. It used to write the WHOLE document it read,
so anything a concurrent writer committed between that read and the write was put back: a ``wake_session`` steer's
``turn_status="claimable"`` and its ``last_seq`` regressed, and the next turn's writer, seeded from ``last_seq``,
reused a seq (a reused seq overwrites a message). Each branch is now one field-scoped ``patch_if`` of its own fields,
with the ``turn_no`` bump fenced on the value read.

The barrier storage below commits that concurrent write AFTER the adapter's read has returned its snapshot and before
the adapter writes, which is exactly the window. The reverse race (a stale whole-document writer that read the row
before the release committed and writes ``turn_no`` back afterwards) cannot be seen or stopped from inside the
release: that one is covered where it matters, by the completed-turn guard in ``run_one_session_turn``
(tests/worker/test_completed_turn_reclaim.py).
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from primer.claim.adapters.sessions import SessionClaimAdapter
from primer.int.claim import ParkRequest, ReleaseOutcome
from primer.model.except_ import ConflictError
from primer.model.workspace_session import AgentSessionBinding, SessionStatus, WorkspaceSession

from tests.conftest import _InMemoryStorage

pytestmark = pytest.mark.asyncio

SID = "s-owned"


class _BarrierStorage(_InMemoryStorage):
    """``get`` hands back the row as read, then commits ``between`` (a concurrent whole-document writer) before the
    caller gets to write. Only the first ``get`` after arming is intercepted."""

    def __init__(self, row: WorkspaceSession) -> None:
        super().__init__(WorkspaceSession)
        self._data[row.id] = row
        self.between = None

    async def get(self, id: str, *, conn=None):
        snapshot = await super().get(id, conn=conn)
        between, self.between = self.between, None
        if between is not None:
            current = self._data[id]
            await self.update(current.model_copy(update=between(current)))
        return snapshot

    def row(self) -> WorkspaceSession:
        return self._data[SID]


def _row(**fields) -> WorkspaceSession:
    base = dict(
        id=SID, workspace_id="w1", binding=AgentSessionBinding(agent_id="ag1"),
        status=SessionStatus.WAITING, created_at=datetime.now(UTC), turn_no=4, last_seq=10,
        next_unprocessed_seq=11, turn_status="idle",
    )
    base.update(fields)
    return WorkspaceSession(**base)


def _wake(row: WorkspaceSession) -> dict:
    """What ``wake_session`` commits for a steer: a USER_INPUT at last_seq + 1, claimable, RUNNING."""
    return {"turn_status": "claimable", "last_seq": row.last_seq + 1, "status": SessionStatus.RUNNING}


_PARK = ParkRequest(
    parked_state={"schema_version": 1, "yielded": {"tool_name": "ask_user"}},
    parked_event_key="ask_user:s-owned:tc-1",
    parked_until=datetime(2026, 10, 5, 12, 0, tzinfo=UTC),
    parked_at=datetime(2026, 10, 5, 11, 0, tzinfo=UTC),
)


@pytest.mark.parametrize("outcome, bumped", [
    (ReleaseOutcome(success=True, drop_lease=True), True),
    (ReleaseOutcome(success=False, drop_lease=True, last_error="boom"), False),
    (ReleaseOutcome(success=True, drop_lease=True, preserve_park=True), True),
    (ReleaseOutcome(success=True, drop_lease=True, park=_PARK), False),
], ids=["completed", "failed", "preserve_park", "park"])
async def test_a_wake_committed_between_the_read_and_the_write_survives_the_release(outcome, bumped):
    storage = _BarrierStorage(_row())
    storage.between = _wake
    await SessionClaimAdapter(session_storage=storage).on_release(None, SID, outcome=outcome)

    row = storage.row()
    assert row.turn_status == "claimable", "the steer's claimable was overwritten by the release's snapshot"
    assert row.last_seq == 11, "last_seq regressed: the next turn would reuse seq 11"
    assert row.status == SessionStatus.RUNNING
    assert row.turn_no == (5 if bumped else 4)
    assert row.last_worker_id is None
    if outcome.park is not None:
        assert row.parked_status == "parked"
        assert row.parked_event_key == "ask_user:s-owned:tc-1"
        assert row.parked_state == _PARK.parked_state
        assert row.parked_until == _PARK.parked_until and row.parked_at == _PARK.parked_at
    if bumped:
        assert row.last_turn_at is not None


async def test_the_release_never_writes_the_fields_other_writers_own():
    """Every field outside the release's own set, changed concurrently, is left as the other writer wrote it."""
    storage = _BarrierStorage(_row(parked_status="resumable", parked_event_key="k"))
    storage.between = lambda row: {
        "cancel_requested": True, "pause_requested": True, "interrupt_requested": True,
        "next_unprocessed_seq": 12, "name": "renamed", "turn_status": "running",
    }
    await SessionClaimAdapter(session_storage=storage).on_release(
        None, SID, outcome=ReleaseOutcome(success=True, drop_lease=True),
    )
    row = storage.row()
    assert (row.cancel_requested, row.pause_requested, row.interrupt_requested) == (True, True, True)
    assert row.next_unprocessed_seq == 12 and row.name == "renamed" and row.turn_status == "running"
    assert row.turn_no == 5
    assert row.parked_status is None and row.parked_event_key is None   # the release's own fields


async def test_the_turn_no_bump_is_fenced_on_the_value_read_and_applied_once():
    """A concurrent ``turn_no`` change rejects the fenced bump; the release re-reads and bumps the CURRENT value once."""
    storage = _BarrierStorage(_row())
    storage.between = lambda row: {"turn_no": 7}
    await SessionClaimAdapter(session_storage=storage).on_release(
        None, SID, outcome=ReleaseOutcome(success=True, drop_lease=True),
    )
    assert storage.row().turn_no == 8


async def test_a_fence_rejected_twice_fails_the_release_instead_of_bumping_a_stale_value():
    storage = _BarrierStorage(_row())
    storage.between = lambda row: {"turn_no": 7}
    real_get = storage.get

    async def get(id, *, conn=None):
        row = await real_get(id, conn=conn)
        storage.between = lambda current: {"turn_no": current.turn_no + 1}   # it moves again before every write
        return row

    storage.get = get  # type: ignore[method-assign]
    with pytest.raises(ConflictError):
        await SessionClaimAdapter(session_storage=storage).on_release(
            None, SID, outcome=ReleaseOutcome(success=True, drop_lease=True),
        )


async def test_a_row_deleted_under_the_release_is_left_alone():
    storage = _BarrierStorage(_row())
    real_get = storage.get

    async def get(id, *, conn=None):
        row = await real_get(id, conn=conn)
        await storage.delete(id)
        return row

    storage.get = get  # type: ignore[method-assign]
    await SessionClaimAdapter(session_storage=storage).on_release(
        None, SID, outcome=ReleaseOutcome(success=False, drop_lease=True, last_error="boom"),
    )
    assert await real_get(SID) is None
