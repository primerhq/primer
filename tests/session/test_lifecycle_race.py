"""T0432 regression: a resume racing a cancel must not strand a session.

The bug: ``resume`` and ``cancel`` each do a non-atomic read-modify-write of
the session row plus an independent claim-lease mutation. Run concurrently on
a freshly-created (``auto_start=False``) session they lost-update each other —
one interleaving lands ``status=RUNNING`` (resume's write wins) with the lease
dropped (cancel's ``delete_lease`` wins). The in-memory claim engine only ever
claims rows that hold a lease, so that row never converges: stuck RUNNING,
``turn_no=0``, ``cancel_requested=False``.

These tests drive the REAL ``resume_session`` (api router) and
``cancel_session`` (workspace factory) handlers against a shared real
``InMemoryClaimEngine``, and pin the serialization contract the fix
restores: while one lifecycle handler is mid-flight on a session, the other
cannot race in. One handler is parked inside its critical section (holding the
``session_lifecycle_lock``); the sibling is then shown to make no progress
until the first completes. Without the lock the sibling races in and the
mid-flight assertion fails — that is the regression these guard.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any

import pytest

from primer.api.routers.sessions import resume_session
from primer.claim.in_memory import InMemoryClaimEngine
from primer.int.claim import ClaimKind
from primer.model.except_ import ConflictError
from primer.model.workspace_session import (
    AgentSessionBinding,
    SessionStatus,
    WorkspaceSession,
)
from primer.workspace.session_factory import SessionCancelDeps, cancel_session


class _Scheduler:
    def __init__(self) -> None:
        self.enqueued: list[str] = []
        self.signalled: list[str] = []

    async def enqueue(self, sid: str) -> None:
        self.enqueued.append(sid)

    async def signal_cancel(self, sid: str) -> None:
        self.signalled.append(sid)


class _EventBus:
    async def publish(self, key: str, payload: Any) -> None:
        pass


class _GatedStorage:
    """Models the real backend and parks the FIRST ``update`` mid-flight.

    Two fidelity points that make this match the SQLite/Postgres lane the bug
    lives in:

    * ``get`` returns a fresh deep copy (the real backend round-trips through
      a JSON blob), so concurrent handlers read INDEPENDENT row objects; and
    * the first ``update`` call parks on ``release`` after announcing itself
      via ``at_gate``. The test starts the handler it wants to hold the lock
      first and waits on ``at_gate``, so that handler is deterministically
      inside its critical section before the sibling is launched.
    """

    def __init__(self, inner) -> None:
        self._inner = inner
        self.at_gate = asyncio.Event()
        self.release = asyncio.Event()
        self._first = True

    async def get(self, sid: str, conn=None):
        row = await self._inner.get(sid, conn=conn)
        return row.model_copy(deep=True) if row is not None else None

    async def update(self, s, conn=None):
        if self._first:
            self._first = False
            self.at_gate.set()
            await self.release.wait()
        return await self._inner.update(s.model_copy(deep=True), conn=conn)

    def __getattr__(self, name: str):
        return getattr(self._inner, name)


class _YieldingProvider:
    def __init__(self, inner, wrapped_session_storage) -> None:
        self._inner = inner
        self._wrapped = wrapped_session_storage

    def get_storage(self, model):
        if model is WorkspaceSession:
            return self._wrapped
        return self._inner.get_storage(model)


async def _seed_created(inner, sid: str) -> None:
    await inner.create(
        WorkspaceSession(
            id=sid,
            workspace_id="ws-1",
            binding=AgentSessionBinding(agent_id="ag-1"),
            status=SessionStatus.CREATED,
            created_at=datetime.now(timezone.utc),
        )
    )


@pytest.mark.asyncio
async def test_cancel_in_flight_blocks_resume_then_converges_ended(
    fake_storage_provider,
) -> None:
    """Cancel holds the lock mid-flight; a concurrent resume must not flip the
    row to RUNNING. Once cancel finishes, the session is ENDED/cancelled with
    no lease, and the resume reports the session ended (409)."""
    inner = fake_storage_provider.get_storage(WorkspaceSession)
    storage = _GatedStorage(inner)
    provider = _YieldingProvider(fake_storage_provider, storage)
    engine = InMemoryClaimEngine(adapters={})
    scheduler = _Scheduler()
    sid = "sess-cancel-first"
    await _seed_created(inner, sid)
    deps = SessionCancelDeps(
        storage_provider=provider, scheduler=scheduler,
        claim_engine=engine, event_bus=_EventBus(),
    )

    cancel_task = asyncio.create_task(
        cancel_session(workspace_id="ws-1", session_id=sid, deps=deps)
    )
    await storage.at_gate.wait()  # cancel is inside its critical section

    resume_task = asyncio.create_task(
        resume_session(
            workspace_id="ws-1", session_id=sid,
            sessions=storage, scheduler=scheduler, engine=engine,
        )
    )
    await asyncio.sleep(0.02)  # let resume run (it should be blocked on the lock)

    mid = await inner.get(sid)
    assert mid.status == SessionStatus.CREATED, (
        "resume modified the session while cancel held the lock "
        f"(status={mid.status.value}) — serialization broken"
    )

    storage.release.set()
    _, resume_res = await asyncio.gather(
        cancel_task, resume_task, return_exceptions=True,
    )

    final = await inner.get(sid)
    assert final.status == SessionStatus.ENDED
    assert final.ended_reason == "cancelled"
    assert (ClaimKind.SESSION, sid) not in engine._leases
    assert isinstance(resume_res, ConflictError)


@pytest.mark.asyncio
async def test_resume_in_flight_blocks_cancel_then_cancel_is_honored(
    fake_storage_provider,
) -> None:
    """Resume holds the lock mid-flight; a concurrent cancel must not end the
    row out from under it. Once resume finishes, the session is RUNNING with a
    live lease, and cancel — observing RUNNING — records cancel_requested +
    signals the worker (the cancel is honored, not lost, and not stranded)."""
    inner = fake_storage_provider.get_storage(WorkspaceSession)
    storage = _GatedStorage(inner)
    provider = _YieldingProvider(fake_storage_provider, storage)
    engine = InMemoryClaimEngine(adapters={})
    scheduler = _Scheduler()
    sid = "sess-resume-first"
    await _seed_created(inner, sid)
    deps = SessionCancelDeps(
        storage_provider=provider, scheduler=scheduler,
        claim_engine=engine, event_bus=_EventBus(),
    )

    resume_task = asyncio.create_task(
        resume_session(
            workspace_id="ws-1", session_id=sid,
            sessions=storage, scheduler=scheduler, engine=engine,
        )
    )
    await storage.at_gate.wait()  # resume is inside its critical section

    cancel_task = asyncio.create_task(
        cancel_session(workspace_id="ws-1", session_id=sid, deps=deps)
    )
    await asyncio.sleep(0.02)  # let cancel run (it should be blocked on the lock)

    mid = await inner.get(sid)
    assert mid.status == SessionStatus.CREATED, (
        "cancel ended the session while resume held the lock "
        f"(status={mid.status.value}) — serialization broken"
    )

    storage.release.set()
    await asyncio.gather(resume_task, cancel_task, return_exceptions=True)

    final = await inner.get(sid)
    # Resume won the row; cancel saw RUNNING and recorded the cancel against a
    # live lease, so a worker converges it on the next claim.
    assert final.status == SessionStatus.RUNNING
    assert final.cancel_requested is True
    assert (ClaimKind.SESSION, sid) in engine._leases
    assert sid in scheduler.signalled
