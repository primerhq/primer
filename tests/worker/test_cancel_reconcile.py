"""Cancel reconciliation: a user cancel must reach a running turn even if its NOTIFY was lost.

``cancel_session`` records ``cancel_requested`` on the session row and sends a
``session_cancel`` NOTIFY; the NOTIFY is what lets ``WorkerPool._cancel_loop`` hard-preempt
a turn that is blocked in a long LLM or tool call. NOTIFY is not durable, so a cancel sent
while the watcher is reconnecting, while a half-open connection has not been detected yet
(up to 90s), or at startup before the first LISTEN, never reached the worker: the API said
200 and the turn kept running until it next yielded an event. The row is the truth, so a
separate pool loop now re-reads ``cancel_requested`` for the sessions this worker is running
every ``heartbeat_interval_seconds`` and cancels the ones that are set.

Two things are tested here that are easy to get wrong:

* A SECOND cancel landing while a session is being converged to ENDED skips the
  convergence and strands it (running, lease dropped). A double-clicked Cancel could do that
  already, and a reconciler would make it likelier, so scope cancels are de-duplicated.
* The reconcile read must never run inside ``_heartbeat_loop``: a storage read can block up
  to ``command_timeout`` (30s), which would delay lease heartbeats past the 30s TTL.

No database needed. The live-Postgres twin is test_cancel_reconcile_live.py.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import patch

import pytest

import primer.worker.pool as pool_module
from primer.claim.in_memory import InMemoryClaimEngine
from primer.int.claim import ClaimKind, Lease, ReleaseOutcome
from primer.model.scheduler import WorkerConfig
from primer.model.workspace_session import SessionStatus, WorkspaceSession
from primer.scheduler.in_memory import InMemoryScheduler
from primer.worker.pool import WorkerPool
from primer.worker.turn import _CancelScope

SESSION = ClaimKind.SESSION


def _row(*, cancel_requested: bool, status=SessionStatus.RUNNING):
    return SimpleNamespace(cancel_requested=cancel_requested, status=status)


class _Notify:
    """A scheduler whose cancel channel is a queue the test feeds (one entry per NOTIFY)."""

    def __init__(self) -> None:
        self.queue: asyncio.Queue[str] = asyncio.Queue()

    def watch_cancel(self, worker_id):
        async def gen():
            while True:
                yield await self.queue.get()

        return gen()


def _pool(rows: dict | None = None, *, interval: int = 1, scheduler=None) -> WorkerPool:
    pool = WorkerPool(
        config=WorkerConfig(concurrency=8, heartbeat_interval_seconds=interval),
        scheduler=scheduler,                            # type: ignore[arg-type]
        storage=None,                                   # type: ignore[arg-type]
        workspace_registry=None,                        # type: ignore[arg-type]
        provider_registry=None,                         # type: ignore[arg-type]
        engine=None,                                    # type: ignore[arg-type]
    )
    pool._worker_id = "wrk-test"
    pool.loads = []                                     # type: ignore[attr-defined]
    rows = rows if rows is not None else {}

    async def load(sid):
        pool.loads.append(sid)                          # type: ignore[attr-defined]
        row = rows[sid]
        if isinstance(row, Exception):
            raise row
        return row

    pool._load_session = load                           # type: ignore[method-assign]
    return pool


_LIVE: list[tuple[asyncio.Task, asyncio.Event]] = []


@pytest.fixture(autouse=True)
async def _release_every_turn():
    """Whatever a test leaves behind (an assertion that failed before its own cleanup), set
    every gate and cancel every turn, so a RED run reports failures instead of hanging."""
    yield
    for _task, gate in _LIVE:
        gate.set()
    for task, _gate in _LIVE:
        task.cancel()
    await asyncio.gather(*(t for t, _ in _LIVE), return_exceptions=True)
    _LIVE.clear()


async def _turn(pool: WorkerPool, sid: str, *, kind=SESSION, unwinding: bool = False):
    """Register a scope and run a 'turn' on it. With ``unwinding`` the turn does not finish
    when cancelled: like the session handler converging a preempted session, it stays inside
    its CancelledError handler (awaiting storage) until ``gate`` is set. That is the window a
    second cancel lands in. Returns (scope, task, unwinding_event, gate)."""
    scope = _CancelScope()
    pool._active_scopes[(kind, sid)] = scope
    started, in_handler, gate = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def turn():
        async with scope:
            started.set()
            try:
                await asyncio.sleep(3600)
            except asyncio.CancelledError:
                if not unwinding:
                    raise
                in_handler.set()
                await gate.wait()
                raise

    task = asyncio.create_task(turn())
    _LIVE.append((task, gate))
    await started.wait()
    return scope, task, in_handler, gate


async def _wait(predicate, what: str, timeout: float = 3.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while not predicate():
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError(f"timed out after {timeout}s waiting for: {what}")
        await asyncio.sleep(0.01)


async def _stop(*tasks: asyncio.Task) -> None:
    for t in tasks:
        t.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)


# ---- _CancelScope: cancel_once ---------------------------------------------


async def test_cancel_once_cancels_the_first_time_and_is_a_no_op_after():
    pool = _pool()
    scope, task, in_handler, gate = await _turn(pool, "s", unwinding=True)

    assert scope.cancel_once("user_signal") is True
    await in_handler.wait()
    assert scope.cancel_once("user_signal") is False        # the turn is already unwinding

    assert task.cancelling() == 1, "a second cancel was delivered to a turn that was already unwinding"
    assert scope.cancelled and scope.cancel_reason == "user_signal"
    gate.set()
    await _stop(task)


async def test_cancel_stays_unconditional_for_the_forced_paths():
    """Lease loss ("preempted") and the drain timeout must still be able to force a second
    cancel into a turn that is stuck unwinding."""
    pool = _pool()
    scope, task, in_handler, gate = await _turn(pool, "s", unwinding=True)

    scope.cancel("user_signal")
    await in_handler.wait()
    scope.cancel("worker_drain_timeout")

    assert task.cancelling() == 2
    assert scope.cancel_reason == "user_signal"             # the first reason is kept
    gate.set()
    await _stop(task)


# ---- _cancel_loop: a double NOTIFY ------------------------------------------


async def test_a_double_clicked_cancel_is_delivered_to_the_turn_once():
    notify = _Notify()
    pool = _pool(scheduler=notify)
    scope, task, in_handler, gate = await _turn(pool, "s", unwinding=True)
    loop = asyncio.create_task(pool._cancel_loop())
    try:
        await notify.queue.put("s")
        await in_handler.wait()
        await notify.queue.put("s")                         # the second click
        await _wait(lambda: notify.queue.empty(), "the second NOTIFY to be consumed")
        await asyncio.sleep(0.05)

        assert task.cancelling() == 1
    finally:
        gate.set()
        await _stop(loop, task)


# ---- the real strand: a second cancel inside the convergence handler -----------


class _Store:
    def __init__(self, gate: asyncio.Event) -> None:
        self.gets = 0
        self.gate = gate
        self.in_convergence = asyncio.Event()

    async def get(self, sid):
        self.gets += 1
        if self.gets >= 2:                  # the get() inside the preempt-convergence handler
            self.in_convergence.set()
            await self.gate.wait()
        return SimpleNamespace(
            id=sid, parked_status=None, cancel_requested=True, status=SessionStatus.RUNNING,
        )


async def test_a_second_cancel_no_longer_strands_a_session_being_ended():
    """The REAL WorkerPool._run_engine_session, storage stubbed. On a preempt it re-reads the
    row and ends the session; a second cancel landing in that await used to skip _end_session
    and release the lease with success=False, leaving the session RUNNING and unclaimable."""
    gate = asyncio.Event()
    store = _Store(gate)
    notify = _Notify()
    released: list[ReleaseOutcome] = []
    ended: list[str] = []

    class Engine:
        async def release(self, lease, *, outcome):
            released.append(outcome)

    pool = WorkerPool(
        config=WorkerConfig(concurrency=1), scheduler=notify,      # type: ignore[arg-type]
        storage=SimpleNamespace(get_storage=lambda _cls: store),   # type: ignore[arg-type]
        workspace_registry=None, provider_registry=None, engine=Engine(),   # type: ignore[arg-type]
    )
    pool._worker_id = "wrk-test"

    async def fake_end_session(session, *, reason):
        ended.append(reason)
        return ReleaseOutcome(success=True, drop_lease=True)

    async def no_rearm(sid):
        return None

    async def stuck_turn(lease, deps):
        await asyncio.Event().wait()                # a turn blocked in a long LLM or tool call

    pool._end_session = fake_end_session            # type: ignore[method-assign]
    pool._maybe_rearm_session = no_rearm            # type: ignore[method-assign]
    lease = Lease(
        kind=SESSION, entity_id="sess-1", claimed_by="w", claimed_at=datetime.now(timezone.utc),
        expires_at=datetime.now(timezone.utc), attempt_count=0, last_error=None,
    )
    loop = asyncio.create_task(pool._cancel_loop())
    with patch.object(pool_module, "run_one_session_turn", stuck_turn):
        run = asyncio.create_task(pool._run_engine(lease, pool._run_engine_session))
        try:
            await _wait(lambda: (SESSION, "sess-1") in pool._active_scopes and store.gets >= 1, "the turn to start")
            await asyncio.sleep(0.05)
            await notify.queue.put("sess-1")                 # the first click
            await asyncio.wait_for(store.in_convergence.wait(), 3.0)
            await notify.queue.put("sess-1")                 # the second click, inside the convergence
            await _wait(lambda: notify.queue.empty(), "the second NOTIFY to be consumed")
            await asyncio.sleep(0.05)
            gate.set()
            await asyncio.wait_for(run, 3.0)
        finally:
            await _stop(loop)

    assert ended == ["cancelled"], "the second cancel skipped ending the session"
    assert released and released[0].success is True


# ---- _reconcile_cancels ------------------------------------------------------


async def test_it_cancels_a_running_session_whose_row_says_cancel():
    pool = _pool({"s": _row(cancel_requested=True)})
    scope, task, *_ = await _turn(pool, "s")

    assert await pool._reconcile_cancels() == 1

    await _stop(task)
    assert scope.cancelled and scope.cancel_reason == "user_signal"
    assert pool.metrics_snapshot()["primer_worker_cancels_reconciled_total"] == 1


@pytest.mark.parametrize(
    "row",
    [
        pytest.param(_row(cancel_requested=False), id="no-cancel-requested"),
        pytest.param(_row(cancel_requested=True, status=SessionStatus.ENDED), id="already-ended"),
        pytest.param(None, id="row-gone"),
    ],
)
async def test_it_leaves_a_session_alone_unless_a_cancel_is_pending(row):
    pool = _pool({"s": row})
    scope, task, *_ = await _turn(pool, "s")

    assert await pool._reconcile_cancels() == 0

    assert not scope.cancelled and not task.done()
    assert pool.metrics_snapshot()["primer_worker_cancels_reconciled_total"] == 0
    await _stop(task)


async def test_it_only_looks_at_session_scopes():
    pool = _pool({"h": _row(cancel_requested=True), "t": _row(cancel_requested=True)})
    _, t1, *_ = await _turn(pool, "h", kind=ClaimKind.HARNESS)
    _, t2, *_ = await _turn(pool, "t", kind=ClaimKind.TRIGGER)

    assert await pool._reconcile_cancels() == 0

    assert pool.loads == []                                  # never even read a row for them
    await _stop(t1, t2)


async def test_a_scope_that_is_already_being_cancelled_is_not_read_or_cancelled_again():
    pool = _pool({"s": _row(cancel_requested=True)})
    scope, task, in_handler, gate = await _turn(pool, "s", unwinding=True)
    scope.cancel_once("user_signal")                         # the normal NOTIFY path got there first
    await in_handler.wait()

    assert await pool._reconcile_cancels() == 0

    assert pool.loads == []
    assert task.cancelling() == 1
    gate.set()
    await _stop(task)


async def test_a_storage_error_for_one_session_does_not_stop_the_others(caplog):
    pool = _pool({"bad": RuntimeError("db down"), "good": _row(cancel_requested=True)})
    _, t_bad, *_ = await _turn(pool, "bad")
    good_scope, t_good, *_ = await _turn(pool, "good")

    with caplog.at_level(logging.DEBUG, logger="primer.worker.pool"):
        assert await pool._reconcile_cancels() == 1

    assert good_scope.cancelled
    assert any("bad" in r.getMessage() and r.levelno == logging.WARNING for r in caplog.records)
    await _stop(t_bad, t_good)


async def test_a_turn_that_ends_while_its_row_is_being_read_is_not_counted():
    """The scope is gone from _active_scopes by the time the read returns: nothing to cancel."""
    pool = _pool()
    scope, task, *_ = await _turn(pool, "s")

    async def load_then_finish(sid):
        pool._active_scopes.pop((SESSION, sid), None)         # the turn finished meanwhile
        return _row(cancel_requested=True)

    pool._load_session = load_then_finish                    # type: ignore[method-assign]

    assert await pool._reconcile_cancels() == 0

    assert not scope.cancelled
    assert pool.metrics_snapshot()["primer_worker_cancels_reconciled_total"] == 0
    await _stop(task)


# ---- the loop ------------------------------------------------------------------


class _Patched:
    """A module stand-in that forwards everything but the named overrides."""

    def __init__(self, real, **overrides):
        self._real, self._overrides = real, overrides

    def __getattr__(self, name):
        return self._overrides[name] if name in self._overrides else getattr(self._real, name)


def _fast_sleep(monkeypatch, pool: WorkerPool, ticks: int) -> list[float]:
    """Make the pool module's asyncio.sleep instant and recorded; stop the pool after ``ticks`` sleeps."""
    real_sleep = asyncio.sleep
    delays: list[float] = []

    async def fake_sleep(delay, *args, **kwargs):
        delays.append(delay)
        if len(delays) >= ticks:
            # the heartbeat and cancel loops outlive _stopping (they keep leases alive through a drain)
            pool._stopping.set()
            pool._keepalive_done.set()
        await real_sleep(0)

    monkeypatch.setattr(pool_module, "asyncio", _Patched(asyncio, sleep=fake_sleep))
    return delays


async def test_the_loop_reconciles_every_heartbeat_interval(monkeypatch):
    pool = _pool({"s": _row(cancel_requested=True)}, interval=7)
    scope, task, *_ = await _turn(pool, "s")
    delays = _fast_sleep(monkeypatch, pool, ticks=3)

    await asyncio.wait_for(pool._cancel_reconcile_loop(), 3.0)

    assert delays == [7, 7, 7]                      # the cadence follows heartbeat_interval_seconds
    assert scope.cancelled
    assert pool.metrics_snapshot()["primer_worker_cancels_reconciled_total"] == 1
    await _stop(task)


async def test_the_loop_survives_a_failing_reconcile(monkeypatch):
    pool = _pool()
    calls = []

    async def boom():
        calls.append(1)
        raise RuntimeError("unexpected")

    pool._reconcile_cancels = boom                  # type: ignore[method-assign]
    _fast_sleep(monkeypatch, pool, ticks=4)

    await asyncio.wait_for(pool._cancel_reconcile_loop(), 3.0)

    assert len(calls) == 3                          # it kept ticking after the first failure


async def test_the_heartbeat_loop_never_reconciles(monkeypatch):
    """A reconcile read can block up to command_timeout (30s). Inside the heartbeat loop that
    would delay lease heartbeats past the 30s TTL, so it must be its own task."""
    class Scheduler:
        async def heartbeat_worker(self, worker_id):
            return None

        async def report_worker_load(self, worker_id, *, in_flight):
            return None

    pool = _pool(scheduler=Scheduler())
    calls = []

    async def reconcile():
        calls.append(1)
        return 0

    pool._reconcile_cancels = reconcile             # type: ignore[method-assign]
    _fast_sleep(monkeypatch, pool, ticks=3)

    await asyncio.wait_for(pool._heartbeat_loop(), 3.0)

    assert calls == []


# ---- the forced paths stay unconditional --------------------------------------------
# De-duplication is for USER cancels only. These two must still be able to push a turn that
# is stuck unwinding, so they keep calling cancel(), not cancel_once().


async def test_a_lost_lease_still_forces_a_cancel_into_a_turn_that_is_already_unwinding(monkeypatch):
    class Scheduler:
        async def heartbeat_worker(self, worker_id):
            return None

        async def report_worker_load(self, worker_id, *, in_flight):
            return None

    class Engine:
        async def heartbeat(self, worker_id, keys):
            return []                               # nothing confirmed: every lease is lost

    pool = _pool(scheduler=Scheduler())
    pool._engine = Engine()                         # type: ignore[assignment]
    pool._in_flight = {(SESSION, "s")}
    scope, task, in_handler, gate = await _turn(pool, "s", unwinding=True)
    scope.cancel_once("user_signal")
    await in_handler.wait()
    _fast_sleep(monkeypatch, pool, ticks=2)

    await asyncio.wait_for(pool._heartbeat_loop(), 3.0)

    assert task.cancelling() == 2
    gate.set()
    await _stop(task)


async def test_the_drain_timeout_still_forces_a_cancel_into_a_turn_stuck_unwinding():
    class Scheduler:
        async def drain_worker(self, worker_id):
            return None

        async def deregister_worker(self, worker_id):
            return None

    pool = _pool(scheduler=Scheduler())
    scope, task, in_handler, gate = await _turn(pool, "s", unwinding=True)
    scope.cancel_once("user_signal")
    await in_handler.wait()

    await pool.drain_and_stop(timeout=0.01)

    assert task.cancelling() == 2
    gate.set()
    await _stop(task)


# ---- lifecycle and the metric ----------------------------------------------------


async def test_start_runs_the_reconcile_loop_as_its_own_task_and_drain_stops_it():
    scheduler = InMemoryScheduler()
    await scheduler.initialize()
    pool = WorkerPool(
        config=WorkerConfig(concurrency=2),
        scheduler=scheduler, storage=None,             # type: ignore[arg-type]
        workspace_registry=None, provider_registry=None,   # type: ignore[arg-type]
        engine=InMemoryClaimEngine(adapters={}),
    )
    await pool.start()
    try:
        names = [t.get_name() for t in pool._tasks]
        assert any(n.startswith("cancel-reconcile-") for n in names), names
        reconcile = next(t for t in pool._tasks if t.get_name().startswith("cancel-reconcile-"))
        heartbeat = next(t for t in pool._tasks if t.get_name().startswith("scheduler-heartbeat-"))
        assert reconcile is not heartbeat
    finally:
        await pool.drain_and_stop(timeout=1.0)
        await scheduler.aclose()

    assert reconcile.done()


def test_the_metric_starts_at_zero():
    assert _pool().metrics_snapshot()["primer_worker_cancels_reconciled_total"] == 0


def test_the_session_model_still_has_the_flag_the_reconciler_reads():
    """The reconciler keys on WorkspaceSession.cancel_requested."""
    assert "cancel_requested" in WorkspaceSession.model_fields


# ---- through a drain ----------------------------------------------------------------
# drain_and_stop sets _stopping and then waits up to drain_timeout_seconds for running turns. The heartbeat and
# both cancel loops used to exit on _stopping, so a Cancel arriving during that wait was only cooperative.


async def test_the_reconcile_loop_keeps_running_through_a_drain_and_ends_with_the_keepalive(monkeypatch):
    pool = _pool({"a": _row(cancel_requested=True), "b": _row(cancel_requested=False)}, interval=1)
    scope_a, task_a, *_ = await _turn(pool, "a")
    scope_b, task_b, *_ = await _turn(pool, "b")
    pool._stopping.set()                            # the drain has begun; the turns are still running
    real_sleep = asyncio.sleep
    ticks = []

    async def fake_sleep(delay, *args, **kwargs):
        ticks.append(delay)
        if len(ticks) >= 3:
            pool._keepalive_done.set()              # drain has finished with the turns
        await real_sleep(0)

    monkeypatch.setattr(pool_module, "asyncio", _Patched(asyncio, sleep=fake_sleep))

    await asyncio.wait_for(pool._cancel_reconcile_loop(), 3.0)

    assert len(ticks) == 3, "the loop quit when drain began instead of when the turns were done"
    assert scope_a.cancelled and not scope_b.cancelled, "a Cancel requested during the drain must preempt its turn"
    await _stop(task_a)
    await _stop(task_b)


async def test_the_cancel_loop_delivers_a_notify_during_a_drain():
    notify = _Notify()
    pool = _pool(scheduler=notify)
    scope_a, task_a, *_ = await _turn(pool, "a")
    scope_b, task_b, *_ = await _turn(pool, "b")
    pool._stopping.set()                            # the drain has begun
    loop_task = asyncio.create_task(pool._cancel_loop())
    try:
        await asyncio.sleep(0.05)
        await notify.queue.put("a")
        await asyncio.wait_for(_until(lambda: scope_a.cancelled), 2.0)
        assert not scope_b.cancelled
        assert not loop_task.done(), "the cancel loop exited with the drain still waiting on running turns"
    finally:
        pool._keepalive_done.set()
        loop_task.cancel()
        await asyncio.gather(loop_task, return_exceptions=True)
        await _stop(task_a)
        await _stop(task_b)


async def test_the_keepalive_has_an_absolute_deadline():
    """A turn task that ignores its cancel would hold drain, and so the keep-alive, open for ever."""
    pool = _pool(scheduler=SimpleNamespace(heartbeat_worker=lambda wid: asyncio.sleep(0)))
    pool._stopping.set()
    assert pool._keepalive_over() is False, "no deadline and no completion: still over?"
    pool._keepalive_deadline = asyncio.get_event_loop().time() - 1
    assert pool._keepalive_over() is True

    await asyncio.wait_for(pool._heartbeat_loop(), 3.0)       # returns at once instead of looping for ever
    await asyncio.wait_for(pool._cancel_reconcile_loop(), 3.0)


async def _until(predicate):
    while not predicate():
        await asyncio.sleep(0.01)
