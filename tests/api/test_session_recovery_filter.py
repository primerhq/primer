"""Unit tests for ``recover_sessions`` (01a0518a edge #3 sweep finding).

Before the parked flip, "every non-ENDED session" was a reasonable
approximation of "sessions that crashed mid-turn or have pending work" -
a clean turn always ended the session, so anything else non-ENDED was
either RUNNING (crashed mid-turn) or a legitimate, narrow WAITING (an
assistant question, max_tokens). With ``_CLEAN_TURN_RESTS_PARKED``
resting a clean turn at WAITING instead, that same "non-ENDED" filter
would re-arm the claim engine (and, for RUNNING, the scheduler) for
essentially every agent session that ever completed a turn - firing a
genuine, wasted LLM call per row on every single process restart. These
tests pin the narrowed predicate: RUNNING (this function's original
purpose) or turn_status="claimable" (independent evidence of real
pending work) get recovered; a merely-resting WAITING/idle session does
not.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from primer.model.workspace_session import (
    AgentSessionBinding,
    SessionStatus,
    WorkspaceSession,
)


class _RecordingClaimEngine:
    def __init__(self) -> None:
        self.upserted: list[str] = []

    async def upsert(self, kind: Any, entity_id: str) -> None:
        self.upserted.append(entity_id)


class _RecordingScheduler:
    def __init__(self) -> None:
        self.enqueued: list[str] = []

    async def enqueue(self, session_id: str) -> None:
        self.enqueued.append(session_id)


def _sess(
    id: str, *, status: SessionStatus, turn_status: str = "idle", turn_no: int = 0,
) -> WorkspaceSession:
    return WorkspaceSession(
        id=id, workspace_id="w1",
        binding=AgentSessionBinding(agent_id="ag1"),
        status=status, created_at=datetime.now(UTC),
        turn_status=turn_status, turn_no=turn_no,
    )


@pytest.mark.asyncio
async def test_running_is_recovered_and_enqueued(fake_storage_provider):
    from primer.api._app_lifespan_phases import recover_sessions

    storage = fake_storage_provider.get_storage(WorkspaceSession)
    await storage.create(_sess("s-running", status=SessionStatus.RUNNING, turn_status="running"))

    claim_engine = _RecordingClaimEngine()
    scheduler = _RecordingScheduler()
    await recover_sessions(claim_engine, scheduler, fake_storage_provider)

    assert claim_engine.upserted == ["s-running"]
    assert scheduler.enqueued == ["s-running"]


@pytest.mark.asyncio
async def test_claimable_non_running_is_recovered_without_enqueue(fake_storage_provider):
    """Independent evidence of pending work (a queued steer/resume not
    yet picked up) still needs a lease, regardless of status - but only
    RUNNING gets the extra scheduler nudge."""
    from primer.api._app_lifespan_phases import recover_sessions

    storage = fake_storage_provider.get_storage(WorkspaceSession)
    await storage.create(_sess(
        "s-claimable", status=SessionStatus.WAITING, turn_status="claimable", turn_no=1,
    ))

    claim_engine = _RecordingClaimEngine()
    scheduler = _RecordingScheduler()
    await recover_sessions(claim_engine, scheduler, fake_storage_provider)

    assert claim_engine.upserted == ["s-claimable"]
    assert scheduler.enqueued == []


@pytest.mark.asyncio
async def test_resting_parked_session_is_not_recovered(fake_storage_provider):
    """01a0518a: a session that cleanly finished a turn and now rests at
    WAITING/idle (session_state="parked") has no pending work - it must
    NOT be re-armed on every restart. wake_session already re-arms it
    itself the moment a real new message actually arrives."""
    from primer.api._app_lifespan_phases import recover_sessions

    storage = fake_storage_provider.get_storage(WorkspaceSession)
    await storage.create(_sess(
        "s-parked", status=SessionStatus.WAITING, turn_status="idle", turn_no=1,
    ))

    claim_engine = _RecordingClaimEngine()
    scheduler = _RecordingScheduler()
    await recover_sessions(claim_engine, scheduler, fake_storage_provider)

    assert claim_engine.upserted == []
    assert scheduler.enqueued == []


@pytest.mark.asyncio
async def test_ended_session_is_never_recovered(fake_storage_provider):
    from primer.api._app_lifespan_phases import recover_sessions

    storage = fake_storage_provider.get_storage(WorkspaceSession)
    await storage.create(_sess("s-ended", status=SessionStatus.ENDED, turn_status="idle", turn_no=1))

    claim_engine = _RecordingClaimEngine()
    scheduler = _RecordingScheduler()
    await recover_sessions(claim_engine, scheduler, fake_storage_provider)

    assert claim_engine.upserted == []
    assert scheduler.enqueued == []
