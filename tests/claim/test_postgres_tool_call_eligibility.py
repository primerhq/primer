"""The TOOL_CALL claim query, executed against a real Postgres (Phase 3 stage 7a, slice S1-D).

`ToolCallClaimAdapter.eligibility_sql` is a SQL fragment joined into the claim CTE; the unit test of its
text cannot tell whether it selects the right ROWS. The in-memory engine has no eligibility filter at all (it
would claim a gated task and let the pool drop it), so only Postgres can show that a GATED task, which
task-granular gating exists to keep out of its batch siblings' way, is never claimed, and that a RUNNING row
whose worker died is. Mutation M2: add 'gated' to the fragment.

Runs in the CI Postgres lane only (tests/pg_gate.py); no test here assumes a default port.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from urllib.parse import parse_qs, urlparse

import pytest
import pytest_asyncio

from primer.claim.adapters.tool_calls import ToolCallClaimAdapter
from primer.claim.postgres import PostgresClaimEngine
from primer.int.claim import ClaimKind
from primer.model.provider import PoolConfig, PostgresConfig
from primer.storage.postgres import PostgresStorageProvider
from tests.claim._entity_seed import EntitySeeder
from tests.pg_gate import explicit_port, needs_postgres, require_postgres_url

_needs_pg = needs_postgres("TOOL_CALL claim-eligibility tests")
_STATES = ["queued", "running", "gated", "done", "failed"]


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


@pytest_asyncio.fixture
async def pg_storage() -> AsyncIterator[PostgresStorageProvider]:
    sp = PostgresStorageProvider(_config(require_postgres_url("TOOL_CALL claim-eligibility tests")))
    await sp.initialize()
    async with sp.pool.acquire() as conn:
        await conn.execute(f"DELETE FROM {sp.leases_table}")
    try:
        yield sp
    finally:
        async with sp.pool.acquire() as conn:
            await conn.execute(f"DELETE FROM {sp.leases_table}")
        await sp.aclose()


@pytest_asyncio.fixture
async def seeder(pg_storage: PostgresStorageProvider) -> AsyncIterator[EntitySeeder]:
    seeder = EntitySeeder(pg_storage)
    try:
        yield seeder
    finally:
        await seeder.cleanup()


async def _armed_engine(pg_storage, seeder, ids: dict[str, str]) -> PostgresClaimEngine:
    engine = PostgresClaimEngine(
        storage_provider=pg_storage,
        adapters={ClaimKind.TOOL_CALL: ToolCallClaimAdapter(task_storage=None)},
    )
    for state, task_id in ids.items():
        await seeder.seed("toolcalltask", [task_id], data={"state": state})
        await engine.upsert(ClaimKind.TOOL_CALL, task_id)
    return engine


def _ids(prefix: str) -> dict[str, str]:
    return {state: f"{prefix}-{state}" for state in _STATES}


@_needs_pg
@pytest.mark.asyncio
async def test_only_queued_and_running_tasks_are_claimed_never_gated_or_finished(pg_storage, seeder):
    ids = _ids("elig")
    engine = await _armed_engine(pg_storage, seeder, ids)

    first = await engine.claim_due("w1", max_count=10, kinds=[ClaimKind.TOOL_CALL])
    again = await engine.claim_due("w2", max_count=10, kinds=[ClaimKind.TOOL_CALL])

    assert sorted(lse.entity_id for lse in first) == sorted([ids["queued"], ids["running"]])
    assert again == [], "nothing else becomes claimable by asking again"
    for held_back in ("gated", "done", "failed"):
        assert not await engine.has_live_lease(ClaimKind.TOOL_CALL, ids[held_back])


@_needs_pg
@pytest.mark.asyncio
async def test_a_gated_task_becomes_claimable_the_moment_its_row_flips_to_queued(pg_storage, seeder):
    """Eligibility is read from the live row on every claim, not cached with the lease."""
    ids = _ids("flip")
    engine = await _armed_engine(pg_storage, seeder, ids)
    await engine.claim_due("w1", max_count=10, kinds=[ClaimKind.TOOL_CALL])
    assert not await engine.has_live_lease(ClaimKind.TOOL_CALL, ids["gated"])

    async with pg_storage.pool.acquire() as conn:
        await conn.execute(
            f'UPDATE "{pg_storage.schema}"."toolcalltask" '
            "SET data = jsonb_set(data, '{state}', '\"queued\"') WHERE id = $1",
            ids["gated"],
        )
    (lease,) = await engine.claim_due("w2", max_count=10, kinds=[ClaimKind.TOOL_CALL])
    assert lease.entity_id == ids["gated"]


@_needs_pg
@pytest.mark.asyncio
async def test_a_running_task_whose_worker_died_is_reclaimed_by_another_worker(pg_storage, seeder):
    """'running' is eligible only for this: a live claim makes the lease unclaimable, an expired one does not."""
    ids = _ids("crash")
    engine = await _armed_engine(pg_storage, seeder, ids)
    claimed = await engine.claim_due("w1", max_count=10, kinds=[ClaimKind.TOOL_CALL])
    assert ids["running"] in {lse.entity_id for lse in claimed}
    assert await engine.claim_due("w2", max_count=10, kinds=[ClaimKind.TOOL_CALL]) == [], "live claims are not stolen"

    async with pg_storage.pool.acquire() as conn:
        await conn.execute(
            f"UPDATE {pg_storage.leases_table} SET expires_at = now() - interval '1 second' "
            "WHERE kind = 'tool_call' AND entity_id = $1",
            ids["running"],
        )
    (reclaimed,) = await engine.claim_due("w2", max_count=10, kinds=[ClaimKind.TOOL_CALL])
    assert (reclaimed.entity_id, reclaimed.claimed_by) == (ids["running"], "w2")
