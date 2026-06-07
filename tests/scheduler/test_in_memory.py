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


async def test_watch_ready_yields_on_enqueue(sched):
    await sched.register_worker(worker_id="w1", host="h", pid=1, capacity=4)
    iterator = sched.watch_ready("w1")

    async def consume():
        async for sid in iterator:
            return sid

    task = asyncio.create_task(consume())
    await asyncio.sleep(0)
    sched.register_session_for_test("s1")
    await sched.enqueue("s1")
    sid = await asyncio.wait_for(task, timeout=1.0)
    assert sid == "s1"


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
    # One enqueue with one registered worker => one notify.
    assert snap["primer_scheduler_notify_received_total"] == 1
    # s1 is runnable + unclaimed.
    assert snap["primer_sessions_runnable_queue_depth"] == 1
    # No expirations yet.
    assert snap["primer_lease_expirations_total"] == 0


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
