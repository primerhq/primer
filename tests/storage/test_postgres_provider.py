"""Tests for PostgresStorageProvider — leases table DDL + qualified-name property.

Requires PRIMER_TEST_POSTGRES_URL to run; skipped otherwise.
"""

from __future__ import annotations

import os

import pytest

pytestmark = pytest.mark.skipif(
    not os.environ.get("PRIMER_TEST_POSTGRES_URL"),
    reason="needs PRIMER_TEST_POSTGRES_URL set",
)


@pytest.mark.asyncio
async def test_postgres_provider_creates_leases_table(postgres_storage_provider):
    """initialize() must create the leases table in the configured schema."""
    sp = postgres_storage_provider
    async with sp.pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT table_name
              FROM information_schema.tables
             WHERE table_schema = $1
               AND table_name   = 'leases'
            """,
            sp.schema,
        )
    assert row is not None, "leases table should exist after initialize()"


@pytest.mark.asyncio
async def test_postgres_provider_leases_table_property(postgres_storage_provider):
    """leases_table property returns the schema-qualified name."""
    sp = postgres_storage_provider
    expected = f'"{sp.schema}"."leases"'
    assert sp.leases_table == expected


@pytest.mark.asyncio
async def test_postgres_provider_ping_round_trips(postgres_storage_provider):
    """ping() answers on a live pool (the /v1/ready happy path)."""
    await postgres_storage_provider.ping()


@pytest.mark.asyncio
async def test_postgres_provider_ping_raises_when_the_pool_is_gone():
    """The provider object still exists and still believes it is
    initialised; the pool behind it does not. ping() must raise rather
    than answer from in-process state. Uses its own provider so the
    shared fixture's teardown is not left holding a dead pool."""
    from primer.storage.postgres import PostgresStorageProvider
    from tests.coordinator.conftest import _URL_ENV, _parse_url

    sp = PostgresStorageProvider(_parse_url(os.environ[_URL_ENV]))
    await sp.initialize()
    sp.pool.terminate()
    with pytest.raises(Exception):
        await sp.ping()
