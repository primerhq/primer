"""Review 2026-11-23 finding 1.6: a worker whose heartbeats keep failing stops its own turns.

``_heartbeat_loop`` used to swallow an engine-heartbeat failure with a log line
(``heartbeat_loop iteration failed``): the in-flight leases' TTL kept running with
nothing confirming them, and after a database outage longer than the lease TTL a
peer reclaimed a lease and ran its turn while this worker still ran it - a duplicate
execution the worker never learned of (it could not ask the database which leases it
still held).

The fix tracks the monotonic time each in-flight lease was last CONFIRMED (stamped at
claim, refreshed by every successful engine heartbeat) and, once a failing round trip
leaves a key unconfirmed for ``lease_ttl - heartbeat_interval / 8`` (the arithmetic is in
``WorkerPool.__init__``; round 1 of the fix moved the bound from ``ttl - heartbeat``),
cancels that key's scope with the lost-lease reason. The local turn stops, and whatever the database does afterwards, the release's
fence on claimed_by refuses a write over a lease the new owner holds: the worker never
writes over the peer.

Driven with the real pool, the real ``InMemoryClaimEngine`` (whose ``heartbeat`` is
scripted to fail) and the real ``SessionClaimAdapter``: a resumable row carries the
person's answer on its park, so a release that ran the adapter's non-park branch would
erase it - the observable "wrote nothing over the new owner" assertion.
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
from primer.scheduler.in_memory import InMemoryScheduler
from primer.worker.pool import WorkerPool

from tests.conftest import _FakeStorageProvider

WORKER = "wrk-hb-outage"
PEER = "wrk-hb-peer"
SID = "sess-hb-outage"
ANSWER = "the person's answer parked on the session"

# 1 s heartbeat against a 5 s TTL: the staleness bound is 4.875 s, so a backdated stamp
# fires on the very first failing tick (the test stays fast).
_CONFIG = dict(
    concurrency=1,
    heartbeat_interval_seconds=1,
    lease_ttl_seconds=5,
    poll_interval_seconds=0.1,
    drain_timeout_seconds=5,
)


async def _seed_resumable_row(session_storage) -> None:
    """A RUNNING session parked resumable with an answer on the park."""
    now = datetime.now(timezone.utc)
    await session_storage.create(
        WorkspaceSession(
            id=SID,
            workspace_id=f"ws-{SID}",
            binding=AgentSessionBinding(kind="agent", agent_id="ag-1"),
            status=SessionStatus.RUNNING,
            created_at=now,
            turn_no=1,
            cancel_requested=False,
            parked_status="resumable",
            parked_at=now,
            parked_event_key="ask_user:call-1",
            parked_state={"kind": "ask_user", "resume_event_payload": {"answer": ANSWER}},
        )
    )


@pytest.mark.asyncio
async def test_a_failing_heartbeat_past_the_lease_ttl_bound_cancels_the_turn_locally(
    monkeypatch,
):
    """An engine heartbeat that keeps failing stops the local turn once its lease is
    unconfirmed past the TTL bound, and the unwinding turn writes nothing over the peer
    that reclaimed the lease in the meantime."""
    storage = _FakeStorageProvider()
    session_storage = storage.get_storage(WorkspaceSession)
    engine = InMemoryClaimEngine(
        adapters={
            ClaimKind.SESSION: SessionClaimAdapter(session_storage=session_storage),
        },
    )
    scheduler = InMemoryScheduler()
    await scheduler.initialize()
    pool = WorkerPool(
        config=WorkerConfig(**_CONFIG),
        scheduler=scheduler,
        storage=storage,
        workspace_registry=None,          # type: ignore[arg-type]
        provider_registry=None,           # type: ignore[arg-type]
        engine=engine,
    )
    pool._worker_id = WORKER
    pool._dispatch = {ClaimKind.SESSION: pool._run_engine_session}

    await _seed_resumable_row(session_storage)
    await engine.mark_resumable(ClaimKind.SESSION, SID)
    lease = next(
        l for l in await engine.claim_due(WORKER, max_count=10)
        if l.kind == ClaimKind.SESSION and l.entity_id == SID
    )
    key = (lease.kind, lease.entity_id)

    # The outage: every engine heartbeat from here on fails.
    async def _heartbeat_always_fails(worker_id, kind_ids, **kwargs):
        raise RuntimeError("database unreachable (simulated outage)")

    monkeypatch.setattr(engine, "heartbeat", _heartbeat_always_fails)

    # A peer reclaims the lease while the outage runs: from then on this worker
    # no longer owns the row, and its release must refuse to write over the peer.
    engine._leases[key].claimed_by = PEER

    cancelled = asyncio.Event()
    never = asyncio.Event()

    async def _slow_resume(_lease, _row):
        try:
            await never.wait()
        except asyncio.CancelledError:
            cancelled.set()
            raise

    # The row is resumable, so the pool's resumable branch runs the resume
    # handler, not run_one_session_turn: patch the handler itself.
    monkeypatch.setattr(pool, "_resume_engine_session", _slow_resume)

    # The production dispatch: reserves the in-flight slot (stamping the lease's
    # last-confirmed time at claim) and runs the real session handler.
    pool._reserve_and_dispatch([lease])
    loop_task = asyncio.create_task(pool._heartbeat_loop())
    turn_task = next(iter(pool._turn_tasks))
    try:
        assert key in pool._in_flight and key in pool._lease_last_confirmed, (
            "the claim must stamp the lease's last-confirmed time"
        )
        # Backdate the confirmation past the bound (4.875 s): the next failing tick -
        # one heartbeat interval away - must cancel the scope instead of waiting
        # for the lease to actually expire.
        pool._lease_last_confirmed[key] = asyncio.get_event_loop().time() - 5.0

        await asyncio.wait_for(cancelled.wait(), timeout=6.0)
        await asyncio.wait_for(turn_task, timeout=10.0)

        # The turn stopped, and the release wrote nothing over the peer's row:
        # the adapter never ran (the engine's fence on claimed_by refused it), so
        # the park - and the answer on it - is exactly as it was.
        row = await session_storage.get(SID)
        assert row is not None
        assert row.status == SessionStatus.RUNNING
        assert row.parked_status == "resumable"
        assert (row.parked_state or {}).get("resume_event_payload", {}).get("answer") == ANSWER, (
            "the unwinding turn wrote over the new owner's row (the park was cleared)"
        )
        assert row.turn_no == 1, "a failed release bumped the turn the peer now owns"
        # The local bookkeeping is gone: nothing keeps heartbeat-ing a lease the
        # worker no longer owns.
        assert key not in pool._in_flight and key not in pool._lease_last_confirmed
    finally:
        never.set()
        pool._stopping.set()
        pool._keepalive_done.set()
        loop_task.cancel()
        for t in (turn_task, loop_task):
            try:
                await t
            except (Exception, asyncio.CancelledError):
                pass
        await scheduler.aclose()


@pytest.mark.asyncio
async def test_the_staleness_check_cancels_only_scopes_older_than_the_bound():
    """``_cancel_stale_scopes`` is the failure path's only act: a fresh stamp is left
    alone, a scope that already returned its lease or was cancelled is not pushed
    again, and only a stamp older than ``lease_ttl - heartbeat_interval / 8`` fires
    (review 2026-11-23, 1.6)."""
    pool = WorkerPool(
        config=WorkerConfig(**_CONFIG),
        scheduler=InMemoryScheduler(),
        storage=None,                       # type: ignore[arg-type]
        workspace_registry=None,            # type: ignore[arg-type]
        provider_registry=None,             # type: ignore[arg-type]
        engine=InMemoryClaimEngine(adapters={}),
    )
    pool._worker_id = WORKER
    now = asyncio.get_event_loop().time()
    assert pool._self_cancel_after == 4.875  # the bound: 5 s TTL - 1 s heartbeat / 8

    fresh_key = (ClaimKind.HARNESS, "h-fresh")
    stale_key = (ClaimKind.HARNESS, "h-stale")
    returned_key = (ClaimKind.HARNESS, "h-returned")
    cancelled_key = (ClaimKind.HARNESS, "h-cancelled")
    from primer.worker.turn import _CancelScope

    fresh_scope = _CancelScope()
    stale_scope = _CancelScope()
    returned_scope = _CancelScope()
    returned_scope.mark_lease_returned()
    cancelled_scope = _CancelScope()
    cancelled_scope.cancel("earlier loss")
    pool._active_scopes.update({
        fresh_key: fresh_scope,
        stale_key: stale_scope,
        returned_key: returned_scope,
        cancelled_key: cancelled_scope,
    })
    pool._lease_last_confirmed.update({
        fresh_key: now - 1.0,      # confirmed last tick: inside the bound
        stale_key: now - 5.0,      # unconfirmed past the bound
        returned_key: now - 99.0,  # stale, but the lease is already handed back
        cancelled_key: now - 99.0, # stale, but the cancel already landed
    })

    pool._cancel_stale_scopes()

    assert not fresh_scope.cancelled, "a lease confirmed last tick was cancelled"
    assert stale_scope.cancelled and stale_scope.cancel_reason == "preempted", (
        "a lease unconfirmed past the bound was not cancelled"
    )
    # The two already-settled scopes are not pushed again (no duplicate-cancel noise),
    # and their stamps stay until the key leaves _in_flight.
    assert not returned_scope.cancelled
    assert cancelled_scope.cancel_reason == "earlier loss"  # the first reason wins
    assert set(pool._lease_last_confirmed) == {fresh_key, stale_key, returned_key, cancelled_key}
