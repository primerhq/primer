"""Preempt-cancel convergence on the normal-turn engine path.

When a REST cancel races an in-flight NORMAL agent turn, two outcomes
race in the worker:

  (a) GOOD: the running turn's stream observes the cancel event between
      LLM calls and ends gracefully (dispatch.py). A fast LLM wins this.
  (b) BAD: the heartbeat loop sees the lease lost and calls
      ``scope.cancel("preempted")`` (pool._heartbeat_loop), HARD-cancelling
      the turn task with ``asyncio.CancelledError``. A slow LLM (turn
      blocked in a long completion) hits this first.

On the BAD path the CancelledError used to propagate straight out of
``_run_engine_session`` (it is a BaseException, not caught by
``except Exception``), through ``_run_engine`` which only logged
"cancelled (preempted)" -- the session row was never transitioned to a
terminal state and stayed stuck RUNNING forever.

``_run_engine_session`` now catches the CancelledError on the normal-turn
path, re-reads the fresh row, and -- ONLY when ``cancel_requested`` is
True -- converges the session to ENDED/cancelled (then re-raises). A
genuine lease STEAL (``cancel_requested`` False) is left untouched so the
owning worker drives it to terminal; ending it here would corrupt the
multi-worker handoff.

These two tests pin both halves deterministically WITHOUT a real LLM: a
patched ``run_one_session_turn`` blocks on an event that never fires, the
turn task is cancelled the same way the heartbeat preempt does, and we
assert convergence (cancel_requested=True) vs no-op (cancel_requested
=False).
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import pytest

from primer.claim.adapters.sessions import SessionClaimAdapter
from primer.claim.in_memory import InMemoryClaimEngine
from primer.int.claim import ClaimKind
from primer.model.scheduler import WorkerConfig
from primer.model.workspace_session import (
    AgentSessionBinding,
    SessionStatus,
    WorkspaceSession,
)
from primer.worker.pool import WorkerPool

from tests.conftest import _FakeStorageProvider


def _build_engine(session_storage) -> InMemoryClaimEngine:
    return InMemoryClaimEngine(
        adapters={
            ClaimKind.SESSION: SessionClaimAdapter(session_storage=session_storage),
        },
    )


def _build_pool(storage, engine) -> WorkerPool:
    pool = WorkerPool(
        config=WorkerConfig(concurrency=1),
        scheduler=None,                  # type: ignore[arg-type]
        storage=storage,
        workspace_registry=None,         # type: ignore[arg-type]
        provider_registry=None,          # type: ignore[arg-type]
        engine=engine,
    )
    pool._worker_id = "wrk-preempt"
    return pool


def _make_running_session(sid: str, *, cancel_requested: bool) -> WorkspaceSession:
    """A plain RUNNING (non-parked) session -> dispatches to the normal
    turn path (``run_one_session_turn``)."""
    return WorkspaceSession(
        id=sid,
        workspace_id=f"ws-{sid}",
        binding=AgentSessionBinding(kind="agent", agent_id="ag-1"),
        status=SessionStatus.RUNNING,
        created_at=datetime.now(timezone.utc),
        turn_no=0,
        cancel_requested=cancel_requested,
    )


async def _claim_session(engine, sid: str):
    await engine.mark_resumable(ClaimKind.SESSION, sid)
    leases = await engine.claim_due("wrk-preempt", max_count=10)
    for lease in leases:
        if lease.kind == ClaimKind.SESSION and lease.entity_id == sid:
            return lease
    raise AssertionError(f"no claimable lease for session {sid!r}")


async def _run_and_preempt(pool, lease, *, monkeypatch):
    """Dispatch ``_run_engine_session`` on a task whose normal turn blocks
    forever, then preempt it exactly like ``_heartbeat_loop`` does:
    ``scope.cancel("preempted")``. Returns once the task has finished
    re-raising the CancelledError."""
    blocked = asyncio.Event()        # set once the turn is actually blocked
    never = asyncio.Event()          # never set -> the turn blocks forever

    async def _blocking_turn(_engine_lease, _deps):
        blocked.set()
        await never.wait()           # blocks until the task is cancelled
        raise AssertionError("turn should have been cancelled")  # pragma: no cover

    # Patch the symbol as imported into primer.worker.pool.
    monkeypatch.setattr("primer.worker.pool.run_one_session_turn", _blocking_turn)

    # Drive through _run_engine so the real _CancelScope is registered in
    # pool._active_scopes -- the heartbeat preempt path operates on that.
    task = asyncio.create_task(
        pool._run_engine(lease, pool._run_engine_session)
    )
    await asyncio.wait_for(blocked.wait(), timeout=2.0)

    key = (lease.kind, lease.entity_id)
    scope = pool._active_scopes.get(key)
    assert scope is not None, "cancel scope must be registered while in flight"
    scope.cancel("preempted")        # mirror _heartbeat_loop:316-317

    # _run_engine swallows the CancelledError (logs "cancelled (preempted)")
    # so awaiting the task returns normally.
    await asyncio.wait_for(task, timeout=2.0)


@pytest.mark.asyncio
async def test_preempt_with_cancel_requested_converges_to_ended(monkeypatch):
    """BAD-path repro: a RUNNING session with cancel_requested=True whose
    normal turn is hard-cancelled by the heartbeat preempt must converge to
    ENDED/cancelled (was stuck RUNNING before the fix)."""
    sid = "sess-preempt-cancel"

    storage_provider = _FakeStorageProvider()
    session_storage = storage_provider.get_storage(WorkspaceSession)
    engine = _build_engine(session_storage)
    pool = _build_pool(storage_provider, engine)

    sess = _make_running_session(sid, cancel_requested=True)
    await session_storage.create(sess)

    lease = await _claim_session(engine, sid)
    await _run_and_preempt(pool, lease, monkeypatch=monkeypatch)

    row = await session_storage.get(sid)
    assert row is not None
    assert row.status == SessionStatus.ENDED
    assert row.ended_reason == "cancelled"
    assert row.ended_at is not None

    # drop_lease=True -> the ended session is not re-claimable.
    leases = await engine.claim_due("wrk-preempt", max_count=10)
    assert not any(
        l.kind == ClaimKind.SESSION and l.entity_id == sid for l in leases
    ), "ended session must not have a surviving lease"


@pytest.mark.asyncio
async def test_preempt_without_cancel_requested_is_not_ended(monkeypatch):
    """Lease-STEAL safety: a RUNNING session with cancel_requested=False
    that is preempted (another worker legitimately took the lease) must NOT
    be ended by this worker -- it stays RUNNING for the new owner to drive."""
    sid = "sess-preempt-steal"

    storage_provider = _FakeStorageProvider()
    session_storage = storage_provider.get_storage(WorkspaceSession)
    engine = _build_engine(session_storage)
    pool = _build_pool(storage_provider, engine)

    sess = _make_running_session(sid, cancel_requested=False)
    await session_storage.create(sess)

    lease = await _claim_session(engine, sid)
    await _run_and_preempt(pool, lease, monkeypatch=monkeypatch)

    row = await session_storage.get(sid)
    assert row is not None
    # The stolen session is untouched by this worker: still RUNNING, no
    # terminal columns written.
    assert row.status == SessionStatus.RUNNING
    assert row.ended_reason is None
    assert row.ended_at is None
