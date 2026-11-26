"""Review 2026-11-23 K1 carried: the pool's terminal ends must announce themselves.

``pool._end_session`` used to write a bare ENDED status and announce nothing, and the
preempt-cancel convergence of ``_run_engine_session`` took it. So a row ended there (a
preempted turn whose row carries a user Cancel, or a resume whose handler raised an
unclassified error) ended WITHOUT the durable ``session.ended`` event, WITHOUT the
``session:{id}:terminal`` publish a webhook hold (primer/trigger/hold.py) waits on, and
WITHOUT the CANCELLED transcript record a full cancelled terminal writes: the hold waited
out its cap and ``session.ended`` subscribers never fired (review 2026-11-23, K1 round 2).

The fix, in two halves:
  * the preempt-cancel path routes a ``cancel_requested`` row through
    ``_end_cancelled_at_claim`` (the same full cancelled terminal a normal turn's
    claim-time exit uses: CANCELLED record, tick, session.ended, terminal publish);
  * the ``WorkspaceRefusedError`` fallback of that exit -- and the failed-resume exit,
    which ends through ``_end_session`` -- still end the row, and ``_end_session`` now
    announces the end (session.ended + the terminal publish). Only the transcript record
    write is skipped there, which is the one a refused workspace cannot take.

Driven through the real ``WorkerPool`` and the real ``InMemoryClaimEngine`` +
``SessionClaimAdapter``: the preempt is delivered the way ``_heartbeat_loop`` delivers
it (``scope.cancel(CANCEL_REASON_PREEMPTED)`` on the registered scope), the workspace
refusal the way the registry delivers it (a ``get_workspace`` that raises
``WorkspaceRefusedError``), and the failed resume the way a raising handler does.
"""

from __future__ import annotations

import asyncio
import json
from contextlib import suppress
from datetime import datetime, timezone

import pytest

from primer.claim.adapters.sessions import SessionClaimAdapter
from primer.claim.in_memory import InMemoryClaimEngine
from primer.int.claim import ClaimKind
from primer.model.scheduler import WorkerConfig
from primer.model.workspace_refusal import WorkspaceRefusedError
from primer.model.workspace_session import (
    AgentSessionBinding,
    SessionStatus,
    WorkspaceSession,
)
from primer.model.yield_ import CANCEL_REASON_PREEMPTED
from primer.worker.pool import WorkerPool

# The established session-factory harness fakes (published/scheduler recording).
from tests.conftest import _FakeStorageProvider
from tests.workspace.test_session_factory import _FakeEventBus, _FakeScheduler

WORKER = "wrk-k1-carry"


class _EventStore:
    """Records the platform event log appends (the recorder's store)."""

    def __init__(self) -> None:
        self.emitted: list[tuple[str, dict]] = []

    async def append(self, **kwargs) -> int:
        self.emitted.append((kwargs["event_type"], kwargs.get("payload") or {}))
        return len(self.emitted)


class _RecordingWorkspace:
    """Records the transcript lines the pool writes into the workspace."""

    def __init__(self) -> None:
        self.lines = bytearray()

    async def append_message_line(self, session_id: str, line: bytes) -> None:
        self.lines += line

    def records(self) -> list[dict]:
        return [
            json.loads(line)
            for line in self.lines.decode().splitlines()
            if line.strip()
        ]


class _FakeWorkspaceRegistry:
    def __init__(self, workspace) -> None:
        self._workspace = workspace

    async def get_workspace(self, workspace_id: str):
        return self._workspace

    async def check_workspace_allowed(self, workspace_id: str) -> None:
        return None


class _RefusingWorkspaceRegistry:
    """A deployment that refuses the workspace: get_workspace raises, and every
    resolution attempt is recorded (the record write is the one that may be skipped)."""

    def __init__(self) -> None:
        self.attempts: list[str] = []

    async def get_workspace(self, workspace_id: str):
        self.attempts.append(workspace_id)
        raise WorkspaceRefusedError("the deployment refuses this workspace")

    async def check_workspace_allowed(self, workspace_id: str) -> None:
        return None


def _build_engine(session_storage) -> InMemoryClaimEngine:
    return InMemoryClaimEngine(
        adapters={
            ClaimKind.SESSION: SessionClaimAdapter(session_storage=session_storage),
        },
    )


def _build_pool(sp, engine, bus, scheduler, registry) -> WorkerPool:
    pool = WorkerPool(
        config=WorkerConfig(concurrency=1),
        scheduler=scheduler,
        storage=sp,
        workspace_registry=registry,
        provider_registry=None,  # type: ignore[arg-type]
        engine=engine,
        event_bus=bus,
    )
    pool._worker_id = WORKER
    return pool


@pytest.mark.asyncio
async def test_preempt_cancel_convergence_publishes_the_full_cancelled_terminal(monkeypatch):
    """A RUNNING row with a user Cancel whose in-flight turn is hard-cancelled by the
    lost-lease preempt ends with the FULL cancelled terminal: the CANCELLED transcript
    record, the session.ended event and the session:{id}:terminal publish a webhook hold
    waits on -- not the bare status write the pre-fix convergence took."""
    sid = "sess-k1-preempt"
    sp = _FakeStorageProvider()
    sessions = sp.get_storage(WorkspaceSession)
    engine = _build_engine(sessions)
    bus = _FakeEventBus()
    scheduler = _FakeScheduler()
    events = _EventStore()
    sp.get_event_store = lambda: events
    workspace = _RecordingWorkspace()
    pool = _build_pool(sp, engine, bus, scheduler, _FakeWorkspaceRegistry(workspace))

    sess = WorkspaceSession(
        id=sid,
        workspace_id=f"ws-{sid}",
        binding=AgentSessionBinding(kind="agent", agent_id="ag-1"),
        status=SessionStatus.RUNNING,
        created_at=datetime.now(timezone.utc),
        turn_no=0,
        cancel_requested=True,
    )
    await sessions.create(sess)
    await engine.mark_resumable(ClaimKind.SESSION, sid)
    lease = next(
        l for l in await engine.claim_due(WORKER, max_count=10)
        if l.kind == ClaimKind.SESSION and l.entity_id == sid
    )

    blocked = asyncio.Event()
    never = asyncio.Event()

    async def _blocking_turn(_lease, _deps):
        blocked.set()
        await never.wait()

    monkeypatch.setattr("primer.worker.pool.run_one_session_turn", _blocking_turn)

    task = asyncio.create_task(pool._run_engine(lease, pool._run_engine_session))
    try:
        await asyncio.wait_for(blocked.wait(), timeout=5.0)
        scope = pool._active_scopes.get((lease.kind, lease.entity_id))
        assert scope is not None, "the cancel scope must be registered while in flight"
        # The lost-lease preempt, the way _heartbeat_loop delivers it.
        scope.cancel(CANCEL_REASON_PREEMPTED)
        await asyncio.wait_for(task, timeout=10.0)

        row = await sessions.get(sid)
        assert row is not None
        assert row.status == SessionStatus.ENDED
        assert row.ended_reason == "cancelled"
        # 1. The CANCELLED transcript record landed in the workspace.
        records = workspace.records()
        assert any(r.get("kind") == "cancelled" for r in records), (
            f"no CANCELLED record in the transcript: {records!r}"
        )
        # 2. The session.ended event was recorded for subscribers.
        assert ("session.ended", {"ended_reason": "cancelled"}) in events.emitted
        # 3. The terminal publish a webhook hold waits on.
        assert any(key == f"session:{sid}:terminal" for key, _ in bus.published), (
            f"no terminal publish: {bus.published!r}"
        )
        # The lease is dropped: an ended session is not re-claimable.
        assert all(l.entity_id != sid for l in await engine.claim_due(WORKER, max_count=10))
    finally:
        never.set()
        if not task.done():
            task.cancel()
        with suppress(Exception, asyncio.CancelledError):
            await task


@pytest.mark.asyncio
async def test_a_refused_workspace_fallback_still_announces_the_end(monkeypatch):
    """The deployment refuses the workspace, so the full cancelled terminal's record
    cannot be written and the exit cannot complete: the fallback ends the row through
    the pool's own write. It still publishes the terminal and the session.ended event --
    only the transcript record write is skipped (the refused workspace never sees a
    single write attempt)."""
    sid = "sess-k1-refused"
    sp = _FakeStorageProvider()
    sessions = sp.get_storage(WorkspaceSession)
    engine = _build_engine(sessions)
    bus = _FakeEventBus()
    scheduler = _FakeScheduler()
    events = _EventStore()
    sp.get_event_store = lambda: events
    registry = _RefusingWorkspaceRegistry()
    pool = _build_pool(sp, engine, bus, scheduler, registry)

    sess = WorkspaceSession(
        id=sid,
        workspace_id=f"ws-{sid}",
        binding=AgentSessionBinding(kind="agent", agent_id="ag-1"),
        status=SessionStatus.RUNNING,
        created_at=datetime.now(timezone.utc),
        turn_no=0,
        cancel_requested=True,
    )
    await sessions.create(sess)
    await engine.mark_resumable(ClaimKind.SESSION, sid)
    lease = next(
        l for l in await engine.claim_due(WORKER, max_count=10)
        if l.kind == ClaimKind.SESSION and l.entity_id == sid
    )

    blocked = asyncio.Event()
    never = asyncio.Event()

    async def _blocking_turn(_lease, _deps):
        blocked.set()
        await never.wait()

    monkeypatch.setattr("primer.worker.pool.run_one_session_turn", _blocking_turn)

    task = asyncio.create_task(pool._run_engine(lease, pool._run_engine_session))
    try:
        await asyncio.wait_for(blocked.wait(), timeout=5.0)
        scope = pool._active_scopes.get((lease.kind, lease.entity_id))
        assert scope is not None
        scope.cancel(CANCEL_REASON_PREEMPTED)
        await asyncio.wait_for(task, timeout=10.0)

        row = await sessions.get(sid)
        assert row is not None
        assert row.status == SessionStatus.ENDED
        assert row.ended_reason == "cancelled"
        # The refusal swallowed exactly ONE write attempt (the CANCELLED record);
        # the fallback's own end write never needs the workspace.
        assert registry.attempts == [f"ws-{sid}"], registry.attempts
        # The announcement the pre-fix fallback skipped:
        assert ("session.ended", {"ended_reason": "cancelled"}) in events.emitted
        assert any(key == f"session:{sid}:terminal" for key, _ in bus.published), (
            f"no terminal publish: {bus.published!r}"
        )
        assert all(l.entity_id != sid for l in await engine.claim_due(WORKER, max_count=10))
    finally:
        never.set()
        if not task.done():
            task.cancel()
        with suppress(Exception, asyncio.CancelledError):
            await task


@pytest.mark.asyncio
async def test_a_failed_resume_end_announces_the_terminal(monkeypatch):
    """A resume handler that raised an unclassified error ends the session failed
    through _end_resume_failed: the ERROR record and the terminal ENDED/failed write
    announce themselves (session.ended + the terminal publish), and the park -- the
    person's answer -- is preserved for the operator to read."""
    sid = "sess-k1-failed-resume"
    answer = "the person's answer parked on the session"
    sp = _FakeStorageProvider()
    sessions = sp.get_storage(WorkspaceSession)
    engine = _build_engine(sessions)
    bus = _FakeEventBus()
    scheduler = _FakeScheduler()
    events = _EventStore()
    sp.get_event_store = lambda: events
    workspace = _RecordingWorkspace()
    pool = _build_pool(sp, engine, bus, scheduler, _FakeWorkspaceRegistry(workspace))

    now = datetime.now(timezone.utc)
    sess = WorkspaceSession(
        id=sid,
        workspace_id=f"ws-{sid}",
        binding=AgentSessionBinding(kind="agent", agent_id="ag-1"),
        status=SessionStatus.RUNNING,
        created_at=now,
        turn_no=1,
        cancel_requested=False,
        parked_status="resumable",
        parked_at=now,
        parked_event_key="ask_user:call-1",
        parked_state={"kind": "ask_user", "resume_event_payload": {"answer": answer}},
    )
    await sessions.create(sess)
    await engine.mark_resumable(ClaimKind.SESSION, sid)
    lease = next(
        l for l in await engine.claim_due(WORKER, max_count=10)
        if l.kind == ClaimKind.SESSION and l.entity_id == sid
    )

    async def _raising_resume(_lease, _row):
        raise RuntimeError("the resume handler blew up (simulated)")

    monkeypatch.setattr(pool, "_resume_engine_session", _raising_resume)

    task = asyncio.create_task(pool._run_engine(lease, pool._run_engine_session))
    try:
        await asyncio.wait_for(task, timeout=10.0)

        row = await sessions.get(sid)
        assert row is not None
        assert row.status == SessionStatus.ENDED
        assert row.ended_reason == "failed"
        # The park -- and the answer on it -- is preserved (preserve_park release).
        assert row.parked_status == "resumable", (
            f"the failed resume cleared the park ({row.parked_status!r})"
        )
        assert (row.parked_state or {}).get("resume_event_payload", {}).get("answer") == answer
        assert row.last_turn_error is not None
        # The ERROR record of the refusal landed in the transcript.
        records = workspace.records()
        assert any(r.get("kind") == "error" for r in records), (
            f"no ERROR record of the failed resume: {records!r}"
        )
        # The announcement the pre-fix exit skipped:
        assert ("session.ended", {"ended_reason": "failed"}) in events.emitted
        assert any(key == f"session:{sid}:terminal" for key, _ in bus.published), (
            f"no terminal publish: {bus.published!r}"
        )
        assert all(l.entity_id != sid for l in await engine.claim_due(WORKER, max_count=10))
    finally:
        if not task.done():
            task.cancel()
        with suppress(Exception, asyncio.CancelledError):
            await task
