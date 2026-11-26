"""1.5 (review 2026-11-23): turns waiting for a provider's concurrency slot do not hold the worker's claim budget.

A turn queued behind a saturated limiter (a local model at ``max_concurrency`` 1) holds a worker slot and runs nothing.
Eight of them used to fill a worker so no other kind of work could be claimed there. Now a waiting turn does not count
against the budget, up to ``2 x concurrency`` in flight in total; a waiting turn that is granted its slot counts again.
Driven through the real claim loop, the real ``_run_engine`` and the real in-memory limiter (the pool's hook rides a
context variable the limiter reads inside the turn's own task).

Also the claim-time stamp (H-2 round 1 carry-over): taken before ``claim_due``, not after its on_claim hooks.
"""

from __future__ import annotations

import asyncio
from contextlib import suppress
from datetime import UTC, datetime, timedelta

import pytest

from primer.claim.adapters.sessions import SessionClaimAdapter
from primer.claim.in_memory import InMemoryClaimEngine
from primer.claim.statement_stamp import claim_statement_at
from primer.coordinator.in_memory import InMemoryRateLimiter
from primer.int.claim import ClaimKind, Lease as ClaimLease
from primer.model.scheduler import WorkerConfig
from primer.model.workspace_session import AgentSessionBinding, SessionStatus, WorkspaceSession
from primer.scheduler.in_memory import InMemoryScheduler
from primer.worker.pool import WorkerPool
import primer.worker.pool as pool_module
from tests.conftest import _FakeStorageProvider

CONCURRENCY = 2


def _lease(kind: ClaimKind, entity_id: str) -> ClaimLease:
    now = datetime.now(UTC)
    return ClaimLease(
        kind=kind, entity_id=entity_id, claimed_by="w", claimed_at=now,
        expires_at=now + timedelta(seconds=30), attempt_count=0, last_error=None,
    )


class _QueueEngine:
    """Hands out queued leases of the asked kinds, as ``claim_due`` does."""

    def __init__(self) -> None:
        self.queue: list[ClaimLease] = []

    def enqueue(self, kind: ClaimKind, entity_id: str) -> None:
        self.queue.append(_lease(kind, entity_id))

    async def claim_due(self, worker_id, *, max_count, kinds=None, **_kw):
        out = [l for l in self.queue if kinds is None or l.kind in kinds][:max_count]
        for lease in out:
            self.queue.remove(lease)
        return out

    async def heartbeat(self, worker_id, kind_ids, **_kw):
        return list(kind_ids)

    async def release(self, *_a, **_kw):
        return None


class _World:
    """A pool with one slow provider ("slow", ``max_concurrency`` 1) and kinds that never touch it."""

    def __init__(self, concurrency: int = CONCURRENCY, reserved: bool = False) -> None:
        self.engine = _QueueEngine()
        self.limiter = InMemoryRateLimiter()
        self.scheduler = InMemoryScheduler()
        self.started: list[str] = []
        self.gates: dict[str, asyncio.Event] = {}
        self.concurrency = concurrency
        self.reserved = reserved
        self.peak_in_flight = 0

    async def __aenter__(self):
        await self.scheduler.initialize()
        self.pool = WorkerPool(
            config=WorkerConfig(
                concurrency=self.concurrency, poll_interval_seconds=0.1,
                tool_calls_as_claims_enabled=self.reserved,
                tool_call_reserved_concurrency=1 if self.reserved else None,
            ),
            scheduler=self.scheduler, storage=None,  # type: ignore[arg-type]
            workspace_registry=None, provider_registry=None,  # type: ignore[arg-type]
            engine=self.engine,  # type: ignore[arg-type]
        )
        self.pool._worker_id = "w"
        self.pool._dispatch = {
            ClaimKind.SESSION: self._slow_provider_turn,
            ClaimKind.HARNESS: self._other_turn,
            ClaimKind.TOOL_CALL: self._other_turn,
        }
        loop = self.pool._select_claim_loop()
        self.claim_task = asyncio.create_task(loop())
        self.watch_task = asyncio.create_task(self._watch_peak())
        return self

    async def __aexit__(self, *exc):
        self.pool._stopping.set()
        self.pool._wake.set()
        for gate in self.gates.values():
            gate.set()
        for task in (self.claim_task, self.watch_task, *self.pool._turn_tasks):
            task.cancel()
        with suppress(BaseException):
            await asyncio.gather(self.claim_task, self.watch_task, *self.pool._turn_tasks, return_exceptions=True)
        await self.scheduler.aclose()

    async def _watch_peak(self) -> None:
        while True:
            self.peak_in_flight = max(self.peak_in_flight, len(self.pool._in_flight))
            await asyncio.sleep(0.005)

    def gate(self, entity_id: str) -> asyncio.Event:
        return self.gates.setdefault(entity_id, asyncio.Event())

    async def _slow_provider_turn(self, lease: ClaimLease) -> None:
        # What every LLM adapter does inside the turn: take the provider's slot, hold it for the call.
        async with await self.limiter.acquire("slow", max_concurrency=1):
            self.started.append(lease.entity_id)
            await self.gate(lease.entity_id).wait()

    async def _other_turn(self, lease: ClaimLease) -> None:
        self.started.append(lease.entity_id)
        await self.gate(lease.entity_id).wait()

    async def until(self, predicate, what: str, timeout: float = 3.0) -> None:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while not predicate():
            if loop.time() > deadline:
                raise AssertionError(f"timed out waiting for: {what} (in_flight={sorted(self.pool._in_flight)}, "
                                     f"waiting={sorted(self.pool._rate_waiting)}, started={self.started})")
            await asyncio.sleep(0.01)

    async def quiet(self, seconds: float = 0.3) -> None:
        await asyncio.sleep(seconds)


@pytest.mark.asyncio
@pytest.mark.parametrize("reserved", [False, True], ids=["unified_loop", "reserved_loop"])
async def test_two_turns_waiting_on_a_saturated_limiter_leave_room_for_other_work(reserved):
    async with _World(concurrency=CONCURRENCY + 1 if reserved else CONCURRENCY, reserved=reserved) as w:
        # Reserved loop: the general slice is concurrency - 1 = CONCURRENCY, like the unified loop's whole budget.
        hold = await w.limiter.acquire("slow", max_concurrency=1)  # the provider is saturated by someone else
        w.engine.enqueue(ClaimKind.SESSION, "s1")
        w.engine.enqueue(ClaimKind.SESSION, "s2")
        await w.until(lambda: len(w.pool._rate_waiting) == 2, "both session turns wait for the slow provider")
        assert w.started == []

        w.engine.enqueue(ClaimKind.HARNESS, "h1")  # a different provider's work (no limiter)
        w.pool._wake.set()
        await w.until(lambda: "h1" in w.started, "other work is claimed and runs although both slots are taken by waiters")
        await hold.release()


@pytest.mark.asyncio
async def test_the_overcommit_cap_holds():
    async with _World() as w:
        hold = await w.limiter.acquire("slow", max_concurrency=1)
        for i in range(6):
            w.engine.enqueue(ClaimKind.SESSION, f"s{i}")
        for i in range(3):
            w.engine.enqueue(ClaimKind.HARNESS, f"h{i}")
        await w.until(lambda: len(w.pool._in_flight) == 2 * CONCURRENCY, "the worker carries twice its concurrency")
        await w.quiet()
        assert len(w.pool._in_flight) == 2 * CONCURRENCY, "claiming went past the overcommit cap"
        assert w.peak_in_flight <= 2 * CONCURRENCY
        assert len(w.engine.queue) == 9 - 2 * CONCURRENCY
        await hold.release()


@pytest.mark.asyncio
async def test_a_waiting_turn_that_gets_its_slot_counts_against_the_budget_again():
    async with _World() as w:
        hold = await w.limiter.acquire("slow", max_concurrency=1)
        w.engine.enqueue(ClaimKind.SESSION, "s1")
        w.engine.enqueue(ClaimKind.SESSION, "s2")
        await w.until(lambda: len(w.pool._rate_waiting) == 2, "s1 and s2 wait")
        w.engine.enqueue(ClaimKind.HARNESS, "h1")
        w.engine.enqueue(ClaimKind.HARNESS, "h2")
        await w.until(lambda: {"h1", "h2"} <= set(w.started), "h1 and h2 use the freed budget")
        # Both waiters are still credited, the cap is reached: nothing more is claimed.
        w.gate("h1").set()
        w.gate("h2").set()
        await w.until(lambda: len(w.pool._in_flight) == 2, "h1 and h2 finish")
        w.engine.enqueue(ClaimKind.HARNESS, "h3")
        w.engine.enqueue(ClaimKind.HARNESS, "h4")
        await w.until(lambda: {"h3", "h4"} <= set(w.started), "two waiters free two slots again")
        assert len(w.pool._in_flight) == 4

        await hold.release()  # the slow provider admits ONE of the two waiting turns (max_concurrency 1)
        await w.until(lambda: len(w.pool._rate_waiting) == 1, "one waiter is admitted and stops waiting")
        assert len(w.started) == 5  # s? plus h1..h4
        w.gate("h3").set()
        await w.until(lambda: len(w.pool._in_flight) == 3, "h3 finishes")
        w.engine.enqueue(ClaimKind.HARNESS, "h5")
        await w.quiet()
        # s? runs (1) + h4 runs (1) = the full concurrency; the other waiter's credit is the only slack, and the cap leaves none.
        assert "h5" not in w.started, "the admitted turn was still treated as waiting: it must count against the budget again"
        w.gate("h4").set()
        await w.until(lambda: "h5" in w.started, "with a slot free again the queued work is claimed")


@pytest.mark.asyncio
async def test_a_turn_whose_wait_timed_out_and_keeps_running_stops_being_credited():
    # The turn is NOT ended by the cancel (the engine's own cleanup would pop its key and hide a bracket that never
    # reports the end): it gives up its acquire after 0.1 s and goes on running.
    async with _World() as w:
        async def turn(lease):
            assert await w.limiter.try_acquire("slow", max_concurrency=1, timeout_s=0.1) is None
            await w.gate(lease.entity_id).wait()

        w.pool._dispatch[ClaimKind.SESSION] = turn
        hold = await w.limiter.acquire("slow", max_concurrency=1)
        w.engine.enqueue(ClaimKind.SESSION, "s1")
        await w.until(lambda: (ClaimKind.SESSION, "s1") in w.pool._in_flight, "s1 is in flight")
        await w.until(lambda: len(w.pool._rate_waiting) == 1, "s1 waits")
        await w.until(lambda: not w.pool._rate_waiting, "the wait timed out and the credit is gone")
        assert (ClaimKind.SESSION, "s1") in w.pool._in_flight, "the turn must still be running"
        await hold.release()


# ---------------------------------------------------------------------------
# carry-over from the H-2 review: the claim-time stamp is taken before claim_due
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("reserved", [False, True], ids=["unified_loop", "reserved_loop"])
async def test_the_claim_stamp_is_taken_before_claim_due_not_after_its_hooks(reserved):
    claim_lag = 0.5
    loop = asyncio.get_running_loop()

    class _SlowReturnEngine(_QueueEngine):
        async def claim_due(self, worker_id, **kw):
            leases = await super().claim_due(worker_id, **kw)
            if leases:
                claim_statement_at.set(loop.time())  # the statement is done, the hooks follow
                await asyncio.sleep(claim_lag)  # the per-lease on_claim hooks, before the leases are handed out
            return leases

    async with _World(concurrency=CONCURRENCY + 1 if reserved else CONCURRENCY, reserved=reserved) as w:
        w.claim_task.cancel()
        with suppress(asyncio.CancelledError):
            await w.claim_task
        w.engine.__class__ = _SlowReturnEngine
        before = loop.time()
        w.engine.enqueue(ClaimKind.HARNESS, "h1")
        w.claim_task = asyncio.create_task(w.pool._select_claim_loop()())
        await w.until(lambda: "h1" in w.started, "the lease is dispatched")
        stamp = w.pool._lease_last_confirmed[(ClaimKind.HARNESS, "h1")]
        assert stamp < before + claim_lag / 2, f"stamped {stamp - before:.2f}s after the claim began (after its hooks)"


@pytest.mark.asyncio
@pytest.mark.parametrize("reserved", [False, True], ids=["unified_loop", "reserved_loop"])
async def test_the_claim_stamp_excludes_the_wait_for_a_pool_connection(reserved):
    # claim_due waits for a pool connection BEFORE its statement runs. The statement stamps the lease expiry, so a
    # stamp taken before the call would start the watchdog's clock that much early (H3-5).
    pool_wait = 0.5
    loop = asyncio.get_running_loop()

    class _PoolWaitEngine(_QueueEngine):
        async def claim_due(self, worker_id, **kw):
            leases = await super().claim_due(worker_id, **kw)
            if leases:
                await asyncio.sleep(pool_wait)  # the connection wait
                claim_statement_at.set(loop.time())  # then the statement
            return leases

    async with _World(concurrency=CONCURRENCY + 1 if reserved else CONCURRENCY, reserved=reserved) as w:
        w.claim_task.cancel()
        with suppress(asyncio.CancelledError):
            await w.claim_task
        w.engine.__class__ = _PoolWaitEngine
        before = loop.time()
        w.engine.enqueue(ClaimKind.HARNESS, "h1")
        w.claim_task = asyncio.create_task(w.pool._select_claim_loop()())
        await w.until(lambda: "h1" in w.started, "the lease is dispatched")
        stamp = w.pool._lease_last_confirmed[(ClaimKind.HARNESS, "h1")]
        assert stamp >= before + pool_wait * 0.9, f"stamped {stamp - before:.2f}s in: it includes the pool wait"


@pytest.mark.asyncio
async def test_the_real_in_memory_engine_records_the_statement_time_before_its_hooks():
    from primer.int.claim import ClaimAdapter

    loop = asyncio.get_running_loop()
    hook_started: list[float] = []

    class _SlowHookAdapter(ClaimAdapter):
        kind = ClaimKind.HARNESS
        entity_table = "spy"

        def eligibility_sql(self) -> str:
            return "true"

        async def on_release(self, conn, entity_id, *, outcome):
            return None

        async def on_claim(self, conn, lease):
            hook_started.append(loop.time())
            await asyncio.sleep(0.2)

    engine = InMemoryClaimEngine(adapters={ClaimKind.HARNESS: _SlowHookAdapter()})
    await engine.upsert(ClaimKind.HARNESS, "h1")
    claim_statement_at.set(None)
    leases = await engine.claim_due("w", max_count=1)
    assert [l.entity_id for l in leases] == ["h1"]
    stamp = claim_statement_at.get()
    assert stamp is not None and stamp <= hook_started[0], "the stamp must be taken before the on_claim hooks run"


# ---------------------------------------------------------------------------
# carry-over from the H-2 review: a hung worker-row write must not delay the lease heartbeat
# ---------------------------------------------------------------------------


class _HangingRowScheduler(InMemoryScheduler):
    def __init__(self, hang: str) -> None:
        super().__init__()
        self._hang = hang

    async def heartbeat_worker(self, worker_id):
        if self._hang == "heartbeat_worker":
            await asyncio.Event().wait()
        return await super().heartbeat_worker(worker_id)

    async def report_worker_load(self, worker_id, *, in_flight):
        if self._hang == "report_worker_load":
            await asyncio.Event().wait()
        return await super().report_worker_load(worker_id, in_flight=in_flight)


@pytest.mark.asyncio
@pytest.mark.parametrize("hang", ["heartbeat_worker", "report_worker_load"])
async def test_a_hung_worker_row_write_does_not_delay_the_lease_heartbeat(hang):
    h = 1
    loop = asyncio.get_running_loop()
    beats: list[float] = []

    class _Engine(_QueueEngine):
        async def heartbeat(self, worker_id, kind_ids, **kw):
            beats.append(loop.time())
            return list(kind_ids)

    scheduler = _HangingRowScheduler(hang)
    await scheduler.initialize()
    pool = WorkerPool(
        config=WorkerConfig(concurrency=1, heartbeat_interval_seconds=h, lease_ttl_seconds=5),
        scheduler=scheduler, storage=None, workspace_registry=None, provider_registry=None,  # type: ignore[arg-type]
        engine=_Engine(),  # type: ignore[arg-type]
    )
    pool._worker_id = "w"
    key = (ClaimKind.HARNESS, "h1")
    pool._in_flight.add(key)
    pool._lease_last_confirmed[key] = loop.time()
    task = asyncio.create_task(pool._heartbeat_loop())
    try:
        deadline = loop.time() + 14
        while len(beats) < 3 and loop.time() < deadline:
            await asyncio.sleep(0.05)
        assert len(beats) >= 3, f"the lease heartbeat stopped behind the hung {hang}: {len(beats)} beats"
        gaps = [b - a for a, b in zip(beats, beats[1:])]
        # One hung write costs at most its own bound; the next lease heartbeat must still begin with room to land before
        # the watchdog's self-cancel (``_self_cancel_after`` from the previous one).
        assert max(gaps) < pool._self_cancel_after - h / 4, (
            f"a hung {hang} held the next lease heartbeat back by {max(gaps):.2f}s"
        )
    finally:
        pool._keepalive_done.set()
        pool._stopping.set()
        task.cancel()
        with suppress(BaseException):
            await task
        await scheduler.aclose()


# ---------------------------------------------------------------------------
# H-3 round 1
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_busy_worker_actively_runs_up_to_twice_its_concurrency_and_no_more():
    # Probe A of the review: two turns wait on a saturated provider, two other turns take the freed budget, then the
    # provider admits both waiters. Four turns run at once, the documented ceiling of 2 x concurrency; never more.
    async with _World() as w:
        running: set[str] = set()

        async def fast_turn(lease):
            async with await w.limiter.acquire("fast", max_concurrency=2):
                running.add(lease.entity_id)
                await w.gate(lease.entity_id).wait()

        async def other(lease):
            running.add(lease.entity_id)
            await w.gate(lease.entity_id).wait()

        w.pool._dispatch[ClaimKind.SESSION] = fast_turn
        w.pool._dispatch[ClaimKind.HARNESS] = other
        h1 = await w.limiter.acquire("fast", max_concurrency=2)
        h2 = await w.limiter.acquire("fast", max_concurrency=2)
        w.engine.enqueue(ClaimKind.SESSION, "s1")
        w.engine.enqueue(ClaimKind.SESSION, "s2")
        await w.until(lambda: len(w.pool._rate_waiting) == 2, "both session turns wait")
        for i in range(4):
            w.engine.enqueue(ClaimKind.HARNESS, f"h{i}")
        await w.until(lambda: {"h0", "h1"} <= running, "two other turns run in the freed budget")
        await h1.release()
        await h2.release()
        await w.until(lambda: {"s1", "s2"} <= running, "both waiters are granted")
        await w.quiet()
        assert len(running) == 2 * CONCURRENCY == pool_module._OVERCOMMIT_FACTOR * CONCURRENCY
        assert len(w.pool._in_flight) == 2 * CONCURRENCY and w.peak_in_flight <= 2 * CONCURRENCY
        assert len(w.engine.queue) == 2, "nothing past the ceiling is claimed"


@pytest.mark.asyncio
async def test_a_task_that_outlives_its_turn_does_not_leave_a_waiting_credit():
    # Probe B of the review: a turn spawns a task it does not await (stoppable_call's _abandon, a deferred window close)
    # that takes a limiter slot after the turn has returned. That wait is not the worker's turn waiting.
    async with _World() as w:
        spawned: list[asyncio.Task] = []

        async def turn_spawning_a_stray(lease):
            async def later():
                await asyncio.sleep(0.05)  # the turn has returned by now
                async with await w.limiter.acquire("slow", max_concurrency=1):
                    pass

            spawned.append(asyncio.ensure_future(later()))

        w.pool._dispatch[ClaimKind.SESSION] = turn_spawning_a_stray
        hold = await w.limiter.acquire("slow", max_concurrency=1)
        try:
            w.engine.enqueue(ClaimKind.SESSION, "s1")
            await w.until(lambda: spawned and (ClaimKind.SESSION, "s1") not in w.pool._in_flight, "the turn returned")
            await w.quiet(0.3)  # the stray task is now waiting on the limiter
            for i in range(3):
                w.engine.enqueue(ClaimKind.HARNESS, f"h{i}")
            w.pool._wake.set()
            await w.quiet(0.4)
            assert w.pool._rate_waiting == {}, "a finished turn's key was credited as waiting"
            assert len(w.pool._in_flight) == CONCURRENCY, "claimed past the budget on a stale credit"
        finally:
            await hold.release()
            for task in spawned:
                task.cancel()
            await asyncio.gather(*spawned, return_exceptions=True)


@pytest.mark.asyncio
async def test_a_stray_task_from_a_previous_turn_is_not_credited_to_the_next_turn_on_the_same_key():
    # H-3 r1 minor 1: turn 1's stray task (spawn-and-abandon; it inherits turn 1's hook in its own
    # context) starts its limiter wait while turn 2 of the SAME key is running. The key is in flight
    # again, so the in-flight check alone credits it: the credit must be ignored, because the signal
    # carries turn 1's scope, which is no longer the key's active scope.
    async with _World() as w:
        spawned: list[asyncio.Task] = []
        hold = await w.limiter.acquire("slow", max_concurrency=1)  # the provider is saturated
        turn_two_started = asyncio.Event()
        calls = 0

        async def session_turn(lease):
            nonlocal calls
            calls += 1
            if calls == 1:  # turn 1: spawn the stray (it inherits this turn's hook context) and return
                async def stray():
                    await asyncio.sleep(0.2)  # by now the key is claimed again and turn 2 is running
                    async with await w.limiter.acquire("slow", max_concurrency=1):
                        pass

                spawned.append(asyncio.ensure_future(stray()))
            else:  # turn 2: keep the key in flight across the stray's stale signal
                turn_two_started.set()
                await asyncio.sleep(0.5)

        try:
            w.pool._dispatch[ClaimKind.SESSION] = session_turn
            w.engine.enqueue(ClaimKind.SESSION, "s1")
            await w.until(lambda: calls >= 1, "turn 1 started")
            await w.until(lambda: (ClaimKind.SESSION, "s1") not in w.pool._in_flight, "turn 1 returned")
            w.engine.enqueue(ClaimKind.SESSION, "s1")  # the same key claimed again: turn 2
            w.pool._wake.set()
            await w.until(turn_two_started.is_set, "turn 2 of the same key started")
            await w.quiet(0.35)  # the stray's wait has begun (its stale True fired); turn 2 still runs
            assert (ClaimKind.SESSION, "s1") in w.pool._in_flight, "turn 2 must still be running"
            assert w.pool._rate_waiting == {}, "a stale wait credit from turn 1's stray task leaked into turn 2"
            await hold.release()  # the stray is admitted: its stale False fires and must not corrupt the count
            await w.quiet(0.2)
            assert w.pool._rate_waiting == {}, "a stale wait-end from turn 1's stray task corrupted the count"
        finally:
            for task in spawned:
                task.cancel()
            with suppress(BaseException):
                await asyncio.gather(*spawned, return_exceptions=True)


# ---- H3-4: each worker-row write has its own bound, sized so the lease heartbeat margin still holds -----------------


@pytest.mark.parametrize("h,ttl", [(1, 5), (1, 30), (2, 5), (5, 10), (10, 20), (10, 30), (30, 60), (60, 120), (60, 300)])
def test_the_worker_row_write_bounds_leave_the_lease_heartbeat_its_margin(h, ttl):
    pool = WorkerPool(
        config=WorkerConfig(heartbeat_interval_seconds=h, lease_ttl_seconds=ttl),
        scheduler=InMemoryScheduler(), storage=None, workspace_registry=None, provider_registry=None,  # type: ignore[arg-type]
        engine=_QueueEngine(),  # type: ignore[arg-type]
    )
    bound = pool._worker_row_timeout_seconds  # per write
    # The next lease heartbeat begins at most H + 2 bounds after the previous began; a quarter interval is left for it
    # to answer before the watchdog's self-cancel.
    assert h + 2 * bound + h / 4 <= pool._self_cancel_after + 1e-9
    # And the bound is not tighter than it has to be: the default config gets the 2 s floor.
    assert bound >= min(max(h / 2, 2.0), (ttl - 1.375 * h) / 2) - 1e-9


@pytest.mark.asyncio
async def test_a_slow_but_answering_worker_row_write_still_stamps_the_row():
    # H = 2: the old shared bound was H/4 = 0.5 s, so a database that answered in 1 s never got a liveness stamp.
    stamped: list[str] = []

    class _SlowScheduler(InMemoryScheduler):
        async def heartbeat_worker(self, worker_id):
            await asyncio.sleep(1.0)
            stamped.append("heartbeat_worker")

        async def report_worker_load(self, worker_id, *, in_flight):
            await asyncio.sleep(1.0)
            stamped.append("report_worker_load")

    scheduler = _SlowScheduler()
    await scheduler.initialize()
    pool = WorkerPool(
        config=WorkerConfig(concurrency=1, heartbeat_interval_seconds=2, lease_ttl_seconds=10),
        scheduler=scheduler, storage=None, workspace_registry=None, provider_registry=None,  # type: ignore[arg-type]
        engine=_QueueEngine(),  # type: ignore[arg-type]
    )
    pool._worker_id = "w"
    task = asyncio.create_task(pool._heartbeat_loop())
    try:
        deadline = asyncio.get_running_loop().time() + 8
        while len(stamped) < 2 and asyncio.get_running_loop().time() < deadline:
            await asyncio.sleep(0.05)
        assert stamped[:2] == ["heartbeat_worker", "report_worker_load"], stamped
    finally:
        pool._keepalive_done.set()
        pool._stopping.set()
        task.cancel()
        with suppress(BaseException):
            await task
        await scheduler.aclose()


# ---- H-3 r1 minor 4: the worker-row writes run off the lease-heartbeat path -------------------------------


@pytest.mark.asyncio
async def test_the_worker_row_writes_do_not_hold_back_the_lease_heartbeat():
    # Every DB call is slow: a worker-row write that answers in 1.6 s (under its bound) used to sit
    # INLINE in the heartbeat tick (two of them), so the next lease heartbeat came H + 2 x 1.6 s
    # later and the watchdog margin narrowed with the slowness. The writes now run on their own
    # task off the lease path (a pass still running when the next tick fires is skipped, not
    # queued), so the lease heartbeat keeps its period no matter how slow the registry is.
    H = 2.0
    lease_beats: list[float] = []
    running = 0
    peak_running = 0

    class _Engine:
        async def heartbeat(self, worker_id, kind_ids, **_kw):
            lease_beats.append(asyncio.get_event_loop().time())
            return list(kind_ids)

        async def release(self, *_a, **_kw):
            return None

    class _SlowScheduler(InMemoryScheduler):
        async def heartbeat_worker(self, worker_id):
            nonlocal running, peak_running
            running += 1
            peak_running = max(peak_running, running)
            try:
                await asyncio.sleep(1.6)  # slower than the tick would allow inline
            finally:
                running -= 1

        async def report_worker_load(self, worker_id, *, in_flight):
            nonlocal running, peak_running
            running += 1
            peak_running = max(peak_running, running)
            try:
                await asyncio.sleep(1.6)
            finally:
                running -= 1

    scheduler = _SlowScheduler()
    await scheduler.initialize()
    pool = WorkerPool(
        config=WorkerConfig(concurrency=1, heartbeat_interval_seconds=H, lease_ttl_seconds=20),
        scheduler=scheduler, storage=None, workspace_registry=None, provider_registry=None,  # type: ignore[arg-type]
        engine=_Engine(),  # type: ignore[arg-type]
    )
    pool._worker_id = "w"
    pool._in_flight.add((ClaimKind.SESSION, "s1"))  # the lease heartbeat runs only for in-flight keys
    task = asyncio.create_task(pool._heartbeat_loop())
    try:
        await asyncio.sleep(8.5)
        assert len(lease_beats) >= 3, f"the lease heartbeat barely ran: {lease_beats}"
        gaps = [b - a for a, b in zip(lease_beats, lease_beats[1:])]
        assert max(gaps) < H + 1.0, (
            f"a slow registry held the lease heartbeat back: gaps {gaps} "
            f"(inline writes would give ~{H + 3.2:g} s)"
        )
        assert peak_running == 1, "a still-running write pass must be skipped, not queued"
    finally:
        pool._keepalive_done.set()
        pool._stopping.set()
        task.cancel()
        with suppress(BaseException):
            await task
        if getattr(pool, "_worker_row_write_task", None) is not None and not pool._worker_row_write_task.done():
            pool._worker_row_write_task.cancel()
            with suppress(BaseException):
                await pool._worker_row_write_task
        await scheduler.aclose()
