"""Tests for the ``sso_jit_enabled``/``sso_default_access`` columns on the
``system_state`` singleton table on both storage backends.

Postgres tests are skipped unless ``PRIMER_TEST_POSTGRES_URL`` is set.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest
import pytest_asyncio

from primer.int.storage_provider import StorageProvider
from primer.model.except_ import ConfigError
from primer.model.provider import PoolConfig, PostgresConfig, SqliteConfig
from primer.storage.postgres import PostgresStorageProvider
from primer.storage.sqlite import SqliteStorageProvider


# ---------------------------------------------------------------------------
# Helpers / fixtures
# ---------------------------------------------------------------------------

_POSTGRES_URL_ENV = "PRIMER_TEST_POSTGRES_URL"


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
                f"SET sso_jit_enabled = false, sso_default_access = NULL"
            )
        try:
            yield provider
        finally:
            async with provider.pool.acquire() as conn:
                await conn.execute(
                    f'UPDATE "{provider.schema}"."system_state" '
                    f"SET sso_jit_enabled = false, sso_default_access = NULL"
                )
            await provider.aclose()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sso_settings_default(storage_provider: StorageProvider):
    """Fresh DB: sso_jit_enabled is False, sso_default_access is None."""
    state = await storage_provider.get_system_state()
    assert state.sso_jit_enabled is False
    assert state.sso_default_access is None


@pytest.mark.asyncio
async def test_sso_settings_roundtrip(storage_provider: StorageProvider):
    """Setters persist and are reflected on a re-fetched system_state."""
    await storage_provider.set_sso_jit_enabled(True)
    await storage_provider.set_sso_default_access("restricted")
    state = await storage_provider.get_system_state()
    assert state.sso_jit_enabled is True
    assert state.sso_default_access == "restricted"
