"""A steer queued on a session is realized when the engine path ENDS that session (ticket 01a1120e).

``route_steer`` counts a PARKED session as busy, so a steer sent to it is stored as a ``PendingSessionMessage`` (no
USER_INPUT, no seq) and realized only by a terminal checkpoint. Every terminal exit of ``dispatch.py`` (a failed turn, a
completed turn, ``_land_cancelled_turn``) applies a queued switch and then realizes ONE queued steer. The pool's
``_end_session`` (a resume that failed, a cancelled park, a finished graph resume) has no turn behind it and so no
checkpoint: the session ended, the user's message stayed queued, and it ran only after some LATER message reopened the
session (after the newer one), or never. Lead's ruling (2026-10-07): realize on failed, completed AND cancelled, like
dispatch's exits (a steer sent after a cancel is a deliberate new instruction).

Driven through the REAL ``WorkerPool`` (``_end_session`` and the real resume handler's failure exit), the in-memory claim
engine and scheduler, and the real ``wake_session`` that realizes the steer (``tests/worker/test_completed_turn_reclaim.py``'s
world; only the model is scripted).
"""

from __future__ import annotations

import pytest

from primer.int.claim import ClaimKind
from primer.model.workspace_session import (
    PendingSessionMessage,
    SessionMessageKind,
    SessionStatus,
    WorkspaceSession,
)
from primer.model.storage import FieldRef, OffsetPage, Op, Predicate, Value
from primer.session.pending_messages import store_pending_steer

from tests.worker.test_completed_turn_reclaim import KEY, SID, _World, world  # noqa: F401

STEER = "a steer queued on the parked session"


async def _queue(world: _World, text: str = STEER) -> None:
    row = await world.row()
    await store_pending_steer(
        storage_provider=world.storage, session=row, text=text, workspace_registry=world.registry, event_bus=world.bus,
    )


async def _pending(world: _World) -> list[str]:
    page = await world.storage.get_storage(PendingSessionMessage).find(
        Predicate(left=FieldRef(name="session_id"), op=Op.EQ, right=Value(value=SID)),
        OffsetPage(offset=0, length=10),
        order_by=None,
    )
    return [r.parts[0]["text"] for r in page.items]


def _user_inputs(world: _World) -> list[str]:
    return [
        r["payload"].get("text") for r in world.ws.records() if r.get("kind") == SessionMessageKind.USER_INPUT.value
    ]


async def _park_resumable_with_an_unreadable_blob(world: _World) -> None:
    """A session parked on a human gate whose parked_state cannot be read: its resume takes the fail-closed exit
    (``_end_session(failed)``), the shape of every engine-path end the issue is about."""
    await world.create_session()
    row = await world.row()
    await world.sessions.update(row.model_copy(update={
        "status": SessionStatus.RUNNING, "parked_status": "resumable", "parked_event_key": f"ask_user:{SID}:tc-1",
        "parked_state": {"garbage": True},
    }))


@pytest.mark.asyncio
@pytest.mark.parametrize("reason", ["failed", "completed", "cancelled"])
async def test_ending_a_session_realizes_the_steer_queued_on_it(world, reason):
    await world.create_session()
    await _queue(world)
    pool = world.pool("wrk-a")

    await pool._end_session(await world.row(), reason=reason)

    assert await _pending(world) == [], f"the steer is still queued after the session ended {reason}"
    assert _user_inputs(world) == [STEER], "the queued message was not written as the user's input"
    row = await world.row()
    assert row.status is not SessionStatus.ENDED and row.turn_status == "claimable", (
        f"the realized steer did not arm a turn: {row.status} {row.turn_status}"
    )
    assert await world.engine.has_lease(*KEY), "the realized steer left no claimable lease"


@pytest.mark.asyncio
async def test_exactly_one_queued_steer_is_realized_per_end(world):
    """The 1:1 user_input-to-terminal pairing the drain counts: the rest follow at later checkpoints."""
    await world.create_session()
    await _queue(world, "first")
    await _queue(world, "second")

    await world.pool("wrk-a")._end_session(await world.row(), reason="failed")

    assert _user_inputs(world) == ["first"]
    assert await _pending(world) == ["second"]


@pytest.mark.asyncio
async def test_an_end_with_nothing_queued_writes_nothing_and_leaves_the_session_ended(world):
    await world.create_session()

    await world.pool("wrk-a")._end_session(await world.row(), reason="failed")

    row = await world.row()
    assert (row.status, row.ended_reason) == (SessionStatus.ENDED, "failed")
    assert _user_inputs(world) == []
    assert not await world.engine.has_lease(*KEY)


@pytest.mark.asyncio
async def test_a_realize_that_fails_does_not_fail_the_end(world, monkeypatch):
    """Best effort, like every checkpoint: the end is the row's truth and must land; the steer stays queued."""
    import primer.session.dispatch as dispatch

    async def boom(**kwargs):
        raise OSError("the workspace volume is not answering")

    monkeypatch.setattr(dispatch, "realize_next_pending", boom)
    await world.create_session()
    await _queue(world)

    outcome = await world.pool("wrk-a")._end_session(await world.row(), reason="failed")

    assert outcome.success is True and outcome.drop_lease is True
    row = await world.row()
    assert (row.status, row.ended_reason) == (SessionStatus.ENDED, "failed")
    assert await _pending(world) == [STEER]


@pytest.mark.asyncio
async def test_a_failed_resume_answers_the_message_queued_on_the_parked_session(world):
    """The whole chain through the real pool: the parked session's resume fails closed, the session ends, the queued
    message is realized, the claim is re-armed after the release, and the NEXT claim runs the turn that answers it."""
    await _park_resumable_with_an_unreadable_blob(world)
    await _queue(world)
    await world.engine.mark_resumable(ClaimKind.SESSION, SID)
    pool = world.pool("wrk-a")

    assert await world.claim_and_run(pool) == 1  # the resume claim: fails closed, ends the session, realizes the steer
    assert world.llm_calls == [], "the failed resume must not call the model"
    assert _user_inputs(world) == [STEER]

    assert await world.claim_and_run(pool) == 1  # the re-armed claim runs the turn for the realized message
    assert world.llm_calls == [STEER], f"the queued message never got its turn: {world.llm_calls}"
    assert await _pending(world) == []
