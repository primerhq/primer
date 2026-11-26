"""Tests for primer.scheduler.in_memory.InMemoryScheduler."""

from __future__ import annotations

import asyncio

import pytest

from primer.model.workspace_session import SessionStatus
from primer.scheduler.in_memory import InMemoryScheduler


@pytest.fixture
async def sched():
    s = InMemoryScheduler()
    await s.initialize()
    yield s
    await s.aclose()


async def test_register_and_list_workers(sched):
    await sched.register_worker(worker_id="w1", host="h", pid=1, capacity=4)
    workers = await sched.list_workers()
    assert len(workers) == 1
    assert workers[0].id == "w1"


async def test_enqueue_does_not_deliver_a_ready_hint(sched):
    """Review 2026-11-23 finding 1.10: nothing reads the ready queue (no
    consumer drives watch_ready), so a wake no longer delivers onto it: a
    consumer of watch_ready sees nothing, however many enqueues land."""
    await sched.register_worker(worker_id="w1", host="h", pid=1, capacity=4)
    iterator = sched.watch_ready("w1")

    async def consume():
        async for sid in iterator:
            return sid

    task = asyncio.create_task(consume())
    await asyncio.sleep(0)
    sched.register_session_for_test("s1")
    for i in range(100):
        await sched.enqueue(f"s{i}")
    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(task, timeout=0.05)


async def test_metrics_snapshot_returns_expected_keys(sched):
    """Metrics surface: gauges + counters land under spec §14 keys."""
    sched.register_session_for_test("s1")
    sched.register_session_for_test("s2", status=SessionStatus.WAITING)
    await sched.register_worker(worker_id="w1", host="h", pid=1, capacity=4)
    await sched.enqueue("s1")
    snap = sched.metrics_snapshot()
    # Required keys per spec §14.
    assert "primer_sessions_active" in snap
    assert "primer_sessions_runnable_queue_depth" in snap
    assert "primer_lease_expirations_total" in snap
    assert "primer_scheduler_notify_received_total" in snap
    # Sessions-by-status reflects what was registered.
    assert snap["primer_sessions_active"]["running"] == 1
    assert snap["primer_sessions_active"]["waiting"] == 1
    # One enqueue with one registered worker => one notify (the wake is
    # recorded; it is no longer delivered onto the ready queue).
    assert snap["primer_scheduler_notify_received_total"] == 1
    # The claim engine owns the runnable state, so the scheduler's runnable
    # depth is 0 (review 2026-11-23 finding 1.10).
    assert snap["primer_sessions_runnable_queue_depth"] == 0
    # No expirations yet.
    assert snap["primer_lease_expirations_total"] == 0


async def test_many_wakes_do_not_grow_the_ready_queue(sched):
    """Review 2026-11-23 finding 1.10: a wake used to put the session id onto
    every worker's ready queue, which grew unboundedly. It no longer does: a
    burst of enqueues leaves the ready queue empty and the depth at 0."""
    await sched.register_worker(worker_id="w1", host="h", pid=1, capacity=4)
    for i in range(1000):
        await sched.enqueue(f"s{i}")
    w1 = sched._workers["w1"]  # noqa: SLF001
    assert w1.ready_queue.qsize() == 0
    snap = sched.metrics_snapshot()
    assert snap["primer_sessions_runnable_queue_depth"] == 0
    # The notify metric still counts the wakes (one per worker per wake).
    assert snap["primer_scheduler_notify_received_total"] == 1000


async def test_signal_cancel_routes_to_subscribers(sched):
    await sched.register_worker(worker_id="w1", host="h", pid=1, capacity=4)
    iterator = sched.watch_cancel("w1")

    async def consume():
        async for sid in iterator:
            return sid

    task = asyncio.create_task(consume())
    await asyncio.sleep(0)
    await sched.signal_cancel("s1")
    sid = await asyncio.wait_for(task, timeout=1.0)
    assert sid == "s1"


async def test_a_worker_has_not_reported_its_load_until_it_does(sched):
    """Lead sweep M2: unknown (None) is not idle (0), so a fleet total can tell a worker that never reported from a quiet one."""
    await sched.register_worker(worker_id="w1", host="h", pid=1, capacity=4)
    [fresh] = await sched.list_workers()
    assert fresh.in_flight is None

    await sched.report_worker_load("w1", in_flight=3)
    assert [w.in_flight for w in await sched.list_workers()] == [3]

    await sched.report_worker_load("w1", in_flight=0)
    assert [w.in_flight for w in await sched.list_workers()] == [0]


async def test_registering_again_forgets_the_old_report(sched):
    """A restarted worker re-registers under the same id: the previous process's load is not its load."""
    await sched.register_worker(worker_id="w1", host="h", pid=1, capacity=4)
    await sched.report_worker_load("w1", in_flight=4)

    await sched.register_worker(worker_id="w1", host="h", pid=2, capacity=4)

    assert [w.in_flight for w in await sched.list_workers()] == [None]


async def test_reporting_the_load_of_an_unknown_worker_is_a_no_op(sched):
    await sched.report_worker_load("nobody", in_flight=1)
    assert await sched.list_workers() == []
