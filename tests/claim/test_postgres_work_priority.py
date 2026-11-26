"""Native PG priority admission. Hosted PG lane only."""
import asyncio
import json
from datetime import UTC, datetime, timedelta

import asyncpg
import pytest

from primer.claim.adapters.sessions import SessionClaimAdapter
from primer.claim.postgres import PostgresClaimEngine
from primer.int.claim import ClaimKind, ReleaseOutcome
from tests.claim._entity_seed import EntitySeeder
from tests.claim.test_postgres_engine import pg_storage  # noqa: F401
from tests.pg_gate import postgres_marks

pytestmark = postgres_marks("work priority native concurrency")


class Sessions(SessionClaimAdapter):
    async def on_claim(self, conn, lease):
        pass


def queue(sp):
    return PostgresClaimEngine(storage_provider=sp,
                              adapters={ClaimKind.SESSION: Sessions(session_storage=None)})


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
                while not await blocker.fetchval(
                    "SELECT 1 FROM pg_locks waiting JOIN pg_locks held "
                    "USING (locktype, database, classid, objid, objsubid) "
                    "WHERE held.pid=pg_backend_pid() AND held.locktype='advisory' "
                    "AND held.granted AND NOT waiting.granted LIMIT 1"
                ):
                    await asyncio.sleep(.01)
            await blocker.execute(f'UPDATE "{pg_storage.schema}"."sessions" SET data=$1::jsonb WHERE id=\'s\'', json.dumps({"priority": 7}))
        await asyncio.wait_for(task, 3)
        lease, = await engine.claim_due("worker", max_count=1)
        assert lease.work_priority == 7
    finally:
        if task is not None:
            await asyncio.gather(task, return_exceptions=True)
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
                                 adapters={ClaimKind.MEMORY: MemoryClaimAdapter(storage=store)})
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
    engine = PostgresClaimEngine(storage_provider=pg_storage, adapters={})
    assert await engine.claim_due("worker", max_count=1, max_work_priority=0) == []


async def test_parent_snapshot_and_cross_kind_order_ignore_task_priority(pg_storage):
    from primer.claim.adapters.tool_calls import ToolCallClaimAdapter

    seed = EntitySeeder(pg_storage)
    engine = PostgresClaimEngine(
        storage_provider=pg_storage,
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


async def _assert_leases_indexes_valid(pg_storage, names) -> None:
    async with pg_storage.pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT c.relname, i.indisvalid FROM pg_index i JOIN pg_class c ON c.oid=i.indexrelid "
            "WHERE i.indrelid=$1::regclass", pg_storage.leases_table,
        )
    validity = {r["relname"]: r["indisvalid"] for r in rows}
    for name in names:
        assert validity.get(name) is True, name


async def test_boot_creates_the_priority_indexes_on_an_upgraded_lease_table(pg_storage):
    # The priority-ordered claim needs both branch indexes. Emulate the layout
    # that predates them (no work_priority column at all), then run the real
    # boot ensure: it must add the column and create the indexes idempotently,
    # and keep the branch-filter indexes the expired branch still needs.
    table = pg_storage.leases_table
    async with pg_storage.pool.acquire() as conn:
        await conn.execute(f"ALTER TABLE {table} DROP COLUMN work_priority")
    await pg_storage.aclose()
    await pg_storage.initialize()
    await _assert_leases_indexes_valid(
        pg_storage,
        ("leases_work_priority_unclaimed_v1", "leases_work_priority_expired_v1",
         "leases_unclaimed_by_kind", "leases_expiry_by_kind"),
    )


async def test_boot_recreates_dropped_priority_indexes_on_reinitialization(pg_storage):
    # A fresh database gets the same two indexes on its first boot; a database
    # that has them and loses them (manual maintenance) gets them back on the
    # next boot instead of needing an explicit offline preparation.
    async with pg_storage.pool.acquire() as conn:
        for name in ("leases_work_priority_unclaimed_v1", "leases_work_priority_expired_v1"):
            await conn.execute(f'DROP INDEX IF EXISTS "{pg_storage.schema}"."{name}"')
    await pg_storage.aclose()
    await pg_storage.initialize()
    await _assert_leases_indexes_valid(
        pg_storage,
        ("leases_work_priority_unclaimed_v1", "leases_work_priority_expired_v1",
         "leases_unclaimed_by_kind", "leases_expiry_by_kind"),
    )
