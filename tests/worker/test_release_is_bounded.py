"""``WorkerPool._release_lease`` bounds the release it makes.

Once an execution's scope is marked ``lease_returned`` (PR #330) a lost-lease verdict can no longer push a
release that hangs: a stuck connection after the lease was genuinely lost would otherwise keep the handler
alive until the drain timeout. The release is therefore bounded by ``_release_timeout_seconds`` (the lease
TTL minus one heartbeat interval): on timeout the release is cancelled, counted and logged, the
``TimeoutError`` propagates like any failed release, the key leaves ``_in_flight`` (so nothing heartbeats the
lease and it expires for a peer to re-claim), and an UNRELATED execution's release is untouched. A
``TimeoutError`` raised by the engine itself (a command timeout) is not this bound and is not counted as it.

The bound is no longer than that because a slow release is NOT harmless to the worker's other leases: on
Postgres the heartbeat is one ``UPDATE`` over every in-flight key, including the one being released, and it
waits on the row lock the release holds, so nothing the worker holds is refreshed until the release ends.

Every behavioural test runs two executions.
"""

from __future__ import annotations

import asyncio
import time
from datetime import UTC, datetime, timedelta

import pytest

from primer.claim.in_memory import InMemoryClaimEngine
from primer.int.claim import ClaimKind, ReleaseOutcome
from primer.model.scheduler import WorkerConfig
from primer.scheduler.in_memory import InMemoryScheduler
from primer.worker.pool import WorkerPool

WORKER = "wrk-bounded"


async def _until(predicate, message: str, timeout: float = 5.0) -> None:
    # The deadline is the RUNNING LOOP's clock: on the virtual-time loop below the wall clock does not move while the
    # loop sleeps, so a wall-clock deadline would not bound a wait that never completes; on a real loop
    # ``loop.time()`` is ``time.monotonic()``.
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not predicate():
        assert loop.time() < deadline, message
        await asyncio.sleep(0.02)


class _World:
    def __init__(self, heartbeat: int = 1, clock=None) -> None:
        self.engine = InMemoryClaimEngine(adapters={}, **({} if clock is None else {"clock": clock}))
        self.scheduler = InMemoryScheduler()
        self.pool = WorkerPool(
            config=WorkerConfig(concurrency=4, heartbeat_interval_seconds=heartbeat, lease_ttl_seconds=5),
            scheduler=self.scheduler, storage=None,  # type: ignore[arg-type]
            workspace_registry=None,  # type: ignore[arg-type]
            provider_registry=None,  # type: ignore[arg-type]
            engine=self.engine,
        )
        self.pool._worker_id = WORKER
        self.outcomes: dict[str, str] = {}

        async def releasing(lease) -> None:
            try:
                await self.pool._release_lease(lease, ReleaseOutcome(success=True, drop_lease=True))
                self.outcomes[lease.entity_id] = "released"
            except TimeoutError:
                self.outcomes[lease.entity_id] = "timeout"
                raise

        self.pool._dispatch = {ClaimKind.HARNESS: releasing, ClaimKind.TRIGGER: releasing}

    async def start(self) -> list:
        await self.scheduler.initialize()
        for kind, eid in ((ClaimKind.HARNESS, "stuck"), (ClaimKind.TRIGGER, "fine")):
            await self.engine.upsert(kind, eid)
        leases = await self.engine.claim_due(WORKER, max_count=10)
        assert len(leases) == 2
        return leases

    async def close(self) -> None:
        await self.scheduler.aclose()


@pytest.mark.asyncio
async def test_a_release_that_hangs_is_abandoned_within_the_bound_and_the_other_execution_is_untouched():
    w = _World()
    leases = await w.start()
    real_release = w.engine.release
    hung = asyncio.Event()

    async def release(lease, *, outcome):
        if lease.entity_id == "stuck":
            hung.set()
            await asyncio.Event().wait()   # never returns
        await real_release(lease, outcome=outcome)

    w.engine.release = release  # type: ignore[method-assign]
    w.pool._release_timeout_seconds = 0.3
    try:
        w.pool._reserve_and_dispatch(leases)
        await asyncio.wait_for(hung.wait(), timeout=5.0)
        await _until(lambda: not w.pool._in_flight, "an execution is still in flight: the hung release was not bounded")
        assert w.outcomes == {"stuck": "timeout", "fine": "released"}
        assert w.pool._release_timeouts_total == 1
        assert w.pool.metrics_snapshot()["primer_worker_release_timeouts_total"] == 1
        # the unrelated execution's lease really went back; the stuck one's lease is left to expire
        assert not await w.engine.has_lease(ClaimKind.TRIGGER, "fine")
        assert await w.engine.has_lease(ClaimKind.HARNESS, "stuck")
    finally:
        for task in list(w.pool._turn_tasks):
            task.cancel()
        await asyncio.gather(*w.pool._turn_tasks, return_exceptions=True)
        await w.close()


@pytest.mark.parametrize(
    ("ttl", "heartbeat", "bound"),
    [(5, 1, 4.0), (5, 2, 3.0), (30, 10, 20.0), (300, 60, 240.0)],
)
def test_the_bound_is_one_heartbeat_interval_short_of_the_lease_ttl(ttl, heartbeat, bound):
    """Derived from the config, never longer than the TTL: 3 s at the 5 s minimum TTL (with the 2 s heartbeat the
    validator allows), 20 s at the defaults. It used to be two TTLs, past the point the worker's other leases lapse."""
    pool = WorkerPool(
        config=WorkerConfig(lease_ttl_seconds=ttl, heartbeat_interval_seconds=heartbeat),
        scheduler=InMemoryScheduler(), storage=None,  # type: ignore[arg-type]
        workspace_registry=None,  # type: ignore[arg-type]
        provider_registry=None,  # type: ignore[arg-type]
        engine=InMemoryClaimEngine(adapters={}),
    )
    assert pool._release_timeout_seconds == bound
    assert pool._release_timeout_seconds < ttl


@pytest.mark.parametrize(
    "config",
    [WorkerConfig(), WorkerConfig(lease_ttl_seconds=5, heartbeat_interval_seconds=2),
     WorkerConfig(lease_ttl_seconds=5, heartbeat_interval_seconds=1)],
    ids=["defaults", "minimum-ttl-heartbeat-2", "minimum-ttl-heartbeat-1"],
)
def test_the_probe_after_a_timed_out_release_is_one_heartbeat_and_fits_in_the_ttl_with_the_bound(config):
    """The ``has_lease`` probe that decides a timed-out release's outcome may take one heartbeat interval, so the
    bound plus the probe stay within one lease TTL: at the defaults (TTL 30 s, heartbeat 10 s) and at the 5 s minimum
    TTL the pool tests use."""
    pool = WorkerPool(
        config=config,
        scheduler=InMemoryScheduler(), storage=None,  # type: ignore[arg-type]
        workspace_registry=None,  # type: ignore[arg-type]
        provider_registry=None,  # type: ignore[arg-type]
        engine=InMemoryClaimEngine(adapters={}),
    )
    assert pool._release_probe_timeout_seconds == config.heartbeat_interval_seconds
    assert pool._release_timeout_seconds + pool._release_probe_timeout_seconds <= config.lease_ttl_seconds


class _VirtualTimeLoop(asyncio.SelectorEventLoop):
    """An event loop whose clock moves only when it would otherwise wait, and then straight to the next timer.

    Every timer (``asyncio.sleep``, ``asyncio.timeout``, ``wait_for``) fires exactly at its deadline whatever the
    host's load, and no time passes between two of them, so a race between timers is decided by their deadlines
    alone. Ready callbacks and file descriptors are served first, as on a real loop; with no timer scheduled it
    blocks like one.
    """

    def __init__(self) -> None:
        super().__init__()
        self._virtual_now = 0.0
        real_select = self._selector.select

        def select(timeout=None):
            if timeout is None:
                return real_select(None)
            events = real_select(0)
            if not events and timeout > 0:
                self._virtual_now += timeout
            return events

        self._selector.select = select  # type: ignore[method-assign]

    def time(self) -> float:
        return self._virtual_now


# Far from the wall clock on purpose: a lease the engine stamped from the wall clock instead of the injected one
# is then visibly expired.
_EPOCH = datetime(2100, 1, 1, tzinfo=UTC)


class _LoopClock:
    """The engine's clock: the virtual loop's time as a datetime, or the instant ``pinned`` while it is set."""

    def __init__(self) -> None:
        self.pinned: datetime | None = None

    def __call__(self) -> datetime:
        if self.pinned is not None:
            return self.pinned
        return _EPOCH + timedelta(seconds=asyncio.get_running_loop().time())


@pytest.mark.parametrize(
    ("heartbeat", "after_beat"),
    [(1, 0.0), (2, 1.6)],
    ids=["release-right-after-a-heartbeat", "release-just-before-the-next-heartbeat"],
)
def test_a_release_that_stalls_the_heartbeat_is_abandoned_before_the_workers_other_leases_lapse(
    heartbeat, after_beat,
):
    """The Postgres stall, modelled on the in-memory engine: while the ``stuck`` release is open (it holds its row
    lock) a heartbeat waits for it, and a heartbeat stamps ``expires_at`` from the moment it STARTED (``now()`` is
    the statement's start). The ``other`` execution is still running, so its lease is refreshed only by heartbeats.
    The bound is the one the config derives (TTL 5 s minus the heartbeat), not an override.

    Two alignments. The release starts right after a heartbeat completed (heartbeat 1 s, bound 4 s): ``other`` has a
    full TTL left. Or it starts 1.6 s after one, just before the next 2 s tick (bound 3 s): ``other``'s last refresh
    is 1.6 s old when the release begins, so it lapses 3.4 s into the release and the bound (3 s) must end the
    release, and let the stalled heartbeat land, inside the 0.4 s left. A bound of a whole TTL (5 s) lapses it there.
    (At the exact worst alignment the margin is only the cancel's round trip; see worker-system.md.)

    It runs on a virtual-time loop and the engine reads that loop's time (``InMemoryClaimEngine(clock=...)``), so the
    0.4 s margin is decided by the deadlines, not by how promptly a loaded host fires the bound's timer."""
    with asyncio.Runner(loop_factory=_VirtualTimeLoop) as runner:
        runner.run(_stall_scenario(heartbeat, after_beat))


async def _stall_scenario(heartbeat: int, after_beat: float) -> None:
    clock = _LoopClock()
    w = _World(heartbeat=heartbeat, clock=clock)
    w.engine.lease_ttl_seconds = w.pool.config.lease_ttl_seconds     # what start() pushes to the engine
    leases = await w.start()
    real_release = w.engine.release
    real_heartbeat = w.engine.heartbeat
    lock_free = asyncio.Event()
    lock_free.set()
    beat = asyncio.Event()
    go = asyncio.Event()
    other_gate = asyncio.Event()

    async def release(lease, *, outcome):
        if lease.entity_id != "stuck":
            return await real_release(lease, outcome=outcome)
        lock_free.clear()                    # the release transaction holds the row lock ...
        try:
            await asyncio.Event().wait()     # ... on a connection that never answers
        finally:
            lock_free.set()                  # the cancel rolls it back and the lock goes

    async def heartbeat_(worker_id, kind_ids):
        started = clock()
        await lock_free.wait()               # the one UPDATE waits on the released row's lock
        clock.pinned = started               # and stamps from its start (the in-memory heartbeat does not await)
        try:
            confirmed = await real_heartbeat(worker_id, kind_ids)
        finally:
            clock.pinned = None
        beat.set()
        return confirmed

    async def stuck(lease) -> None:
        await go.wait()
        try:
            await w.pool._release_lease(lease, ReleaseOutcome(success=True, drop_lease=True))
        except TimeoutError:
            w.outcomes[lease.entity_id] = "timeout"
            raise

    async def other(lease) -> None:
        await other_gate.wait()
        await w.pool._release_lease(lease, ReleaseOutcome(success=True, drop_lease=True))

    w.engine.release = release  # type: ignore[method-assign]
    w.engine.heartbeat = heartbeat_  # type: ignore[method-assign]
    w.pool._dispatch = {ClaimKind.HARNESS: stuck, ClaimKind.TRIGGER: other}
    lapsed: list[datetime] = []

    async def watch_other() -> None:
        while True:
            row = w.engine._leases[(ClaimKind.TRIGGER, "fine")]
            if row.expires_at < clock():
                lapsed.append(clock())
            await asyncio.sleep(0.02)

    w.pool._reserve_and_dispatch(leases)
    loop_task = asyncio.create_task(w.pool._heartbeat_loop())
    watcher = asyncio.create_task(watch_other())
    try:
        beat.clear()
        await asyncio.wait_for(beat.wait(), timeout=5.0)
        await asyncio.sleep(after_beat)       # 0: right after a heartbeat refreshed ``other``; 1.6: just before the next
        go.set()
        await _until(
            lambda: lapsed or "stuck" in w.outcomes,
            "the stuck release was never abandoned", timeout=15.0,
        )
        assert not lapsed, "the worker's OTHER lease expired while the release stalled its heartbeat"
        assert w.outcomes == {"stuck": "timeout"}
        beat.clear()
        await asyncio.wait_for(beat.wait(), timeout=5.0)   # the stalled heartbeat (or the next one) lands
        assert not lapsed, "the worker's OTHER lease expired before the stalled heartbeat landed"
        row = w.engine._leases[(ClaimKind.TRIGGER, "fine")]
        assert row.claimed_by == WORKER and row.expires_at > clock()
        assert w.pool._release_timeouts_total == 1
    finally:
        other_gate.set()
        go.set()
        watcher.cancel()
        w.pool._keepalive_done.set()
        loop_task.cancel()
        await asyncio.gather(watcher, loop_task, return_exceptions=True)
        for task in list(w.pool._turn_tasks):
            task.cancel()
        await asyncio.gather(*w.pool._turn_tasks, return_exceptions=True)
        await w.close()


@pytest.mark.asyncio
async def test_a_timeout_raised_by_the_engine_itself_is_not_counted_as_the_bound():
    w = _World()
    leases = await w.start()
    real_release = w.engine.release

    async def release(lease, *, outcome):
        if lease.entity_id == "stuck":
            raise TimeoutError("command timeout from the driver")
        await real_release(lease, outcome=outcome)

    w.engine.release = release  # type: ignore[method-assign]
    w.pool._release_timeout_seconds = 30.0
    try:
        w.pool._reserve_and_dispatch(leases)
        await _until(lambda: not w.pool._in_flight, "executions did not finish")
        assert w.outcomes == {"stuck": "timeout", "fine": "released"}   # the handler saw the TimeoutError either way
        assert w.pool._release_timeouts_total == 0
    finally:
        for task in list(w.pool._turn_tasks):
            task.cancel()
        await asyncio.gather(*w.pool._turn_tasks, return_exceptions=True)
        await w.close()
