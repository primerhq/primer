"""Tests for PRIMER_DB_SCHEMA / per-test schema isolation.

SQLite test: setting db_schema on AppConfig has no effect (SQLite has no
schema concept) — the provider still boots and stores data normally.

Postgres test: two providers pointing at different schemas don't see each
other's data.  Skipped unless PRIMER_TEST_POSTGRES_URL is set.
"""

from __future__ import annotations

import os
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest

from primer.model.common import Identifiable
from primer.model.except_ import ConfigError
from primer.model.provider import (
    PoolConfig,
    PostgresConfig,
    SqliteConfig,
    StorageProviderConfig,
    StorageProviderType,
)
from primer.storage.postgres import PostgresStorageProvider
from primer.storage.sqlite import SqliteStorageProvider

_POSTGRES_URL_ENV = "PRIMER_TEST_POSTGRES_URL"


# ---------------------------------------------------------------------------
# Minimal model for isolation checks
# ---------------------------------------------------------------------------


class _Widget(Identifiable):
    name: str


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _parse_postgres_url(url: str, *, schema: str) -> PostgresConfig:
    p = urlparse(url)
    if p.scheme not in {"postgres", "postgresql"}:
        raise ConfigError(f"unexpected scheme {p.scheme!r} in {_POSTGRES_URL_ENV}")
    return PostgresConfig(
        hostname=p.hostname or "localhost",
        port=p.port or 5432,
        username=p.username or "postgres",
        password=p.password or "",  # type: ignore[arg-type]
        database=(p.path or "/postgres").lstrip("/") or "postgres",
        db_schema=schema,
        pool=PoolConfig(min_size=1, max_size=2),
    )


# ---------------------------------------------------------------------------
# SQLite: db_schema is a no-op
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sqlite_schema_override_has_no_effect(tmp_path: Path) -> None:
    """Setting db_schema on AppConfig when SQLite is in use doesn't crash.

    The StorageProviderConfig for SQLite carries no schema concept; the
    override is silently ignored.  The provider must still initialise and
    accept writes correctly.
    """
    from primer.api.config import AppConfig

    cfg = AppConfig(
        db=StorageProviderConfig(
            provider=StorageProviderType.SQLITE,
            config=SqliteConfig(path=tmp_path / "data.sqlite"),
        ),
        db_schema="test_schema_xyz",  # should be silently ignored
        auto_bootstrap=False,
    )

    # Replicate what _build_storage_provider does (import locally so this
    # test doesn't depend on a running FastAPI app).
    from primer.api.app import _build_storage_provider

    provider = _build_storage_provider(cfg)
    assert isinstance(provider, SqliteStorageProvider)

    await provider.initialize()
    try:
        storage = provider.get_storage(_Widget)
        widget = _Widget(id="w1", name="hello")
        created = await storage.create(widget)
        assert created.id == "w1"
        fetched = await storage.get("w1")
        assert fetched is not None
        assert fetched.name == "hello"
    finally:
        await provider.aclose()


# ---------------------------------------------------------------------------
# Postgres: two schemas are fully isolated
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_postgres_two_schemas_are_isolated() -> None:
    """Providers using different Postgres schemas don't see each other's rows.

    One widget is written via schema_a; schema_b must return None for the
    same id.  Skipped unless PRIMER_TEST_POSTGRES_URL is set.
    """
    url = os.environ.get(_POSTGRES_URL_ENV)
    if not url:
        pytest.skip(f"set {_POSTGRES_URL_ENV} to run Postgres schema-isolation tests")

    cfg_a = _parse_postgres_url(url, schema="test_iso_a")
    cfg_b = _parse_postgres_url(url, schema="test_iso_b")

    provider_a = PostgresStorageProvider(cfg_a)
    provider_b = PostgresStorageProvider(cfg_b)

    await provider_a.initialize()
    await provider_b.initialize()
    try:
        storage_a = provider_a.get_storage(_Widget)
        storage_b = provider_b.get_storage(_Widget)

        widget = _Widget(id="shared-id", name="schema_a_widget")
        await storage_a.create(widget)

        # schema_b must not see the row created in schema_a
        result = await storage_b.get("shared-id")
        assert result is None, (
            "schema_b should not see rows created under schema_a"
        )

        # Confirm schema_a still owns the row
        result_a = await storage_a.get("shared-id")
        assert result_a is not None
        assert result_a.name == "schema_a_widget"
    finally:
        # Clean up test schemas so repeated runs start fresh.
        async with provider_a.pool.acquire() as conn:
            await conn.execute('DROP SCHEMA IF EXISTS "test_iso_a" CASCADE')
        async with provider_b.pool.acquire() as conn:
            await conn.execute('DROP SCHEMA IF EXISTS "test_iso_b" CASCADE')
        await provider_a.aclose()
        await provider_b.aclose()


@pytest.mark.asyncio
async def test_postgres_db_schema_env_override() -> None:
    """PRIMER_DB_SCHEMA env var flows through AppConfig into the Postgres provider.

    Skipped unless PRIMER_TEST_POSTGRES_URL is set.
    """
    url = os.environ.get(_POSTGRES_URL_ENV)
    if not url:
        pytest.skip(f"set {_POSTGRES_URL_ENV} to run Postgres schema-isolation tests")

    p = urlparse(url)
    from primer.api.config import AppConfig

    cfg = AppConfig(
        db=StorageProviderConfig(
            provider=StorageProviderType.POSTGRES,
            config=PostgresConfig(
                hostname=p.hostname or "localhost",
                port=p.port or 5432,
                username=p.username or "postgres",
                password=p.password or "",  # type: ignore[arg-type]
                database=(p.path or "/postgres").lstrip("/") or "postgres",
                pool=PoolConfig(min_size=1, max_size=2),
            ),
        ),
        db_schema="test_env_override",
        auto_bootstrap=False,
    )

    from primer.api.app import _build_storage_provider

    provider = _build_storage_provider(cfg)
    assert isinstance(provider, PostgresStorageProvider)
    assert provider.schema == "test_env_override"

    await provider.initialize()
    try:
        storage = provider.get_storage(_Widget)
        widget = _Widget(id="env-w1", name="env_test")
        await storage.create(widget)
        fetched = await storage.get("env-w1")
        assert fetched is not None
        assert fetched.name == "env_test"
    finally:
        async with provider.pool.acquire() as conn:
            await conn.execute('DROP SCHEMA IF EXISTS "test_env_override" CASCADE')
        await provider.aclose()
