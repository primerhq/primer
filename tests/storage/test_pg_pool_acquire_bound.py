"""Deferred X2.5: every pool.acquire() that can wait on a saturated pool is
bounded by the configured PoolConfig.acquire_timeout.

asyncpg's Pool.acquire() waits forever when no timeout is passed: a
saturated pool (all max_size connections checked out) then parks the caller
for good. primer applies the configured bound once, at the two places a pool
is built (the storage and the pgvector providers), rather than at each of
the ~90 call sites, so these tests drive those initialises with a stand-in
asyncpg.create_pool and pin that an acquire() without an explicit timeout
reaches the pool carrying the configured one, that an explicit caller
timeout still wins, and that the wrapper is otherwise transparent.
"""

from __future__ import annotations

from contextlib import asynccontextmanager

import asyncpg
import pytest

from primer.model.provider import PgVectorConfig, PoolConfig, PostgresConfig
from primer.storage.postgres import PostgresStorageProvider
from primer.vector.pgvector import PgVectorStoreProvider

_CONN = dict(hostname="db.invalid", port=5432, username="u", password="p", database="d")


class _Conn:
    """A connection that accepts any execute (the DDL never matters here)."""

    async def execute(self, *args, **kwargs):
        return "OK"


class _RecordingPool:
    """Stands in for the asyncpg pool: records the timeout kwarg of every
    acquire() the production code makes, so the bound is observable."""

    def __init__(self):
        self.conn = _Conn()
        self.acquired: list[float | None] = []
        self.closed = 0

    def acquire(self, *, timeout=None):
        self.acquired.append(timeout)

        @asynccontextmanager
        async def _acq():
            yield self.conn
        return _acq()

    async def close(self):
        self.closed += 1


def _install(monkeypatch) -> _RecordingPool:
    pool = _RecordingPool()

    async def fake_create_pool(*args, **kwargs):
        return pool

    monkeypatch.setattr(asyncpg, "create_pool", fake_create_pool)
    return pool


async def test_storage_initialise_binds_unbounded_acquires_to_the_configured_timeout(monkeypatch):
    pool = _install(monkeypatch)
    provider = PostgresStorageProvider(PostgresConfig(**_CONN))
    await provider.initialize()
    # initialize() acquires the pool for its startup DDL without passing a
    # timeout: every one of those waits must carry the configured bound.
    assert pool.acquired, "initialize() never reached the pool"
    assert set(pool.acquired) == {30.0}, pool.acquired  # the PoolConfig default


async def test_a_custom_acquire_timeout_is_the_bound(monkeypatch):
    pool = _install(monkeypatch)
    provider = PostgresStorageProvider(
        PostgresConfig(**_CONN, pool=PoolConfig(acquire_timeout=7.5)),
    )
    await provider.initialize()
    assert set(pool.acquired) == {7.5}


async def test_an_explicit_caller_timeout_still_wins(monkeypatch):
    pool = _install(monkeypatch)
    provider = PostgresStorageProvider(
        PostgresConfig(**_CONN, pool=PoolConfig(acquire_timeout=7.5)),
    )
    await provider.initialize()
    async with provider.pool.acquire(timeout=1.25):
        pass
    assert pool.acquired[-1] == 1.25


async def test_pgvector_initialise_binds_unbounded_acquires_to_the_configured_timeout(monkeypatch):
    pool = _install(monkeypatch)

    async def _register(conn):
        return None

    monkeypatch.setattr("primer.vector.pgvector.register_vector", _register)
    provider = PgVectorStoreProvider(
        PgVectorConfig(**_CONN, pool=PoolConfig(acquire_timeout=12.0)),
    )
    await provider.initialize()
    assert pool.acquired, "initialize() never reached the pool"
    assert set(pool.acquired) == {12.0}


async def test_the_pool_wrapper_is_transparent_to_close(monkeypatch):
    pool = _install(monkeypatch)
    provider = PostgresStorageProvider(PostgresConfig(**_CONN))
    await provider.initialize()
    await provider.aclose()
    assert pool.closed == 1
    # A saturated-pool wait that exceeds the bound surfaces as a TimeoutError
    # to the caller (asyncpg's bounded acquire), never as an infinite wait:
    # the wrapper is the whole mechanism, so pin that it is what initialize
    # installed rather than the bare asyncpg pool.
    assert pool.acquired and None not in pool.acquired
