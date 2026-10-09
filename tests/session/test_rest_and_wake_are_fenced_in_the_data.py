"""The rest of a failed turn and the wake of a rested session are fenced in the DATA, not by an in-process lock (C-024 slice 2, review round 3 of PR 623).

``session_lifecycle_lock`` is per process. The stuck-session sweeper is leader-elected and in k3s can run on another pod than the API that wakes a
session, a Cancel can be set by another API process, and the pool's re-arm after a release takes no lock at all. So:

* the failure exit RESTS the session with ONE field-scoped ``patch_if`` guarded on the row not being ended, on ``cancel_requested`` being false and on
  the binding epoch, not on a read it made earlier under the lock: a Cancel that lands after that read refuses the write and the session ends;
* a wake or a resume of a row that carries ``last_turn_error`` restarts the sweeper's grace clock (``started_at``), because that row is RUNNING with
  no lease in the instant between the write and the claim arming, and its old ``started_at`` made it exactly what the sweeper reaps;
* the sweeper takes NO lock: a lock holder doing workspace I/O (a cancel's slot mirror, a wake's append) would stall every tick.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from primer.bus.scheduler_tasks import StuckSessionSweeper
from primer.int.claim import ClaimKind
from primer.model.chat import Error, TurnStreamFailure
from primer.model.workspace_session import AgentSessionBinding, LastTurnError, SessionStatus, WorkspaceSession
from primer.session.enqueue import SessionWakeDeps, wake_session
from primer.session.mutation_lock import session_lifecycle_lock
from tests.session.test_dispatch import (  # noqa: F401  (fixtures are used by name)
    FakeExecutor,
    _make_lease,
    _seed_session,
    fake_event_bus,
    fake_storage_provider,
    fake_workspace_io,
)
from tests.session.test_advisory_records_write_back_their_seq import _isolating
from tests.session.test_wake_vs_the_stuck_session_sweeper import _Engine, _Registry, _Scheduler, _rested_row

SID = "s-rested"


def _failure(code: str = "server_error") -> TurnStreamFailure:
    return TurnStreamFailure(Error(code=code, message="boom", fatal=True), partial_messages=[], rounds_completed=0)


async def _fail_one_turn(storage_provider, io, bus, session) -> None:
    from primer.session.dispatch import SessionDispatchDeps, run_one_session_turn

    async def build(_session):
        return FakeExecutor([_failure()])

    deps = SessionDispatchDeps(storage_provider=storage_provider, workspace_io=io, event_bus=bus, build_executor=build)
    await run_one_session_turn(_make_lease(session.id), deps)


@pytest.mark.asyncio
async def test_a_cancel_set_by_another_process_after_the_failure_exit_read_the_row_ends_the_session(
    fake_workspace_io, fake_event_bus, fake_storage_provider,
):
    """The exit's own read (under the in-process lock) says no Cancel is pending; the Cancel lands in the stored row right after the stamp. The rest
    must refuse in the write itself, and the session ends instead of resting with ``cancel_requested`` set (which would end it cancelled, without
    ever calling the model, on the next send)."""
    session = await _seed_session(fake_storage_provider, "s-cancel-race")
    storage = fake_storage_provider.get_storage(WorkspaceSession)
    real_get, real_patch_if, real_update = storage.get, storage.patch_if, storage.update
    stamped = {"done": False}

    async def patch_if(id, patch=None, **kwargs):
        written = await real_patch_if(id, patch, **kwargs)
        if patch and "last_turn_error" in patch and written is not None and not stamped["done"]:
            stamped["done"] = True
            await real_update((await real_get(id)).model_copy(update={"cancel_requested": True}))      # another process's Cancel
        return written

    async def get(id, **kwargs):
        row = await real_get(id, **kwargs)
        if row is not None and stamped["done"]:
            return row.model_copy(update={"cancel_requested": False})          # the snapshot the exit decided from
        return row

    storage.patch_if, storage.get = patch_if, get

    await _fail_one_turn(fake_storage_provider, fake_workspace_io, fake_event_bus, session)

    row = await real_get("s-cancel-race")
    assert (row.status, row.ended_reason) == (SessionStatus.ENDED, "failed"), "a session with a pending Cancel rested"


@pytest.mark.asyncio
async def test_resting_is_one_field_scoped_write_and_no_whole_document_transition(
    fake_workspace_io, fake_event_bus, fake_storage_provider,
):
    session = await _seed_session(fake_storage_provider, "s-one-write")
    storage = fake_storage_provider.get_storage(WorkspaceSession)
    real_patch_if, real_update_unless = storage.patch_if, storage.update_unless
    seen: list[tuple[str, object]] = []

    async def patch_if(id, patch=None, **kwargs):
        seen.append(("patch_if", dict(patch or {}), dict(kwargs.get("where") or {})))
        return await real_patch_if(id, patch, **kwargs)

    async def update_unless(entity, **kwargs):
        seen.append(("update_unless", entity.status))
        return await real_update_unless(entity, **kwargs)

    storage.patch_if, storage.update_unless = patch_if, update_unless

    await _fail_one_turn(fake_storage_provider, fake_workspace_io, fake_event_bus, session)

    row = await storage.get("s-one-write")
    assert row.status == SessionStatus.WAITING
    rest = [c for c in seen if c[0] == "patch_if" and c[1].get("status") == "waiting"]
    assert len(rest) == 1, seen
    where = rest[0][2]
    assert where.get("cancel_requested") == [False] and "binding_epoch" in where and "status" in where, where
    # (later bookkeeping, the drain cursor, still writes the whole row; what matters is that the STATUS moved by the field-scoped write)
    first_waiting = next(i for i, c in enumerate(seen) if c[0] == "patch_if" and c[1].get("status") == "waiting")
    assert ("update_unless", SessionStatus.WAITING) not in seen[:first_waiting], "a whole-document write moved the status first"
    assert [c for c in seen[:first_waiting] if c[0] == "update_unless"] == []


@pytest.mark.asyncio
async def test_a_wake_restarts_the_grace_clock_of_a_rested_failure_so_a_sweeper_anywhere_leaves_it_alone(fake_storage_provider, fake_event_bus):
    """The window is real on ANY pod: the wake has written RUNNING and the lease row is not visible yet. The sweeper here holds no lock at all, and
    the row it reads is one the wake just started."""
    row = await _rested_row(fake_storage_provider)
    sessions = fake_storage_provider.get_storage(WorkspaceSession)
    engine = _Engine()

    class _LeaseNotVisibleYet(_Engine):
        async def upsert(self, kind, entity_id) -> None:
            return None                       # armed on the other side of a replica lag: has_lease still says no

    deps = SessionWakeDeps(
        storage_provider=fake_storage_provider, scheduler=_Scheduler(), claim_engine=_LeaseNotVisibleYet(),
        workspace_registry=_Registry(), event_bus=fake_event_bus,
    )
    before = datetime.now(timezone.utc)

    await wake_session(workspace_id=row.workspace_id, session_id=SID, instruction=None, human_intent=True, deps=deps)
    reaped = await StuckSessionSweeper(session_storage=sessions, claim_engine=engine)._tick()

    after = await sessions.get(SID)
    assert after.started_at is not None and after.started_at >= before, "the wake did not restart the clock the sweeper's age check reads"
    assert reaped == 0 and after.status == SessionStatus.RUNNING


@pytest.mark.asyncio
async def test_a_wake_of_a_row_without_a_failure_keeps_its_original_start(fake_storage_provider, fake_event_bus):
    long_ago = datetime.now(timezone.utc) - timedelta(hours=1)
    sessions = fake_storage_provider.get_storage(WorkspaceSession)
    await sessions.create(WorkspaceSession(
        id=SID, workspace_id="ws-1", binding=AgentSessionBinding(agent_id="agent-a"), status=SessionStatus.WAITING,
        created_at=long_ago, started_at=long_ago, turn_no=2, turn_status="idle",
    ))
    deps = SessionWakeDeps(
        storage_provider=fake_storage_provider, scheduler=_Scheduler(), claim_engine=_Engine(),
        workspace_registry=_Registry(), event_bus=fake_event_bus,
    )

    await wake_session(workspace_id="ws-1", session_id=SID, instruction=None, human_intent=True, deps=deps)

    assert (await sessions.get(SID)).started_at == long_ago


@pytest.mark.asyncio
async def test_the_sweeper_does_not_wait_for_the_lifecycle_lock(fake_storage_provider):
    """A holder of the lock may be doing workspace I/O that never returns (a cancel's slot mirror, a wake's append): one such session must not stall
    every tick, so the sweeper takes no lock."""
    long_ago = datetime.now(timezone.utc) - timedelta(hours=1)
    sessions = fake_storage_provider.get_storage(WorkspaceSession)
    await sessions.create(WorkspaceSession(
        id=SID, workspace_id="ws-1", binding=AgentSessionBinding(agent_id="agent-a"), status=SessionStatus.RUNNING,
        created_at=long_ago, started_at=long_ago, turn_no=0, turn_status="idle",
    ))
    sweeper = StuckSessionSweeper(session_storage=sessions, claim_engine=_Engine())

    async with session_lifecycle_lock().acquire(SID):
        reaped = await asyncio.wait_for(sweeper._tick(), 3)

    assert reaped == 1


# ---- round 4 of the review of PR 623: a stamped row that is already RUNNING, and the fence itself ------------------------------------------------


async def _stamped_running_row(storage) -> WorkspaceSession:
    """The unfinished failure exit the sweeper reaps on purpose: RUNNING, turn 0, past the grace, stamped, no lease."""
    long_ago = datetime.now(timezone.utc) - timedelta(hours=1)
    row = WorkspaceSession(
        id=SID, workspace_id="ws-1", binding=AgentSessionBinding(agent_id="agent-a"), status=SessionStatus.RUNNING,
        created_at=long_ago, started_at=long_ago, turn_no=0, turn_status="idle",
        last_turn_error=LastTurnError(code="server_error", at=long_ago),
    )
    await storage.create(row)
    return row


@pytest.mark.asyncio
async def test_a_wake_of_a_stamped_row_that_is_already_running_restarts_the_clock_too(fake_storage_provider, fake_event_bus):
    """The restart is not for the statuses a wake MOVES to RUNNING (CREATED, PAUSED, WAITING): a message to a stamped row that is already RUNNING (a
    failure exit that stamped and never finished) lands in the same window, between the sweeper's re-read and its write."""
    sessions = fake_storage_provider.get_storage(WorkspaceSession)
    row = await _stamped_running_row(sessions)
    before = datetime.now(timezone.utc)
    deps = SessionWakeDeps(
        storage_provider=fake_storage_provider, scheduler=_Scheduler(), claim_engine=_Engine(),
        workspace_registry=_Registry(), event_bus=fake_event_bus,
    )

    await wake_session(workspace_id=row.workspace_id, session_id=SID, instruction=None, human_intent=True, deps=deps)

    assert (await sessions.get(SID)).started_at >= before


@pytest.mark.asyncio
async def test_a_wake_between_the_sweepers_re_read_and_its_write_saves_a_stamped_running_row(fake_storage_provider, fake_event_bus):
    """The window itself: the sweeper has read the stamped RUNNING row and asks the lease table (no lease yet); the user's message is woken in
    that await. Its write must be refused, or the session the user just messaged is ended ``failure_exit_unfinished``."""
    sessions = fake_storage_provider.get_storage(WorkspaceSession)
    _isolating(sessions)          # the in-memory fake hands out the STORED object: without copies the wake would also change the sweeper's snapshot
    row = await _stamped_running_row(sessions)
    wake_deps = SessionWakeDeps(
        storage_provider=fake_storage_provider, scheduler=_Scheduler(), claim_engine=_Engine(),
        workspace_registry=_Registry(), event_bus=fake_event_bus,
    )

    class _WakesDuringTheLookup(_Engine):
        async def has_lease(self, kind, entity_id) -> bool:
            await wake_session(workspace_id=row.workspace_id, session_id=SID, instruction=None, human_intent=True, deps=wake_deps)
            return False

    reaped = await StuckSessionSweeper(session_storage=sessions, claim_engine=_WakesDuringTheLookup())._tick()

    after = await sessions.get(SID)
    assert reaped == 0 and (after.status, after.ended_reason) == (SessionStatus.RUNNING, None), "the session the user just messaged was ended"


@pytest.mark.asyncio
async def test_the_sweepers_write_for_a_stamped_row_is_fenced_on_the_started_at_it_read(fake_storage_provider):
    """Whatever moved ``started_at`` between the re-read and the write (a wake on another pod), the write is refused."""
    sessions = fake_storage_provider.get_storage(WorkspaceSession)
    await _stamped_running_row(sessions)

    class _MovesTheClockDuringTheLookup(_Engine):
        async def has_lease(self, kind, entity_id) -> bool:
            row = await sessions.get(entity_id)
            await sessions.update(row.model_copy(update={"started_at": datetime.now(timezone.utc)}))
            return False

    reaped = await StuckSessionSweeper(session_storage=sessions, claim_engine=_MovesTheClockDuringTheLookup())._tick()

    assert reaped == 0 and (await sessions.get(SID)).status == SessionStatus.RUNNING
