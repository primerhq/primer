"""Behaviour every Scheduler implementation must satisfy.

This suite is parametrised across :class:`InMemoryScheduler` and
:class:`PostgresScheduler` via the ``scheduler`` fixture in
``conftest.py``. The Postgres parametrisation is skipped automatically
when ``MATRIX_PG_TEST_DSN`` is unset.

The helper :func:`_seed_session` papers over the per-impl seeding
difference (in-memory exposes ``register_session_for_test``; Postgres
needs a real synthetic row inserted via the helper in
``test_postgres.py``) so the test bodies stay impl-agnostic.
"""

from __future__ import annotations

import asyncio

from matrix.int.scheduler import CompleteTurnResult
from matrix.model.session import SessionStatus


async def _seed_session(scheduler, pg_storage_or_none, sid: str,
                        *, turn_no: int = 0) -> None:
    """Seed a synthetic session row in whichever impl is under test."""
    if hasattr(scheduler, "register_session_for_test"):
        scheduler.register_session_for_test(sid, turn_no=turn_no)
    else:
        # Postgres impl — reuse the proven inserter from test_postgres.
        from tests.scheduler.test_postgres import _insert_session
        assert pg_storage_or_none is not None, (
            "Postgres scheduler used but pg_storage_or_none is None"
        )
        await _insert_session(pg_storage_or_none, sid, turn_no=turn_no)


async def test_claim_returns_empty_when_nothing_runnable(scheduler):
    await scheduler.register_worker(
        worker_id="w1", host="h", pid=1, capacity=1,
    )
    assert await scheduler.claim("w1", max_count=4) == []


async def test_complete_turn_round_trip(scheduler, pg_storage_or_none):
    await _seed_session(scheduler, pg_storage_or_none, "p-1")
    await scheduler.register_worker(
        worker_id="w1", host="h", pid=1, capacity=1,
    )
    await scheduler.enqueue("p-1")
    [lease] = await scheduler.claim("w1", max_count=1)
    result = await scheduler.complete_turn(
        "w1", "p-1",
        expected_turn_no=lease.turn_no,
        new_status=SessionStatus.WAITING,
        re_enqueue=False,
    )
    assert result == CompleteTurnResult.SUCCESS


async def test_lease_lost_when_other_worker_claims_completion(
    scheduler, pg_storage_or_none,
):
    await _seed_session(scheduler, pg_storage_or_none, "p-2")
    await scheduler.register_worker(
        worker_id="w1", host="h", pid=1, capacity=1,
    )
    await scheduler.register_worker(
        worker_id="w2", host="h", pid=2, capacity=1,
    )
    await scheduler.enqueue("p-2")
    [lease] = await scheduler.claim("w1", max_count=1)
    result = await scheduler.complete_turn(
        "w2", "p-2",
        expected_turn_no=lease.turn_no,
        new_status=SessionStatus.RUNNING,
        re_enqueue=False,
    )
    assert result == CompleteTurnResult.LEASE_LOST


async def test_concurrent_claims_only_one_winner(
    scheduler, pg_storage_or_none,
):
    await _seed_session(scheduler, pg_storage_or_none, "p-3")
    await scheduler.register_worker(
        worker_id="w1", host="h", pid=1, capacity=1,
    )
    await scheduler.register_worker(
        worker_id="w2", host="h", pid=2, capacity=1,
    )
    await scheduler.enqueue("p-3")
    a, b = await asyncio.gather(
        scheduler.claim("w1", max_count=1),
        scheduler.claim("w2", max_count=1),
    )
    assert (len(a) + len(b)) == 1
