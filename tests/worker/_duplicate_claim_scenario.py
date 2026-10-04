"""Shared scenario for the same-worker duplicate-claim tests (in-memory and Postgres engines).

``claim_due`` re-claims any lease whose ``expires_at`` has passed, including this worker's own
claim, so a heartbeat that stalls past the lease TTL while a turn is still running makes the
pool's claim loop claim the same ``(kind, id)`` again. The pool must leave that duplicate
alone. The scenario forces the expiry by hand (what a stalled heartbeat looks like to the
engine), lets the pool's REAL claim loop re-claim, lets a REAL heartbeat run afterwards, and
then checks the in-flight execution was neither preempted nor duplicated and that its release
reached ``on_release``. Two keys: ``dup`` is duplicate-claimed, ``other`` is untouched.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from datetime import datetime
from typing import Any

from primer.int.claim import ClaimKind, Lease as ClaimLease, ReleaseOutcome

LeaseState = tuple[str | None, datetime | None] | None  # (claimed_by, claimed_at); None = no row


async def _until(predicate: Callable[[], Awaitable[bool] | bool], message: str, timeout: float = 5.0):
    deadline = time.monotonic() + timeout
    while True:
        result = predicate()
        if asyncio.iscoroutine(result):
            result = await result
        if result:
            return
        assert time.monotonic() < deadline, message
        await asyncio.sleep(0.02)


async def run_reclaim_scenario(
    *,
    kind: ClaimKind,
    pool: Any,
    engine: Any,
    released: list[str],
    ids: tuple[str, str],
    force_expired: Callable[[ClaimKind, str], Awaitable[None]],
    lease_state: Callable[[ClaimKind, str], Awaitable[LeaseState]],
) -> None:
    dup_id, other_id = ids
    calls: dict[str, list[datetime]] = {dup_id: [], other_id: []}
    cancelled: set[str] = set()
    started = {k: asyncio.Event() for k in calls}
    gates = {k: asyncio.Event() for k in calls}

    async def _handler(lease: ClaimLease) -> None:
        calls[lease.entity_id].append(lease.claimed_at)
        started[lease.entity_id].set()
        try:
            await gates[lease.entity_id].wait()
        except asyncio.CancelledError:
            cancelled.add(lease.entity_id)
            raise
        # The real handlers release from inside the handler, before _run_engine's finally
        # discards the key; mirror that (this is the pool invariant).
        await engine.release(lease, outcome=ReleaseOutcome(success=True, drop_lease=True))

    beats: list[tuple[float, list, list]] = []
    real_heartbeat = engine.heartbeat
    stalled = True  # until the re-claim has happened

    async def _spy_heartbeat(worker_id, kind_ids):
        started_at = time.monotonic()
        if stalled:
            # A stalled heartbeat: it does not refresh the lease (so the forced expiry
            # cannot be undone by a tick landing before the pool's claim poll) and it
            # reports nothing the test could mistake for a post-re-claim confirmation.
            return list(kind_ids)
        confirmed = await real_heartbeat(worker_id, kind_ids)
        beats.append((started_at, list(kind_ids), list(confirmed)))
        return confirmed

    engine.heartbeat = _spy_heartbeat

    try:
        await pool.start()
        pool._dispatch[kind] = _handler
        await engine.upsert(kind, dup_id, priority=100)
        await engine.upsert(kind, other_id, priority=100)
        await asyncio.wait_for(started[dup_id].wait(), timeout=5.0)
        await asyncio.wait_for(started[other_id].wait(), timeout=5.0)

        key = (kind, dup_id)
        original_scope = pool._active_scopes[key]
        state = await lease_state(kind, dup_id)
        assert state is not None and state[0] == pool.worker_id
        claimed_at_before = state[1]

        await force_expired(kind, dup_id)

        async def _reclaimed() -> bool:
            s = await lease_state(kind, dup_id)
            return s is not None and s[1] != claimed_at_before

        await _until(_reclaimed, "the pool never re-claimed the expired lease")
        reclaimed_at = time.monotonic()
        stalled = False  # heartbeats resume: a REAL one must now confirm the in-flight key

        await _until(
            lambda: any(t > reclaimed_at and key in c for t, _, c in beats),
            "no heartbeat confirmed the in-flight key after the re-claim",
        )

        assert len(calls[dup_id]) == 1, "the duplicate claim dispatched a second executor"
        assert dup_id not in cancelled, "the in-flight execution was preempted"
        assert pool._active_scopes[key] is original_scope, "the cancel scope was replaced"
        state = await lease_state(kind, dup_id)
        assert state is not None and state[0] == pool.worker_id, (
            "the duplicate claim's lease was released or requeued"
        )
        assert pool.metrics_snapshot()["primer_worker_duplicate_claims_total"] >= 1
        assert len(calls[other_id]) == 1 and other_id not in cancelled, "the untouched key moved"

        # Release the first execution: its release carries the OLD claimed_at and must still
        # reach on_release; the lease row is then gone and nothing re-runs.
        gates[dup_id].set()
        await _until(lambda: dup_id in released, "the in-flight execution's release was dropped")
        assert released.count(dup_id) == 1
        await _until(
            lambda: _is_gone(lease_state, kind, dup_id), "the released lease row still exists",
        )
        await asyncio.sleep(0.3)
        assert len(calls[dup_id]) == 1, "the entity was re-run after its release"

        gates[other_id].set()
        await _until(lambda: other_id in released, "the untouched key never released")
        assert len(calls[other_id]) == 1
    finally:
        for g in gates.values():
            g.set()
        await pool.drain_and_stop(timeout=5)


async def _is_gone(lease_state, kind, entity_id) -> bool:
    return (await lease_state(kind, entity_id)) is None
