"""Tests for the ``system_state`` singleton table on both storage backends.

Postgres tests are skipped unless ``MATRIX_TEST_POSTGRES_URL`` is set.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest
import pytest_asyncio

from matrix.int.storage_provider import StorageProvider
from matrix.model.except_ import ConfigError
from matrix.model.provider import PoolConfig, PostgresConfig, SqliteConfig
from matrix.storage.postgres import PostgresStorageProvider
from matrix.storage.sqlite import SqliteStorageProvider


# ---------------------------------------------------------------------------
# Helpers / fixtures
# ---------------------------------------------------------------------------

_POSTGRES_URL_ENV = "MATRIX_TEST_POSTGRES_URL"


def _parse_postgres_url(url: str) -> PostgresConfig:
    p = urlparse(url)
    if p.scheme not in {"postgres", "postgresql"}:
        raise ConfigError(f"unexpected scheme {p.scheme!r} in {_POSTGRES_URL_ENV}")
    query = parse_qs(p.query)
    schema = query.get("schema", ["public"])[0]
    return PostgresConfig(
        hostname=p.hostname or "localhost",
        port=p.port or 5432,
        username=p.username or "postgres",
        password=p.password or "",  # type: ignore[arg-type]
        database=(p.path or "/postgres").lstrip("/") or "postgres",
        db_schema=schema,
        pool=PoolConfig(min_size=1, max_size=4),
    )


@pytest_asyncio.fixture
async def sqlite_provider(tmp_path: Path) -> AsyncIterator[SqliteStorageProvider]:
    cfg = SqliteConfig(path=tmp_path / "data.sqlite")
    provider = SqliteStorageProvider(cfg)
    await provider.initialize()
    try:
        yield provider
    finally:
        await provider.aclose()


@pytest_asyncio.fixture
async def postgres_provider() -> AsyncIterator[PostgresStorageProvider]:
    url = os.environ.get(_POSTGRES_URL_ENV)
    if not url:
        pytest.skip(f"set {_POSTGRES_URL_ENV} to run Postgres system_state tests")
    cfg = _parse_postgres_url(url)
    provider = PostgresStorageProvider(cfg)
    await provider.initialize()
    # Reset singleton between tests.
    async with provider.pool.acquire() as conn:
        await conn.execute(
            f'UPDATE "{provider.schema}"."system_state" '
            f"SET bootstrap_completed_at = NULL, last_migration_at = NULL, "
            f"schema_version = 1"
        )
    try:
        yield provider
    finally:
        async with provider.pool.acquire() as conn:
            await conn.execute(
                f'UPDATE "{provider.schema}"."system_state" '
                f"SET bootstrap_completed_at = NULL, last_migration_at = NULL, "
                f"schema_version = 1"
            )
        await provider.aclose()


# Parametrize over both backends using indirect fixtures.
# SQLite is always available; Postgres is skipped when the env var is absent.
@pytest_asyncio.fixture(params=["sqlite", "postgres"])
async def storage_provider(
    request: pytest.FixtureRequest,
    tmp_path: Path,
) -> AsyncIterator[StorageProvider]:
    if request.param == "sqlite":
        cfg = SqliteConfig(path=tmp_path / "data.sqlite")
        provider = SqliteStorageProvider(cfg)
        await provider.initialize()
        try:
            yield provider
        finally:
            await provider.aclose()
    else:
        url = os.environ.get(_POSTGRES_URL_ENV)
        if not url:
            pytest.skip(f"set {_POSTGRES_URL_ENV} to run Postgres system_state tests")
        cfg = _parse_postgres_url(url)
        provider = PostgresStorageProvider(cfg)
        await provider.initialize()
        async with provider.pool.acquire() as conn:
            await conn.execute(
                f'UPDATE "{provider.schema}"."system_state" '
                f"SET bootstrap_completed_at = NULL, last_migration_at = NULL, "
                f"schema_version = 1"
            )
        try:
            yield provider
        finally:
            async with provider.pool.acquire() as conn:
                await conn.execute(
                    f'UPDATE "{provider.schema}"."system_state" '
                    f"SET bootstrap_completed_at = NULL, last_migration_at = NULL, "
                    f"schema_version = 1"
                )
            await provider.aclose()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_system_state_initial_marker_null(storage_provider: StorageProvider):
    """Fresh DB: bootstrap_completed_at must be None."""
    state = await storage_provider.get_system_state()
    assert state.bootstrap_completed_at is None


@pytest.mark.asyncio
async def test_set_bootstrap_completed_marker(storage_provider: StorageProvider):
    """After set_bootstrap_completed, bootstrap_completed_at must be non-None."""
    await storage_provider.set_bootstrap_completed(datetime.now(UTC))
    state = await storage_provider.get_system_state()
    assert state.bootstrap_completed_at is not None


@pytest.mark.asyncio
async def test_system_state_schema_version_default(storage_provider: StorageProvider):
    """schema_version defaults to 1 on the singleton row."""
    state = await storage_provider.get_system_state()
    assert state.schema_version == 1


@pytest.mark.asyncio
async def test_set_bootstrap_completed_roundtrip(storage_provider: StorageProvider):
    """The timestamp round-trips with timezone information preserved."""
    ts = datetime(2026, 1, 15, 12, 0, 0, tzinfo=UTC)
    await storage_provider.set_bootstrap_completed(ts)
    state = await storage_provider.get_system_state()
    assert state.bootstrap_completed_at is not None
    assert state.bootstrap_completed_at.tzinfo is not None


@pytest.mark.asyncio
async def test_set_bootstrap_completed_idempotent(storage_provider: StorageProvider):
    """Calling set_bootstrap_completed twice overwrites — does not raise."""
    ts1 = datetime(2026, 1, 15, 12, 0, 0, tzinfo=UTC)
    ts2 = datetime(2026, 6, 1, 8, 0, 0, tzinfo=UTC)
    await storage_provider.set_bootstrap_completed(ts1)
    await storage_provider.set_bootstrap_completed(ts2)
    state = await storage_provider.get_system_state()
    assert state.bootstrap_completed_at is not None
