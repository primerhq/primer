"""A worker's heartbeat keeps its row alive (H-3 round 1, H3-4).

A peer's boot sweep marks a worker row ``dead`` after five quiet minutes, and ``purge_dead_workers`` deletes it. A worker
that is in fact alive (its heartbeat writes were only slow, or the database was away) must come back: a ``dead`` row
becomes ``active`` again, a ``draining`` row stays draining, and a row that was purged is registered again from what
``register_worker`` recorded. Runs against both schedulers (the Postgres leg needs the live-database gate).
"""

from __future__ import annotations

import logging

import pytest

from primer.scheduler.in_memory import InMemoryScheduler


async def _mark_dead(scheduler, worker_id: str) -> None:
    if isinstance(scheduler, InMemoryScheduler):
        scheduler.mark_worker_dead_for_test(worker_id)
        return
    async with scheduler._storage.pool.acquire() as conn:  # the peer's boot sweep, as SQL
        await conn.execute(f"UPDATE {scheduler._workers_table} SET status = 'dead' WHERE id = $1", worker_id)


async def _row(scheduler, worker_id: str):
    return next((w for w in await scheduler.list_workers() if w.id == worker_id), None)


async def test_a_heartbeat_revives_a_row_marked_dead(scheduler):
    await scheduler.register_worker(worker_id="w1", host="h", pid=1, capacity=4)
    await _mark_dead(scheduler, "w1")
    assert (await _row(scheduler, "w1")).status == "dead"

    await scheduler.heartbeat_worker("w1")

    assert (await _row(scheduler, "w1")).status == "active"


async def test_a_heartbeat_never_resurrects_a_draining_row(scheduler):
    await scheduler.register_worker(worker_id="w1", host="h", pid=1, capacity=4)
    await scheduler.drain_worker("w1")

    await scheduler.heartbeat_worker("w1")

    assert (await _row(scheduler, "w1")).status == "draining"


async def test_a_heartbeat_keeps_an_active_row_active(scheduler):
    await scheduler.register_worker(worker_id="w1", host="h", pid=1, capacity=4)
    await scheduler.heartbeat_worker("w1")
    assert (await _row(scheduler, "w1")).status == "active"


async def test_a_heartbeat_registers_a_purged_row_again(scheduler):
    await scheduler.register_worker(worker_id="w1", host="host-1", pid=7, capacity=4, label="west")
    await _mark_dead(scheduler, "w1")
    assert await scheduler.purge_dead_workers() == 1
    assert await _row(scheduler, "w1") is None

    await scheduler.heartbeat_worker("w1")

    row = await _row(scheduler, "w1")
    assert row is not None and row.status == "active"
    assert (row.host, row.pid, row.capacity, row.label) == ("host-1", 7, 4, "west")


async def test_a_heartbeat_does_not_bring_back_a_deregistered_worker(scheduler):
    await scheduler.register_worker(worker_id="w1", host="h", pid=1, capacity=4)
    await scheduler.deregister_worker("w1")

    await scheduler.heartbeat_worker("w1")

    assert await _row(scheduler, "w1") is None


async def test_a_heartbeat_restores_draining_for_a_drained_worker_marked_dead(scheduler):
    # H-3 r1 minor 3: a peer's boot sweep marks a stale DRAINING row dead, and the local
    # heartbeat used to revive it to ACTIVE (draining -> dead -> active in two steps). A
    # draining worker comes back draining: the dead mark is a liveness verdict, not a
    # state change out of the drain.
    await scheduler.register_worker(worker_id="w1", host="h", pid=1, capacity=4)
    await scheduler.drain_worker("w1")
    await _mark_dead(scheduler, "w1")
    assert (await _row(scheduler, "w1")).status == "dead"

    await scheduler.heartbeat_worker("w1")

    assert (await _row(scheduler, "w1")).status == "draining"


async def test_a_heartbeat_logs_a_dead_row_revival_exactly_once(scheduler, caplog):
    # H-3 r1 minor 3: every revival is logged once (the dead -> restored transition);
    # a row that is already restored gets no further lines.
    await scheduler.register_worker(worker_id="w1", host="h", pid=1, capacity=4)
    await _mark_dead(scheduler, "w1")
    with caplog.at_level(logging.WARNING):
        await scheduler.heartbeat_worker("w1")
        await scheduler.heartbeat_worker("w1")
    assert sum(1 for r in caplog.records if "marked dead" in r.getMessage()) == 1


async def test_a_heartbeat_for_a_worker_never_registered_here_is_a_no_op(scheduler):
    await scheduler.heartbeat_worker("stranger")
    assert await _row(scheduler, "stranger") is None
