"""Native PG priority admission and offline migration. Hosted PG lane only."""
import asyncio
import json
from datetime import UTC, datetime, timedelta

import asyncpg
import pytest

from primer.claim.adapters.sessions import SessionClaimAdapter
from primer.claim.postgres import PostgresClaimEngine
from primer.claim.priority_migration import backfill_priority_batch, prepare_priority_indexes
from primer.int.claim import ClaimKind, ReleaseOutcome
from tests.claim._entity_seed import EntitySeeder
from tests.claim.test_postgres_engine import pg_storage  # noqa: F401
from tests.pg_gate import postgres_marks

pytestmark = postgres_marks("work priority native concurrency and migration")


class Sessions(SessionClaimAdapter):
    async def on_claim(self, conn, lease):
        pass


def queue(sp, enabled=True):
    return PostgresClaimEngine(storage_provider=sp,
                              adapters={ClaimKind.SESSION: Sessions(session_storage=None)},
                              work_priority_enabled=enabled)


async def test_two_claimers_skip_locked_and_expired_candidates_without_duplicate_ownership(pg_storage):
    seed = EntitySeeder(pg_storage)
    engine = queue(pg_storage)
    try:
        for name, priority in (("urgent", 0), ("next", 3), ("background", 100)):
            await seed.seed("sessions", [name], {"priority": priority})
            await engine.upsert(ClaimKind.SESSION, name)
        async with pg_storage.pool.acquire() as lock, lock.transaction():
            await lock.execute(f"SELECT 1 FROM {pg_storage.leases_table} WHERE entity_id='urgent' FOR UPDATE")
            next_, = await engine.claim_due("one", max_count=1)
            assert next_.entity_id == "next" and next_.work_priority == 3
            assert await engine.claim_due("two", max_count=1, max_work_priority=0) == []
        urgent, = await engine.claim_due("two", max_count=1, max_work_priority=0)
        assert urgent.entity_id == "urgent"
        async with pg_storage.pool.acquire() as conn:
            await conn.execute(f"UPDATE {pg_storage.leases_table} SET expires_at=now()-interval '1 second' WHERE entity_id='next'")
        a, b = await asyncio.gather(engine.claim_due("three", max_count=1), engine.claim_due("four", max_count=1))
        # Each SQL branch can temporarily lock a candidate not selected by
        # the final union. A competing worker may get an empty batch; polling
        # after the first statements commit must recover it without duplicates.
        tail = await engine.claim_due("five", max_count=2)
        claimed = a + b + tail
        assert len(claimed) == 2
        assert {l.entity_id for l in claimed} == {"next", "background"}
        await engine.release(next_, outcome=ReleaseOutcome(success=True, drop_lease=True, entity_noop=True))
        assert await engine.has_live_lease(ClaimKind.SESSION, "next")
    finally:
        await seed.cleanup()


async def test_serialized_arm_reads_after_waiting_for_older_arm(pg_storage):
    seed = EntitySeeder(pg_storage)
    engine = queue(pg_storage)
    task = None
    try:
        await seed.seed("sessions", ["s"], {"priority": 90})
        # Materialize tables outside the held lock so the waiter can reach its
        # actual arm lock; observe pg_locks rather than relying on sleep timing.
        async with pg_storage.pool.acquire() as conn:
            await engine._ensure_entity_tables(conn)
        async with pg_storage.pool.acquire() as blocker, blocker.transaction():
            key = f"{pg_storage.leases_table}:session:s:work-priority"
            await blocker.execute("SELECT pg_advisory_xact_lock(hashtextextended($1, 0))", key)
            task = asyncio.create_task(engine.upsert(ClaimKind.SESSION, "s"))
            async with asyncio.timeout(3):
                while not await blocker.fetchval("SELECT 1 FROM pg_locks WHERE locktype='advisory' AND NOT granted LIMIT 1"):
                    await asyncio.sleep(.01)
            await blocker.execute(f'UPDATE "{pg_storage.schema}"."sessions" SET data=$1::jsonb WHERE id=\'s\'', json.dumps({"priority": 7}))
        await asyncio.wait_for(task, 3)
        lease, = await engine.claim_due("worker", max_count=1)
        assert lease.work_priority == 7
    finally:
        if task is not None:
            await asyncio.gather(task, return_exceptions=True)
        await seed.cleanup()


async def test_bounded_backfill_resumes_and_does_not_rewrite_deadlines_or_retry_state(pg_storage):
    seed = EntitySeeder(pg_storage)
    engine = queue(pg_storage, enabled=False)
    try:
        for name, data in (("a", {"priority": 8}), ("b", {}), ("c", {"priority": 2})):
            await seed.seed("sessions", [name], data)
            await engine.upsert(ClaimKind.SESSION, name, next_attempt_at=datetime.now(UTC)+timedelta(hours=1))
        async with pg_storage.pool.acquire() as conn:
            before = await conn.fetch(f"SELECT entity_id,next_attempt_at,attempt_count,memory_occurrence FROM {pg_storage.leases_table} ORDER BY entity_id")
        cursor = await backfill_priority_batch(pg_storage, engine._adapters, limit=1)
        assert cursor == ("session", "a")
        # The second row fails after the first row was updated. The whole
        # page must roll back, and its original input cursor remains usable.
        await seed.seed("sessions", ["b"], {"priority": 5})
        await seed.seed("sessions", ["c"], {"priority": "2"})
        with pytest.raises(asyncpg.NotNullViolationError):
            await backfill_priority_batch(pg_storage, engine._adapters, after=cursor, limit=2)
        async with pg_storage.pool.acquire() as conn:
            assert await conn.fetchval(f"SELECT work_priority FROM {pg_storage.leases_table} WHERE entity_id='b'") == 0
        await seed.seed("sessions", ["b"], {})
        await seed.seed("sessions", ["c"], {"priority": 2})
        cursor = await backfill_priority_batch(pg_storage, engine._adapters, after=cursor, limit=1)
        assert cursor == ("session", "b")
        cursor = await backfill_priority_batch(pg_storage, engine._adapters, after=cursor, limit=1)
        assert await backfill_priority_batch(pg_storage, engine._adapters, after=cursor, limit=1) is None
        await prepare_priority_indexes(pg_storage)
        await prepare_priority_indexes(pg_storage)
        async with pg_storage.pool.acquire() as conn:
            after = await conn.fetch(f"SELECT entity_id,next_attempt_at,attempt_count,memory_occurrence FROM {pg_storage.leases_table} ORDER BY entity_id")
            assert before == after
            assert await conn.fetchval(f"SELECT array_agg(work_priority ORDER BY entity_id) FROM {pg_storage.leases_table}") == [8, 0, 2]
            indexes = await conn.fetch(
                "SELECT c.relname, i.indisvalid FROM pg_index i JOIN pg_class c ON c.oid=i.indexrelid "
                "WHERE i.indrelid=$1::regclass", pg_storage.leases_table,
            )
            validity = {r["relname"]: r["indisvalid"] for r in indexes}
            for name in ("leases_work_priority_unclaimed_v1", "leases_work_priority_expired_v1",
                         "leases_unclaimed_by_kind", "leases_expiry_by_kind"):
                assert validity[name] is True
            await conn.execute(f"UPDATE {pg_storage.leases_table} SET claimed_by='old-worker' WHERE entity_id='a'")
        with pytest.raises(ValueError, match="drained"):
            await backfill_priority_batch(pg_storage, engine._adapters)
    finally:
        await seed.cleanup()


async def test_missing_and_malformed_priority_cannot_be_armed(pg_storage):
    seed = EntitySeeder(pg_storage)
    engine = queue(pg_storage)
    try:
        await seed.seed("sessions", ["legacy"], {})
        await engine.upsert(ClaimKind.SESSION, "legacy")
        for bad in (True, "3", 1.5, -1, 1_000_001, None):
            await seed.seed("sessions", ["bad"], {"priority": bad})
            with pytest.raises(asyncpg.NotNullViolationError):
                await engine.upsert(ClaimKind.SESSION, "bad")
            assert not await engine.has_lease(ClaimKind.SESSION, "bad")
        with pytest.raises(ValueError, match="missing"):
            await engine.upsert(ClaimKind.SESSION, "absent")
        lease, = await engine.claim_due("worker", max_count=1)
        assert lease.work_priority == 0 and lease.entity_id == "legacy"
    finally:
        await seed.cleanup()


async def test_memory_projection_priority_survives_old_claim_release(pg_storage):
    from primer.claim.adapters.memory import MemoryClaimAdapter
    from primer.memory.scheduling import arm_current
    from primer.model.memory import MemoryJob

    store = pg_storage.get_storage(MemoryJob)
    now = datetime.now(UTC)
    job = MemoryJob(id="priority-memory", kind="topics", agent_id="a", created_at=now,
                    expires_at=now+timedelta(days=1), incarnation_id="inc", occurrence_id="old",
                    requested_due_at=now-timedelta(seconds=1), priority=80)
    engine = PostgresClaimEngine(storage_provider=pg_storage,
                                adapters={ClaimKind.MEMORY: MemoryClaimAdapter(storage=store)},
                                work_priority_enabled=True)
    await store.create(job)
    try:
        await arm_current(pg_storage, engine, job.id)
        old, = await engine.claim_due("worker", max_count=1)
        await store.patch_if(job.id, {"occurrence_id": "new", "priority": 30}, where={"occurrence_id": ["old"]})
        await arm_current(pg_storage, engine, job.id)
        await engine.release(old, outcome=ReleaseOutcome(success=False, drop_lease=True))
        fresh, = await engine.claim_due("worker", max_count=1)
        assert old.work_priority == 80 and fresh.work_priority == 30
        assert fresh.memory_occurrence == "new"
        await engine.release(old, outcome=ReleaseOutcome(success=True, drop_lease=True))
        assert await engine.has_live_lease(ClaimKind.MEMORY, job.id)
    finally:
        await store.delete(job.id)


async def test_empty_priority_query_binds_ceiling_and_returns_no_claims(pg_storage):
    engine = PostgresClaimEngine(storage_provider=pg_storage, adapters={}, work_priority_enabled=True)
    assert await engine.claim_due("worker", max_count=1, max_work_priority=0) == []


async def test_parent_snapshot_and_cross_kind_order_ignore_task_priority(pg_storage):
    from primer.claim.adapters.tool_calls import ToolCallClaimAdapter

    seed = EntitySeeder(pg_storage)
    engine = PostgresClaimEngine(
        storage_provider=pg_storage, work_priority_enabled=True,
        adapters={ClaimKind.SESSION: Sessions(session_storage=None),
                  ClaimKind.TOOL_CALL: ToolCallClaimAdapter(task_storage=None)},
    )
    try:
        await seed.seed("sessions", ["parent"], {"priority": 20})
        await seed.seed("sessions", ["urgent"], {"priority": 0})
        await seed.seed("toolcalltask", ["task"], {"session_id": "parent", "priority": 0, "state": "queued"})
        await engine.upsert(ClaimKind.TOOL_CALL, "task", priority=10)
        await engine.upsert(ClaimKind.SESSION, "urgent", priority=100)
        await seed.seed("sessions", ["parent"], {"priority": 90})
        urgent, = await engine.claim_due("first", max_count=1)
        assert urgent.entity_id == "urgent"
        task, = await engine.claim_due("second", max_count=1, kinds=[ClaimKind.TOOL_CALL])
        assert task.work_priority == 20 and task.entity_id == "task"
        await engine.release(task, outcome=ReleaseOutcome(success=True, entity_noop=True))
        again, = await engine.claim_due("third", max_count=1, kinds=[ClaimKind.TOOL_CALL])
        assert again.work_priority == 20
        await engine.release(again, outcome=ReleaseOutcome(success=True, entity_noop=True))
        await engine.mark_resumable(ClaimKind.TOOL_CALL, "task")
        async with pg_storage.pool.acquire() as conn:
            assert await conn.fetchval(f"SELECT work_priority FROM {pg_storage.leases_table} WHERE entity_id='task'") == 90
            await conn.execute(f'DELETE FROM "{pg_storage.schema}"."sessions" WHERE id=$1', "parent")
        with pytest.raises(asyncpg.NotNullViolationError):
            await engine.mark_resumable(ClaimKind.TOOL_CALL, "task")
        async with pg_storage.pool.acquire() as conn:
            assert await conn.fetchval(f"SELECT work_priority FROM {pg_storage.leases_table} WHERE entity_id='task'") == 90
    finally:
        await seed.cleanup()


async def test_additive_upgrade_preserves_legacy_lease_and_old_writer_defaults(pg_storage):
    # The gate fixture uses a disposable test database. Emulate the old column
    # layout there, then exercise the actual provider initialization twice.
    table = pg_storage.leases_table
    async with pg_storage.pool.acquire() as conn:
        await conn.execute(f"ALTER TABLE {table} DROP COLUMN work_priority")
        await conn.execute(
            f"INSERT INTO {table} (kind,entity_id,claimed_by,memory_occurrence,memory_claim_id,attempt_count,last_error) "
            "VALUES ('memory','legacy','old-worker','occ','claim',3,'retry')"
        )
        before = await conn.fetchrow(f"SELECT * FROM {table} WHERE entity_id='legacy'")
    for _ in range(2):
        await pg_storage.aclose()
        await pg_storage.initialize()
    async with pg_storage.pool.acquire() as conn:
        after = dict(await conn.fetchrow(f"SELECT * FROM {table} WHERE entity_id='legacy'"))
        assert after.pop("work_priority") == 0 and after == dict(before)
        # An old writer omitting the new column still receives a zero snapshot.
        await conn.execute(f"INSERT INTO {table} (kind,entity_id) VALUES ('session','old-insert')")
        assert await conn.fetchval(f"SELECT work_priority FROM {table} WHERE entity_id='old-insert'") == 0
        for bad in (-1, 1_000_001):
            with pytest.raises(asyncpg.CheckViolationError):
                await conn.execute(f"UPDATE {table} SET work_priority=$1 WHERE entity_id='old-insert'", bad)
        with pytest.raises(asyncpg.NotNullViolationError):
            await conn.execute(f"UPDATE {table} SET work_priority=NULL WHERE entity_id='old-insert'")
