"""Integration test: WorkerPool with InMemoryClaimEngine dispatches all three
claim kinds (session, chat, harness) via the single unified claim loop."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import pytest

from matrix.claim.in_memory import InMemoryClaimEngine
from matrix.int.claim import ClaimKind, Lease as ClaimLease, ReleaseOutcome
from matrix.int.scheduler import Scheduler
from matrix.model.scheduler import WorkerConfig
from matrix.worker.pool import WorkerPool


# ---------------------------------------------------------------------------
# Minimal stubs so WorkerPool starts without a real storage/workspace stack.
# ---------------------------------------------------------------------------


class _NullScheduler(Scheduler):
    """Minimal no-op Scheduler for integration tests that use ClaimEngine.

    The engine path doesn't call scheduler.claim/claim_chats/claim_harnesses,
    but the pool still calls register_worker, heartbeat_worker, and
    drain_worker/deregister_worker on lifecycle boundaries.  All are no-ops
    here.  watch_cancel returns an empty generator so the cancel loop spins
    down cleanly on drain.
    """

    async def initialize(self) -> None:
        pass

    async def aclose(self) -> None:
        pass

    async def register_worker(self, *, worker_id, host, pid, capacity) -> None:
        pass

    async def heartbeat_worker(self, worker_id: str) -> None:
        pass

    async def drain_worker(self, worker_id: str) -> None:
        pass

    async def deregister_worker(self, worker_id: str) -> None:
        pass

    # --- methods required by Scheduler ABC (not called in engine mode) ---

    async def enqueue(self, session_id, *, ready_at=None):
        pass

    async def claim(self, worker_id, *, max_count):
        return []

    async def heartbeat_leases(self, worker_id, session_ids):
        return []

    async def complete_turn(self, worker_id, session_id, **kwargs):
        from matrix.int.scheduler import CompleteTurnResult
        return CompleteTurnResult.OK

    async def park_turn(
        self, worker_id, session_id, *, expected_turn_no,
        parked_event_key, parked_until, parked_at, parked_state,
    ):
        from matrix.int.scheduler import CompleteTurnResult
        return CompleteTurnResult.OK

    async def clear_park(self, session_id):
        pass

    async def mark_resumable(self, event_key, *, resume_event_payload):
        return 0

    async def claim_chats(self, worker_id, *, max_count, **kwargs):
        return []

    async def heartbeat_chat(self, chat_id, worker_id):
        return False

    async def release_chat(self, chat_id, worker_id, *, next_turn_status):
        pass

    async def claim_harnesses(self, worker_id, *, max_count, **kwargs):
        return []

    async def heartbeat_harness(self, harness_id, worker_id):
        return False

    async def release_harness(
        self, harness_id, worker_id, *, next_status, last_operation_error=None,
    ):
        pass

    async def list_workers(self):
        return []

    async def signal_cancel(self, session_id):
        pass

    def watch_ready(self, worker_id: str):
        # Empty async generator — engine bus loop handles wakeup.
        async def _gen():
            if False:
                yield  # type: ignore[misc]
        return _gen()

    def watch_cancel(self, worker_id: str):
        # Empty async generator — no cancel signals in these tests.
        async def _gen():
            if False:
                yield  # type: ignore[misc]
        return _gen()


# ---------------------------------------------------------------------------
# Integration test
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_engine_claim_loop_dispatches_all_three_kinds():
    """Boot a WorkerPool with InMemoryClaimEngine; seed one lease per kind;
    assert all three dispatch through the single unified claim loop."""

    dispatched: list[tuple[ClaimKind, str]] = []

    # InMemoryClaimEngine with empty adapters (no on_release side-effects).
    engine = InMemoryClaimEngine(adapters={})

    pool = WorkerPool(
        config=WorkerConfig(
            concurrency=4,
            claim_batch_size=3,
            heartbeat_interval_seconds=1,
            poll_interval_seconds=0.1,
        ),
        scheduler=_NullScheduler(),
        storage=None,               # type: ignore[arg-type]
        workspace_registry=None,    # type: ignore[arg-type]
        provider_registry=None,     # type: ignore[arg-type]
        engine=engine,
    )

    # Stub the per-kind handlers to record dispatch and release the lease.
    async def _record_and_release(lease: ClaimLease) -> None:
        dispatched.append((lease.kind, lease.entity_id))
        await engine.release(lease, outcome=ReleaseOutcome(success=True, drop_lease=True))

    await pool.start()
    # Override dispatch table AFTER start() populates it.
    pool._dispatch = {
        ClaimKind.SESSION: _record_and_release,
        ClaimKind.CHAT:    _record_and_release,
        ClaimKind.HARNESS: _record_and_release,
    }

    try:
        # Seed one lease per kind.
        await engine.upsert(ClaimKind.SESSION, "sess-1", priority=10)
        await engine.upsert(ClaimKind.CHAT,    "chat-1", priority=20)
        await engine.upsert(ClaimKind.HARNESS, "harn-1", priority=30)

        # Wait for all three to be dispatched (poll up to 2 s).
        for _ in range(100):
            if len(dispatched) >= 3:
                break
            await asyncio.sleep(0.02)

    finally:
        await pool.drain_and_stop(timeout=2.0)

    assert len(dispatched) == 3, (
        f"expected 3 dispatched, got {len(dispatched)}: {dispatched}"
    )
    kinds_seen = {k for k, _ in dispatched}
    assert kinds_seen == {ClaimKind.SESSION, ClaimKind.CHAT, ClaimKind.HARNESS}, (
        f"not all kinds dispatched: {kinds_seen}"
    )
    entity_ids_seen = {eid for _, eid in dispatched}
    assert entity_ids_seen == {"sess-1", "chat-1", "harn-1"}, (
        f"unexpected entity_ids: {entity_ids_seen}"
    )


@pytest.mark.asyncio
async def test_engine_claim_loop_respects_concurrency():
    """In-flight counter limits concurrency: with max_concurrency=2 and
    three leases seeded simultaneously, at most 2 are claimed at once."""

    max_concurrent = 0
    current_concurrent = 0

    engine = InMemoryClaimEngine(adapters={})
    engine_ref = engine  # capture for assertions

    pool = WorkerPool(
        config=WorkerConfig(
            concurrency=2,
            claim_batch_size=3,
            heartbeat_interval_seconds=1,
            poll_interval_seconds=0.1,
        ),
        scheduler=_NullScheduler(),
        storage=None,               # type: ignore[arg-type]
        workspace_registry=None,    # type: ignore[arg-type]
        provider_registry=None,     # type: ignore[arg-type]
        engine=engine,
    )

    done_event = asyncio.Event()
    done_count = 0

    async def _slow_handler(lease: ClaimLease) -> None:
        nonlocal max_concurrent, current_concurrent, done_count
        current_concurrent += 1
        max_concurrent = max(max_concurrent, current_concurrent)
        # Simulate some work.
        await asyncio.sleep(0.05)
        current_concurrent -= 1
        done_count += 1
        await engine_ref.release(lease, outcome=ReleaseOutcome(success=True, drop_lease=True))
        if done_count >= 3:
            done_event.set()

    await pool.start()
    pool._dispatch = {
        ClaimKind.SESSION: _slow_handler,
        ClaimKind.CHAT:    _slow_handler,
        ClaimKind.HARNESS: _slow_handler,
    }

    try:
        await engine.upsert(ClaimKind.SESSION, "sess-x", priority=10)
        await engine.upsert(ClaimKind.CHAT,    "chat-x", priority=10)
        await engine.upsert(ClaimKind.HARNESS, "harn-x", priority=10)

        await asyncio.wait_for(done_event.wait(), timeout=5.0)
    finally:
        await pool.drain_and_stop(timeout=2.0)

    assert max_concurrent <= 2, (
        f"concurrency violated: max_concurrent={max_concurrent} > 2"
    )
    assert done_count == 3


@pytest.mark.asyncio
async def test_engine_bus_loop_wakes_claim_loop_on_upsert():
    """watch_ready on the engine wakes the claim loop, so a late upsert
    (after the loop is sleeping on the poll timeout) still gets picked up
    without waiting for the full poll interval."""

    dispatched_at: list[float] = []

    engine = InMemoryClaimEngine(adapters={})

    pool = WorkerPool(
        config=WorkerConfig(
            concurrency=2,
            claim_batch_size=2,
            heartbeat_interval_seconds=1,
            poll_interval_seconds=30.0,  # long poll — bus should wake faster
        ),
        scheduler=_NullScheduler(),
        storage=None,               # type: ignore[arg-type]
        workspace_registry=None,    # type: ignore[arg-type]
        provider_registry=None,     # type: ignore[arg-type]
        engine=engine,
    )

    import time

    async def _record(lease: ClaimLease) -> None:
        dispatched_at.append(time.monotonic())
        await engine.release(lease, outcome=ReleaseOutcome(success=True, drop_lease=True))

    await pool.start()
    pool._dispatch = {
        ClaimKind.SESSION: _record,
        ClaimKind.CHAT:    _record,
        ClaimKind.HARNESS: _record,
    }

    try:
        t0 = asyncio.get_event_loop().time()
        # Give the claim loop time to go to sleep on poll_interval (10 s).
        await asyncio.sleep(0.05)
        await engine.upsert(ClaimKind.CHAT, "chat-wake", priority=10)

        for _ in range(50):
            if dispatched_at:
                break
            await asyncio.sleep(0.02)

    finally:
        await pool.drain_and_stop(timeout=2.0)

    assert dispatched_at, "lease was never dispatched"
    elapsed = dispatched_at[0] - t0
    # Should be dispatched well within the 10 s poll interval.
    assert elapsed < 5.0, (
        f"dispatch took {elapsed:.2f}s — bus wakeup didn't work"
    )
