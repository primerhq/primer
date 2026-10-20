"""Storage-level uniqueness test for ``UserIdentity.(provider_id, subject)``.

Mirrors the parametrised backend ``provider`` fixture in
``test_storage_contract.py``: SQLite always runs; Postgres runs too when
``PRIMER_TEST_POSTGRES_URL`` is set.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from datetime import datetime, timezone
from pathlib import Path

import pytest
import pytest_asyncio

from primer.int.storage_provider import StorageProvider
from primer.model.except_ import ConflictError
from primer.model.oidc import UserIdentity
from primer.model.provider import (
    SqliteConfig,
    StorageProviderConfig,
    StorageProviderType,
)
from primer.storage.factory import StorageProviderFactory
from tests.pg_gate import (
    explicit_port,
    needs_postgres,
    postgres_url,
    require_postgres_url,
)


_BACKENDS: list = ["sqlite"]
if postgres_url():
    _BACKENDS.append(pytest.param("postgres", marks=pytest.mark.postgres))


def _pg_config_for_test() -> StorageProviderConfig:
    """Build a Postgres config from PRIMER_TEST_POSTGRES_URL with a unique schema."""
    from urllib.parse import urlparse

    from primer.model.provider import PoolConfig, PostgresConfig

    u = urlparse(require_postgres_url())
    return StorageProviderConfig(
        provider=StorageProviderType.POSTGRES,
        config=PostgresConfig(
            hostname=u.hostname or "localhost",
            port=explicit_port(u),
            username=u.username or "primer",
            password=u.password or "primer",  # type: ignore[arg-type]
            database=(u.path or "/primer_pgtest").lstrip("/") or "primer_pgtest",
            db_schema=f"t{uuid.uuid4().hex[:16]}",
            pool=PoolConfig(min_size=1, max_size=4),
        ),
    )


@pytest_asyncio.fixture(params=_BACKENDS)
async def provider(
    request: pytest.FixtureRequest, tmp_path: Path,
) -> AsyncIterator[StorageProvider]:
    backend = request.param
    if backend == "sqlite":
        cfg = StorageProviderConfig(
            provider=StorageProviderType.SQLITE,
            config=SqliteConfig(path=tmp_path / "useridentity.sqlite"),
        )
    else:
        cfg = _pg_config_for_test()
    p = StorageProviderFactory.create(cfg)
    await p.initialize()
    try:
        yield p
    finally:
        if backend == "postgres":
            schema = cfg.config.db_schema
            try:
                async with p.pool.acquire() as c:
                    await c.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
            except Exception:
                pass
        await p.aclose()


@pytest.mark.asyncio
async def test_duplicate_provider_subject_rejected(provider: StorageProvider) -> None:
    """A second UserIdentity with the same (provider_id, subject) conflicts."""
    s = provider.get_storage(UserIdentity)
    now = datetime(2024, 1, 1, tzinfo=timezone.utc)
    await s.create(
        UserIdentity(user_id="user-1", provider_id="p", subject="s", created_at=now)
    )
    with pytest.raises(ConflictError):
        await s.create(
            UserIdentity(
                user_id="user-2", provider_id="p", subject="s", created_at=now,
            )
        )


@pytest.mark.asyncio
async def test_distinct_provider_subject_allowed(provider: StorageProvider) -> None:
    """Different (provider_id, subject) pairs do not collide."""
    s = provider.get_storage(UserIdentity)
    now = datetime(2024, 1, 1, tzinfo=timezone.utc)
    await s.create(
        UserIdentity(user_id="user-1", provider_id="p", subject="s1", created_at=now)
    )
    created = await s.create(
        UserIdentity(user_id="user-2", provider_id="p", subject="s2", created_at=now)
    )
    assert created.subject == "s2"


@needs_postgres("the sequential-provider useridentity regression test")
@pytest.mark.asyncio
async def test_sequential_providers_do_not_share_ensure_state() -> None:
    """Each fresh provider in one process must run its own table DDL.

    Regression for the ensure state that used to be a module-level cache
    keyed by ``id(provider)``: id() is a recycled memory address, so a fresh
    provider allocated at a dead provider's address started with a stale
    "table ensured" entry, skipped the DDL for its (unique) schema, and hit
    a missing table on the first write -- the postgres-lane flake this file
    produced in CI. Sequential providers here mirror the fixture above, so a
    reused address (near-certain once enough providers churn in one process)
    is exactly the window the old code failed in.
    """
    now = datetime(2024, 1, 1, tzinfo=timezone.utc)
    for i in range(20):
        cfg = _pg_config_for_test()
        p = StorageProviderFactory.create(cfg)
        await p.initialize()
        try:
            s = p.get_storage(UserIdentity)
            await s.create(
                UserIdentity(
                    user_id=f"user-{i}", provider_id="p", subject=f"s-{i}",
                    created_at=now,
                )
            )
        finally:
            schema = cfg.config.db_schema
            try:
                async with p.pool.acquire() as c:
                    await c.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
            except Exception:
                pass
            await p.aclose()
