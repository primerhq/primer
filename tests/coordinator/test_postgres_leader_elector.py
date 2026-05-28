"""Tests for PostgresLeaderElector against a real Postgres test container."""

from __future__ import annotations

import asyncio
import os

import pytest

pytestmark = pytest.mark.skipif(
    not os.environ.get("PRIMER_TEST_POSTGRES_URL"),
    reason="needs PRIMER_TEST_POSTGRES_URL set",
)


@pytest.mark.asyncio
async def test_first_caller_wins(postgres_storage_provider):
    from primer.coordinator.postgres import PostgresLeaderElector

    e1 = PostgresLeaderElector(postgres_storage_provider, owner_id="o1")
    e2 = PostgresLeaderElector(postgres_storage_provider, owner_id="o2")

    lease1 = await e1.try_acquire("test-role", lease_seconds=5)
    assert lease1 is not None
    lease2 = await e2.try_acquire("test-role", lease_seconds=5)
    assert lease2 is None
    await lease1.release()
    lease3 = await e2.try_acquire("test-role", lease_seconds=5)
    assert lease3 is not None
    await lease3.release()


@pytest.mark.asyncio
async def test_expired_lease_can_be_stolen(postgres_storage_provider):
    """Cancelling the heartbeat task lets the lease expire; another
    instance can then take the role."""
    from primer.coordinator.postgres import PostgresLeaderElector

    e1 = PostgresLeaderElector(postgres_storage_provider, owner_id="o1")
    e2 = PostgresLeaderElector(postgres_storage_provider, owner_id="o2")

    lease1 = await e1.try_acquire("test-role-2", lease_seconds=1)
    assert lease1 is not None
    # Force lease loss by killing the heartbeat without releasing
    lease1._heartbeat_task.cancel()  # type: ignore[attr-defined]
    try: await lease1._heartbeat_task  # type: ignore[attr-defined]
    except (asyncio.CancelledError, Exception): pass
    await asyncio.sleep(1.5)
    lease2 = await e2.try_acquire("test-role-2", lease_seconds=5)
    assert lease2 is not None
    await lease2.release()
