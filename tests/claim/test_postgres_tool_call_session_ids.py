"""Two sessions' identically-shaped batches coexist in the real tables (Phase 3 stage 7a, slice S1b; CI Postgres lane).

`ToolCallTask.id` is the PRIMARY KEY of the `toolcalltask` table and `(kind, entity_id)` the key of `leases`. The scoped
call id of every agent session's first call of turn 0 is the same string, so unqualified ids made the second session's
create collide with the first's. Qualified with the session they do not, and the claim query (which JOINs the entity
table) hands each session's tasks out separately.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from urllib.parse import parse_qs, urlparse

import pytest
import pytest_asyncio

from primer.claim.adapters.tool_calls import ToolCallClaimAdapter
from primer.claim.postgres import PostgresClaimEngine
from primer.int.claim import ClaimKind
from primer.model.except_ import ConflictError
from primer.model.provider import PoolConfig, PostgresConfig
from primer.model.tool_call_task import ToolCallTask, ToolCallTaskState, tool_call_task_id
from primer.storage.postgres import PostgresStorageProvider
from tests.pg_gate import explicit_port, needs_postgres, require_postgres_url

_needs_pg = needs_postgres("TOOL_CALL session-qualified id tests")


def _config(url: str) -> PostgresConfig:
    p = urlparse(url)
    return PostgresConfig(
        hostname=p.hostname or "localhost",
        port=explicit_port(p),
        username=p.username or "postgres",
        password=p.password or "",  # type: ignore[arg-type]
        database=(p.path or "/postgres").lstrip("/") or "postgres",
        db_schema=parse_qs(p.query).get("schema", ["public"])[0],
        pool=PoolConfig(min_size=1, max_size=4),
    )


@pytest.fixture
def created_ids() -> list[str]:
    """Every `toolcalltask` id a test here created; `pg_storage` deletes exactly these rows and no others."""
    return []


@pytest_asyncio.fixture
async def pg_storage(created_ids: list[str]) -> AsyncIterator[PostgresStorageProvider]:
    sp = PostgresStorageProvider(_config(require_postgres_url("TOOL_CALL session-qualified id tests")))
    await sp.initialize()
    async with sp.pool.acquire() as conn:
        await conn.execute(f"DELETE FROM {sp.leases_table}")
    try:
        yield sp
    finally:
        async with sp.pool.acquire() as conn:
            await conn.execute(f"DELETE FROM {sp.leases_table}")
            if created_ids:
                await conn.execute(
                    f'DELETE FROM "{sp.schema}"."toolcalltask" WHERE id = ANY($1::text[])', created_ids,
                )
        await sp.aclose()


def _task(task_id: str, session_id: str) -> ToolCallTask:
    return ToolCallTask(
        id=task_id, session_id=session_id, turn_no=0, tool_name="t", state=ToolCallTaskState.QUEUED,
        record_seq=1, call_id="call_a", created_at=datetime.now(UTC),
    )


async def _create(store, task: ToolCallTask, created_ids: list[str]) -> None:
    await store.create(task)
    # Only once the create succeeded: an id whose create the database refused is held by a row this call did not
    # write (which may not be this file's at all), so it is never registered for deletion.
    created_ids.append(task.id)


@_needs_pg
@pytest.mark.asyncio
async def test_unqualified_ids_collide_on_the_primary_key_and_qualified_ones_do_not(pg_storage, created_ids):
    store = pg_storage.get_storage(ToolCallTask)
    await _create(store, _task("x:tool:0:1", "sess-pg-A"), created_ids)
    with pytest.raises(ConflictError):
        await store.create(_task("x:tool:0:1", "sess-pg-B"))       # the bug S1b closes
    await store.delete("x:tool:0:1")

    for sid in ("sess-pg-A", "sess-pg-B"):
        await _create(store, _task(tool_call_task_id(sid, "x:tool:0:1"), sid), created_ids)
    for sid in ("sess-pg-A", "sess-pg-B"):
        row = await store.get(tool_call_task_id(sid, "x:tool:0:1"))
        assert row is not None and row.session_id == sid and row.scoped_call_id == "x:tool:0:1"


@_needs_pg
@pytest.mark.asyncio
async def test_each_sessions_task_gets_its_own_lease_and_is_claimed_separately(pg_storage, created_ids):
    store = pg_storage.get_storage(ToolCallTask)
    engine = PostgresClaimEngine(
        storage_provider=pg_storage,
        adapters={ClaimKind.TOOL_CALL: ToolCallClaimAdapter(task_storage=store)},
    )
    ids = [tool_call_task_id(sid, "x:tool:0:1") for sid in ("sess-pg-A", "sess-pg-B")]
    for task_id, sid in zip(ids, ("sess-pg-A", "sess-pg-B")):
        await _create(store, _task(task_id, sid), created_ids)
        await engine.upsert(ClaimKind.TOOL_CALL, task_id)

    for task_id in ids:
        assert await engine.has_lease(ClaimKind.TOOL_CALL, task_id)
    leases = await engine.claim_due("worker-A", max_count=10, kinds=[ClaimKind.TOOL_CALL])
    assert sorted(lse.entity_id for lse in leases) == sorted(ids)
