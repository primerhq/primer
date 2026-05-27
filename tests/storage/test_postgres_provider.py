"""Tests for PostgresStorageProvider — leases table DDL + qualified-name property.

Requires MATRIX_TEST_POSTGRES_URL to run; skipped otherwise.
"""

from __future__ import annotations

import os

import pytest

pytestmark = pytest.mark.skipif(
    not os.environ.get("MATRIX_TEST_POSTGRES_URL"),
    reason="needs MATRIX_TEST_POSTGRES_URL set",
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
