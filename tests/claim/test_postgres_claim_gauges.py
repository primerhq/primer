"""The claim gauges sampler against the real leases table (live Postgres).

``claim_queue_depth`` counts unclaimed leases. A claim whose lease expired because its worker died is a reclaimable orphan, not an
unclaimed lease, so it is not counted either.

Skipped unless ``PRIMER_TEST_POSTGRES_URL`` is set (see ``tests/pg_gate.py``).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from urllib.parse import parse_qs, urlparse

import pytest
import pytest_asyncio

import primer.api._app_lifespan_phases as phases
import primer.observability.metrics as m
from primer.claim.postgres import PostgresClaimEngine
from primer.int.claim import ClaimAdapter, ClaimKind, ReleaseOutcome
from primer.model.provider import PoolConfig, PostgresConfig
from primer.storage.postgres import PostgresStorageProvider
from tests.claim._entity_seed import EntitySeeder
from tests.pg_gate import explicit_port, needs_postgres, require_postgres_url

_needs_pg = needs_postgres("claim gauge sampler tests")


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


class _NoJoinAdapter(ClaimAdapter):
    """Eligibility touches only the lease alias; the claim query still joins the entity table, so rows are seeded."""

    kind = ClaimKind.HARNESS
    entity_table = "chats"

    def eligibility_sql(self) -> str:
        return "l.kind IS NOT NULL"

    async def on_release(self, conn, entity_id, *, outcome): ...


@pytest.fixture(autouse=True)
def _fresh_metrics():
    m.reset_for_test()
    yield
    m.reset_for_test()


@pytest_asyncio.fixture
async def pg_storage() -> AsyncIterator[PostgresStorageProvider]:
    sp = PostgresStorageProvider(_config(require_postgres_url("claim gauge sampler tests")))
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


async def _armed_engine(pg_storage, seeder, ids: list[str]) -> PostgresClaimEngine:
    adapter = _NoJoinAdapter()
    engine = PostgresClaimEngine(storage_provider=pg_storage, adapters={ClaimKind.HARNESS: adapter})
    await seeder.seed(adapter.entity_table, ids)
    for entity_id in ids:
        await engine.upsert(ClaimKind.HARNESS, entity_id)
    return engine


def _queued(kind: ClaimKind) -> float:
    return m.claim_queue_depth.labels(kind.value)._value.get()


@_needs_pg
@pytest.mark.asyncio
async def test_queue_depth_follows_the_claims_and_returns_to_zero_when_the_kind_drains(pg_storage, seeder):
    engine = await _armed_engine(pg_storage, seeder, ["g-1", "g-2", "g-3"])

    await phases.sample_claim_gauges_once(engine)
    assert _queued(ClaimKind.HARNESS) == 3.0

    claimed = await engine.claim_due("worker-A", max_count=2)
    await phases.sample_claim_gauges_once(engine)
    assert _queued(ClaimKind.HARNESS) == 1.0

    for lease in claimed:
        await engine.release(lease, outcome=ReleaseOutcome(success=True, drop_lease=True))
    (last,) = await engine.claim_due("worker-A", max_count=1)
    await engine.release(last, outcome=ReleaseOutcome(success=True, drop_lease=True))
    await phases.sample_claim_gauges_once(engine)

    assert _queued(ClaimKind.HARNESS) == 0.0, "the drained kind must read 0, not its last non-zero depth"


@_needs_pg
@pytest.mark.asyncio
async def test_a_claim_whose_lease_expired_is_not_queued(pg_storage, seeder):
    engine = await _armed_engine(pg_storage, seeder, ["g-1"])
    await engine.claim_due("worker-A", max_count=1)
    async with pg_storage.pool.acquire() as conn:
        await conn.execute(
            f"UPDATE {pg_storage.leases_table} SET expires_at = now() - interval '1 second' WHERE entity_id = 'g-1'"
        )

    await phases.sample_claim_gauges_once(engine)

    assert await engine.has_live_lease(ClaimKind.HARNESS, "g-1") is False
    assert _queued(ClaimKind.HARNESS) == 0.0


@_needs_pg
@pytest.mark.asyncio
async def test_the_other_kinds_read_zero_and_the_depth_is_per_kind(pg_storage, seeder):
    engine = await _armed_engine(pg_storage, seeder, ["g-1", "g-2"])

    await phases.sample_claim_gauges_once(engine)

    assert _queued(ClaimKind.HARNESS) == 2.0
    for other in (ClaimKind.SESSION, ClaimKind.TRIGGER, ClaimKind.TOOL_CALL):
        assert _queued(other) == 0.0, other
