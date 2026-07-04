"""Storage-level uniqueness test for ``UserIdentity.(provider_id, subject)``.

Mirrors the parametrised backend ``provider`` fixture in
``test_storage_contract.py``: SQLite always runs; Postgres runs too when
``PRIMER_TEST_PG_DSN`` is set.
"""

from __future__ import annotations

import os
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


_BACKENDS: list[str] = ["sqlite"]
if os.environ.get("PRIMER_TEST_PG_DSN"):
    _BACKENDS.append("postgres")


def _pg_config_for_test() -> StorageProviderConfig:
    """Build a Postgres config from PRIMER_TEST_PG_DSN with a unique schema."""
    from urllib.parse import urlparse

    from primer.model.provider import PoolConfig, PostgresConfig

    u = urlparse(os.environ["PRIMER_TEST_PG_DSN"])
    return StorageProviderConfig(
        provider=StorageProviderType.POSTGRES,
        config=PostgresConfig(
            hostname=u.hostname or "localhost",
            port=u.port or 5432,
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
