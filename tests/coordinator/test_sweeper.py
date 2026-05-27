"""Tests for CoordinatorSweeper — periodic cleanup of stale leases.

The factory branch test runs without Postgres (uses an InMemoryEventBus
to confirm the in-memory path; Postgres path is exercised indirectly via
the Postgres rate-limiter tests + factory branch test which still uses
the in-memory bus and proves the in-memory fall-through is gone).
"""

from __future__ import annotations

import os

import pytest


pytestmark_pg = pytest.mark.skipif(
    not os.environ.get("MATRIX_TEST_POSTGRES_URL"),
    reason="needs MATRIX_TEST_POSTGRES_URL set",
)


@pytestmark_pg
@pytest.mark.asyncio
async def test_sweeper_deletes_expired_rate_limit_leases(postgres_storage_provider):
    """Insert an expired lease; run the sweep once; lease is gone."""
    from matrix.coordinator.sweeper import sweep_expired_leases

    async with postgres_storage_provider.pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO rate_limit_lease (lease_id, key, owner_id, claimed_at, expires_at)
            VALUES ('stale', 'k', 'o', now() - interval '120 seconds', now() - interval '60 seconds')
            """,
        )

    n = await sweep_expired_leases(postgres_storage_provider)
    assert n >= 1

    async with postgres_storage_provider.pool.acquire() as conn:
        row = await conn.fetchval(
            "SELECT lease_id FROM rate_limit_lease WHERE lease_id = 'stale'",
        )
    assert row is None


@pytestmark_pg
@pytest.mark.asyncio
async def test_sweeper_deletes_expired_leader_leases(postgres_storage_provider):
    from matrix.coordinator.sweeper import sweep_expired_leases

    async with postgres_storage_provider.pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO leader_lease (role, owner_id, claimed_at, expires_at)
            VALUES ('stale-role', 'o1', now() - interval '120 seconds', now() - interval '60 seconds')
            """,
        )

    n = await sweep_expired_leases(postgres_storage_provider)
    assert n >= 1

    async with postgres_storage_provider.pool.acquire() as conn:
        row = await conn.fetchval(
            "SELECT role FROM leader_lease WHERE role = 'stale-role'",
        )
    assert row is None


@pytest.mark.asyncio
async def test_factory_returns_in_memory_for_in_memory_bus(fake_storage_provider):
    """Factory still returns in-memory backends for in-memory bus."""
    from matrix.bus.in_memory import InMemoryEventBus
    from matrix.coordinator.factory import CoordinatorFactory
    from matrix.coordinator.in_memory import (
        InMemoryInvalidationBus, InMemoryLeaderElector, InMemoryRateLimiter,
    )

    bus = InMemoryEventBus()
    await bus.initialize()
    coord = CoordinatorFactory.create(
        storage_provider=fake_storage_provider,
        event_bus=bus,
        owner_id="api-t",
    )
    assert isinstance(coord.rate_limiter, InMemoryRateLimiter)
    assert isinstance(coord.invalidation_bus, InMemoryInvalidationBus)
    assert isinstance(coord.leader_elector, InMemoryLeaderElector)
    await bus.aclose()


@pytestmark_pg
@pytest.mark.asyncio
async def test_factory_returns_postgres_for_postgres_bus(postgres_storage_provider):
    """Factory returns Postgres backends when given a Postgres event bus.

    Construct a real PostgresEventBus to satisfy the isinstance check in
    the factory, so we verify the Postgres branch isn't dead code."""
    from matrix.bus.postgres import PostgresEventBus
    from matrix.coordinator.factory import CoordinatorFactory
    from matrix.coordinator.postgres import (
        PostgresInvalidationBus, PostgresLeaderElector, PostgresRateLimiter,
    )

    bus = PostgresEventBus(postgres_storage_provider)
    await bus.initialize()
    try:
        coord = CoordinatorFactory.create(
            storage_provider=postgres_storage_provider,
            event_bus=bus,
            owner_id="api-t",
        )
        assert isinstance(coord.rate_limiter, PostgresRateLimiter)
        assert isinstance(coord.invalidation_bus, PostgresInvalidationBus)
        assert isinstance(coord.leader_elector, PostgresLeaderElector)
    finally:
        await bus.aclose()
