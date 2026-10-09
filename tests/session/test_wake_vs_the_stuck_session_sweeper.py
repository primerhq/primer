"""A message to a rested session is not undone by the stuck-session sweeper in the instant before its claim is armed (C-024 slice 2, review of PR 623).

``wake_session`` moves a rested (WAITING, stamped) row to RUNNING with a whole-row write and only THEN arms the claim (``claim_engine.upsert``), all under
the session's lifecycle lock. For that instant the row is RUNNING, ``turn_no`` 0, past the grace (a first turn that failed was started long ago), stamped
and without a lease: every mark of the half-finished failure exit the sweeper reaps on purpose. The sweeper read the row and the lease table without the
lock, so it could end the session the user had just messaged. It now takes the same lock for each candidate, so it sees the row after the wake has
finished (with its lease), or ends it before the wake starts (and the wake reopens an ended session).
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from primer.bus.scheduler_tasks import StuckSessionSweeper
from primer.int.claim import ClaimKind
from primer.model.workspace_session import AgentSessionBinding, LastTurnError, SessionStatus, WorkspaceSession
from primer.session.enqueue import SessionWakeDeps, wake_session
from tests.session.test_dispatch import fake_event_bus, fake_storage_provider  # noqa: F401  (fixtures are used by name)

SID = "s-rested"


class _Slot:
    async def reopen(self) -> None: ...

    async def append_instruction(self, content, *, extra_parts=None) -> None: ...


class _Workspace:
    async def get_session(self, session_id):
        return _Slot()


class _Registry:
    async def get_workspace(self, workspace_id):
        return _Workspace()

    async def get_workspace_row(self, workspace_id):
        return None


class _Scheduler:
    async def enqueue(self, session_id) -> None: ...


class _Engine:
    """The claim engine as the wake and the sweeper both see it: ``upsert`` arms a lease row, ``has_lease`` asks for one.

    ``during_upsert`` runs while the wake is in the middle of arming the claim, which is where a sweeper tick can land.
    """

    def __init__(self) -> None:
        self.leased: set[str] = set()
        self.during_upsert = None

    async def upsert(self, kind, entity_id) -> None:
        if self.during_upsert is not None:
            await self.during_upsert()
        self.leased.add(entity_id)

    async def has_lease(self, kind: ClaimKind, entity_id: str) -> bool:
        return entity_id in self.leased


async def _rested_row(fake_storage_provider) -> WorkspaceSession:
    long_ago = datetime.now(timezone.utc) - timedelta(hours=1)
    row = WorkspaceSession(
        id=SID, workspace_id="ws-1", binding=AgentSessionBinding(agent_id="agent-a"), status=SessionStatus.WAITING,
        created_at=long_ago, started_at=long_ago, turn_no=0, turn_status="idle",
        last_turn_error=LastTurnError(code="server_error", at=long_ago + timedelta(minutes=1)),
    )
    await fake_storage_provider.get_storage(WorkspaceSession).create(row)
    return row


@pytest.mark.asyncio
async def test_a_sweeper_tick_in_the_middle_of_a_wake_does_not_end_the_session_the_user_just_messaged(fake_storage_provider, fake_event_bus):
    row = await _rested_row(fake_storage_provider)
    sessions = fake_storage_provider.get_storage(WorkspaceSession)
    engine = _Engine()
    sweeper = StuckSessionSweeper(session_storage=sessions, claim_engine=engine)
    ticks: list[asyncio.Task] = []

    async def a_tick_lands_while_the_claim_is_being_armed() -> None:
        ticks.append(asyncio.create_task(sweeper._tick()))
        await asyncio.sleep(0.05)               # the tick runs now, if nothing stops it

    engine.during_upsert = a_tick_lands_while_the_claim_is_being_armed
    deps = SessionWakeDeps(
        storage_provider=fake_storage_provider, scheduler=_Scheduler(), claim_engine=engine,
        workspace_registry=_Registry(), event_bus=fake_event_bus,
    )

    woken = await wake_session(workspace_id=row.workspace_id, session_id=SID, instruction=None, human_intent=True, deps=deps)
    reaped = sum(await asyncio.gather(*ticks))

    assert woken.status == SessionStatus.RUNNING
    assert reaped == 0, "the sweeper ended a session whose claim the wake was arming"
    after = await sessions.get(SID)
    assert (after.status, after.ended_reason, after.ended_detail) == (SessionStatus.RUNNING, None, None)
    assert SID in engine.leased


@pytest.mark.asyncio
async def test_a_stamped_running_row_with_no_wake_in_flight_is_still_reaped(fake_storage_provider):
    """The lock is for the race, not an exemption: a stamped RUNNING row nobody is waking is the unfinished failure exit the sweeper exists to end."""
    long_ago = datetime.now(timezone.utc) - timedelta(hours=1)
    sessions = fake_storage_provider.get_storage(WorkspaceSession)
    await sessions.create(WorkspaceSession(
        id=SID, workspace_id="ws-1", binding=AgentSessionBinding(agent_id="agent-a"), status=SessionStatus.RUNNING,
        created_at=long_ago, started_at=long_ago, turn_no=0, turn_status="idle",
        last_turn_error=LastTurnError(code="server_error", at=long_ago),
    ))

    reaped = await StuckSessionSweeper(session_storage=sessions, claim_engine=_Engine())._tick()

    assert reaped == 1
    assert (await sessions.get(SID)).ended_detail == "failure_exit_unfinished"
