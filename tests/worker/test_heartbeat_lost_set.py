"""The heartbeat loop's lost-lease set is computed from the keys it SENT.

``_heartbeat_loop`` sends ``list(self._in_flight)`` to ``engine.heartbeat`` and treats every sent
key the engine does not confirm as a lost lease: it cancels that execution with ``"preempted"``.
It used to diff the CONFIRMED set against the LIVE ``_in_flight`` after the await, so a key
dispatched while the heartbeat's round trip was outstanding (never sent, so never confirmed) was
reported lost and cancelled at its start. For a session that ended in a terminal ERROR record via
the default ``success=False`` outcome. The window is one round trip per heartbeat interval and
widens exactly when the database is slow, which is when a stall also makes leases expire.

A second consequence of reading the live state after the await is pinned too: the lost-lease cancel
must target the scope that was running when the keys were SENT (a key that finished and was
re-dispatched during the round trip owns a new scope that must not be cancelled).

The lost-lease cancel itself stays UNCONDITIONAL (``scope.cancel``, not ``cancel_once``): a lease lost
mid-turn must be able to push a turn that is stuck unwinding, and
``test_cancel_reconcile.py::test_a_lost_lease_still_forces_a_cancel_into_a_turn_that_is_already_unwinding``
pins that.

Every test uses more than one key.
"""

from __future__ import annotations

import asyncio
import time
from datetime import UTC, datetime, timedelta

import pytest

from primer.claim.in_memory import InMemoryClaimEngine
from primer.int.claim import ClaimKind, Lease as ClaimLease
from primer.model.scheduler import WorkerConfig
from primer.scheduler.in_memory import InMemoryScheduler
from primer.worker.pool import WorkerPool

KIND = ClaimKind.HARNESS


def _lease(entity_id: str) -> ClaimLease:
    now = datetime.now(UTC)
    return ClaimLease(
        kind=KIND, entity_id=entity_id, claimed_by="wrk-test", claimed_at=now,
        expires_at=now + timedelta(seconds=30), attempt_count=0, last_error=None,
    )


def _config() -> WorkerConfig:
    return WorkerConfig(
        concurrency=8, claim_batch_size=4, heartbeat_interval_seconds=1,
        lease_ttl_seconds=5, poll_interval_seconds=0.1, drain_timeout_seconds=5,
    )


async def _until(predicate, message: str, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        assert time.monotonic() < deadline, message
        await asyncio.sleep(0.02)


class _Harness:
    """A pool whose handlers block until released, and a scriptable engine.heartbeat."""

    def __init__(self) -> None:
        self.engine = InMemoryClaimEngine(adapters={})
        self.scheduler = InMemoryScheduler()
        self.pool = WorkerPool(
            config=_config(), scheduler=self.scheduler,
            storage=None,  # type: ignore[arg-type]
            workspace_registry=None,  # type: ignore[arg-type]
            provider_registry=None,  # type: ignore[arg-type]
            engine=self.engine,
        )
        self.pool._worker_id = "wrk-test"
        self.started: dict[str, int] = {}
        self.cancels: dict[str, int] = {}
        self.gates: dict[str, asyncio.Event] = {}
        self.pool._dispatch = {KIND: self._handler}
        self.sent: list[list[tuple[ClaimKind, str]]] = []
        self.entered = asyncio.Event()
        self.proceed = asyncio.Event()
        self.confirm = lambda kind_ids: list(kind_ids)
        self.hold_next = False

    async def _handler(self, lease: ClaimLease) -> None:
        key = lease.entity_id
        self.started[key] = self.started.get(key, 0) + 1
        gate = self.gates.setdefault(key, asyncio.Event())
        try:
            await gate.wait()
        except asyncio.CancelledError:
            self.cancels[key] = self.cancels.get(key, 0) + 1
            await self._on_cancel(key)
            raise

    async def _on_cancel(self, key: str) -> None:  # overridden by a test that unwinds slowly
        return None

    async def heartbeat(self, worker_id, kind_ids):
        self.sent.append(list(kind_ids))
        if self.hold_next:
            self.hold_next = False
            self.entered.set()
            await self.proceed.wait()
        return self.confirm(kind_ids)

    def dispatch(self, *ids: str) -> None:
        self.pool._reserve_and_dispatch([_lease(i) for i in ids])

    async def __aenter__(self) -> "_Harness":
        await self.scheduler.initialize()
        self.engine.heartbeat = self.heartbeat  # type: ignore[method-assign]
        self.loop_task = asyncio.create_task(self.pool._heartbeat_loop())
        return self

    async def __aexit__(self, *exc) -> None:
        self.pool._stopping.set()
        self.pool._keepalive_done.set()
        for gate in self.gates.values():
            gate.set()
        self.proceed.set()
        self.loop_task.cancel()
        await asyncio.gather(self.loop_task, return_exceptions=True)
        for task in list(self.pool._turn_tasks):
            task.cancel()
        await asyncio.gather(*self.pool._turn_tasks, return_exceptions=True)
        await self.scheduler.aclose()


@pytest.mark.asyncio
async def test_a_key_dispatched_during_the_heartbeat_round_trip_is_not_cancelled():
    async with _Harness() as h:
        h.dispatch("kept", "lost")
        await _until(lambda: len(h.pool._active_scopes) == 2, "the first two never started")
        h.confirm = lambda kind_ids: [k for k in kind_ids if k[1] != "lost"]
        h.hold_next = True
        await asyncio.wait_for(h.entered.wait(), timeout=4.0)   # a heartbeat is now in flight

        h.dispatch("new")                                       # claimed during the round trip
        await _until(lambda: "new" in h.started, "the new key never started")
        assert (KIND, "new") not in {k for batch in h.sent for k in batch}, "it was never sent"

        h.proceed.set()                                         # the round trip completes
        await _until(lambda: h.cancels.get("lost", 0) >= 1, "the genuinely lost key was not cancelled")
        await asyncio.sleep(0.3)

        assert h.cancels.get("new", 0) == 0, "a key dispatched during the round trip was cancelled"
        assert h.cancels.get("kept", 0) == 0
        assert h.started["new"] == 1


@pytest.mark.asyncio
async def test_a_key_re_dispatched_during_the_round_trip_keeps_its_new_scope():
    """K is in the snapshot, finishes and is dispatched again while the heartbeat is in flight; the
    engine (rightly) does not confirm the old lease. The cancel must hit the OLD scope, not the new."""
    async with _Harness() as h:
        h.dispatch("k", "other")
        await _until(lambda: len(h.pool._active_scopes) == 2, "the first two never started")
        old_scope = h.pool._active_scopes[(KIND, "k")]
        h.confirm = lambda kind_ids: [x for x in kind_ids if x[1] != "k"]
        h.hold_next = True
        await asyncio.wait_for(h.entered.wait(), timeout=4.0)

        h.gates["k"].set()                                      # the old execution finishes ...
        await _until(lambda: (KIND, "k") not in h.pool._in_flight, "the old k never finished")
        h.gates["k"] = asyncio.Event()                          # ... and the next one blocks again
        h.dispatch("k")                                         # ... and k is claimed again
        await _until(lambda: h.started.get("k") == 2, "the second k never started")
        new_scope = h.pool._active_scopes[(KIND, "k")]
        assert new_scope is not old_scope

        h.proceed.set()
        await asyncio.sleep(0.4)

        assert old_scope.cancelled, "the verdict never reached the scope that was sent"
        assert h.cancels.get("k", 0) == 0, "the re-dispatched execution was cancelled"
        assert not new_scope.cancelled
        assert h.cancels.get("other", 0) == 0


@pytest.mark.asyncio
async def test_a_sent_key_with_no_scope_yet_is_caught_on_the_next_tick():
    """A key reserved in ``_in_flight`` whose ``_run_engine`` task has not registered its scope when
    the snapshot is taken has nothing to cancel in THAT round trip (a post-await lookup could not tell
    "started late" from "re-dispatched", which is the bug). It is caught one interval later."""
    from primer.worker.turn import _CancelScope

    async with _Harness() as h:
        h.dispatch("kept")
        await _until(lambda: len(h.pool._active_scopes) == 1, "the first key never started")
        key = (KIND, "pending")
        h.pool._in_flight.add(key)                      # reserved, task not started: no scope yet
        h.confirm = lambda kind_ids: [k for k in kind_ids if k[1] != "pending"]

        await _until(lambda: any(key in batch for batch in h.sent), "the key was never sent", timeout=4.0)
        assert key not in h.pool._active_scopes         # nothing to cancel in that round trip

        late = _CancelScope()                           # the task starts and registers its scope
        h.pool._active_scopes[key] = late
        await _until(lambda: late.cancelled, "the next tick never cancelled it", timeout=4.0)
        assert late.cancel_reason == "preempted"
        assert h.cancels.get("kept", 0) == 0
        h.pool._in_flight.discard(key)
        h.pool._active_scopes.pop(key, None)
