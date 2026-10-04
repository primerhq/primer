"""Both asyncpg pools built from PoolConfig must apply its TCP keepalive.

``PoolConfig`` is shared by the storage provider and the pgvector provider, so a
``tcp_keepalive_*`` field that only one of them honoured would be a setting that
silently does nothing. These tests capture the keyword arguments each provider
passes to ``asyncpg.create_pool`` (a stand-in raises straight away, so no
database is needed) and check the ``init`` hook: present and callable by
default, absent when keepalive is switched off, and, for pgvector, still
running its own codec registration.
"""

from __future__ import annotations

import asyncpg
import pytest

from primer.model.except_ import ProviderError
from primer.model.provider import PgVectorConfig, PoolConfig, PostgresConfig
from primer.storage.postgres import PostgresStorageProvider
from primer.vector.pgvector import PgVectorStoreProvider

_CONN = dict(hostname="db.invalid", port=5432, username="u", password="p", database="d")


@pytest.fixture
def captured(monkeypatch) -> dict:
    kwargs: dict = {}

    async def fake_create_pool(*args, **kw):
        kwargs.update(kw)
        raise RuntimeError("stop after capturing create_pool's arguments")

    monkeypatch.setattr(asyncpg, "create_pool", fake_create_pool)
    return kwargs


async def _open_storage(pool: PoolConfig) -> None:
    provider = PostgresStorageProvider(PostgresConfig(**_CONN, pool=pool))
    with pytest.raises(ProviderError):
        await provider.initialize()


async def _open_pgvector(pool: PoolConfig) -> None:
    provider = PgVectorStoreProvider(PgVectorConfig(**_CONN, pool=pool))
    with pytest.raises(ProviderError):
        await provider.initialize()


async def test_the_storage_pool_gets_a_keepalive_init_hook_by_default(captured):
    await _open_storage(PoolConfig())

    assert callable(captured["init"])


async def test_the_storage_pool_gets_no_hook_when_keepalive_is_off(captured):
    await _open_storage(PoolConfig(tcp_keepalive_idle_seconds=0))

    assert captured.get("init") is None


async def test_the_pgvector_pool_gets_the_keepalive_too(captured):
    await _open_pgvector(PoolConfig())

    assert callable(captured["init"])


async def test_the_pgvector_pool_keeps_its_own_init_when_keepalive_is_off(captured):
    """Turning keepalive off must not drop the vector codec registration."""
    await _open_pgvector(PoolConfig(tcp_keepalive_idle_seconds=0))

    assert callable(captured["init"])


async def test_the_pgvector_init_runs_the_keepalive_and_then_its_own_setup(monkeypatch, captured):
    order: list[str] = []

    class _Conn:
        async def execute(self, *args, **kwargs):
            order.append("search_guc")

    async def fake_hook(conn):
        order.append("keepalive")

    async def fake_register(conn):
        order.append("register_vector")

    monkeypatch.setattr("primer.vector.pgvector.keepalive_init_hook", lambda *a, **k: fake_hook)
    monkeypatch.setattr("primer.vector.pgvector.register_vector", fake_register)

    await _open_pgvector(PoolConfig())
    await captured["init"](_Conn())

    assert order == ["keepalive", "register_vector", "search_guc"]
