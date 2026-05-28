"""Tests for primer.coordinator.postgres.PostgresRateLimiter against a
real Postgres test container."""

from __future__ import annotations

import asyncio
import os

import pytest


pytestmark = pytest.mark.skipif(
    not os.environ.get("PRIMER_TEST_POSTGRES_URL"),
    reason="needs PRIMER_TEST_POSTGRES_URL set",
)


@pytest.mark.asyncio
async def test_postgres_rate_limiter_basic(postgres_storage_provider):
    from primer.coordinator.postgres import PostgresRateLimiter

    rl = PostgresRateLimiter(postgres_storage_provider, owner_id="t1")

    lease = await rl.acquire("k", max_concurrency=1)
    blocked = await rl.try_acquire("k", max_concurrency=1, timeout_s=0.2)
    assert blocked is None
    await lease.release()
    lease2 = await asyncio.wait_for(rl.acquire("k", max_concurrency=1), timeout=1.0)
    await lease2.release()


@pytest.mark.asyncio
async def test_postgres_rate_limiter_keys_independent(postgres_storage_provider):
    from primer.coordinator.postgres import PostgresRateLimiter

    rl = PostgresRateLimiter(postgres_storage_provider, owner_id="t2")

    await rl.acquire("a", max_concurrency=1)
    lease_b = await asyncio.wait_for(
        rl.acquire("b", max_concurrency=1), timeout=1.0,
    )
    await lease_b.release()
