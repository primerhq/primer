"""Shared fixtures for the scheduler test suite.

Exposes a parametric ``scheduler`` fixture that runs each consuming
test against both :class:`InMemoryScheduler` and
:class:`PostgresScheduler`. The Postgres parametrisation is skipped
automatically when ``PRIMER_PG_TEST_DSN`` is unset, so the suite stays
green on machines without a live database.

A companion ``pg_storage_or_none`` fixture yields a real
:class:`PostgresStorageProvider` when the DSN env var is set, and
``None`` otherwise. The parametric scheduler fixture uses it to wire
the Postgres impl, and behavioural tests use it together with the
``_seed_session`` helper in ``test_correctness.py`` to seed synthetic
session rows in an impl-agnostic way.

The fixture is named ``pg_storage_or_none`` rather than
``storage_provider`` to avoid shadowing the local ``storage_provider``
fixture inside ``test_postgres.py`` (which is unconditional and
therefore intentionally distinct from the optional one here).
"""

from __future__ import annotations

import os
from urllib.parse import parse_qs, urlparse

import pytest

from primer.model.except_ import ConfigError
from primer.model.provider import PoolConfig, PostgresConfig
from primer.model.scheduler import PostgresSchedulerConfig
from primer.scheduler.in_memory import InMemoryScheduler
from primer.scheduler.postgres import PostgresScheduler
from primer.storage.postgres import PostgresStorageProvider


_DSN_ENV = "PRIMER_PG_TEST_DSN"


def _parse_dsn(dsn: str) -> PostgresConfig:
    """Same DSN parser shape as ``test_postgres.py`` — kept local so
    the conftest doesn't depend on test-module internals."""
    p = urlparse(dsn)
    if p.scheme not in {"postgres", "postgresql"}:
        raise ConfigError(f"unexpected scheme {p.scheme!r} in {_DSN_ENV}")
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


@pytest.fixture
async def pg_storage_or_none():
    """Yield a :class:`PostgresStorageProvider` when ``PRIMER_PG_TEST_DSN``
    is set, otherwise ``None``.

    Mirrors the table-cleanup pattern from ``test_postgres.py``: drops
    ``workers`` (and ``sessions``, since the correctness suite seeds
    synthetic rows there too) on entry and exit so each test starts
    and ends clean.
    """
    dsn = os.environ.get(_DSN_ENV)
    if not dsn:
        yield None
        return

    cfg = _parse_dsn(dsn)
    sp = PostgresStorageProvider(cfg)
    await sp.initialize()
    async with sp.pool.acquire() as conn:
        await conn.execute("DROP TABLE IF EXISTS workers")
        await conn.execute("DROP TABLE IF EXISTS sessions")
    try:
        yield sp
    finally:
        async with sp.pool.acquire() as conn:
            await conn.execute("DROP TABLE IF EXISTS workers")
            await conn.execute("DROP TABLE IF EXISTS sessions")
        await sp.aclose()


@pytest.fixture(params=["in_memory", "postgres"])
async def scheduler(request, pg_storage_or_none):
    """Yield an initialised Scheduler. The ``postgres`` param is
    skipped when ``PRIMER_PG_TEST_DSN`` is unset."""
    if request.param == "in_memory":
        s = InMemoryScheduler()
        await s.initialize()
        try:
            yield s
        finally:
            await s.aclose()
        return

    # postgres
    if pg_storage_or_none is None:
        pytest.skip(
            f"set {_DSN_ENV} to run the PostgresScheduler parametrisation"
        )
    s = PostgresScheduler(
        storage_provider=pg_storage_or_none,
        config=PostgresSchedulerConfig(),
    )
    await s.initialize()
    try:
        yield s
    finally:
        await s.aclose()
