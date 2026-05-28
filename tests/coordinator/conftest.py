"""Shared fixtures for coordinator tests."""

from __future__ import annotations

import os
from urllib.parse import parse_qs, urlparse

import pytest_asyncio

from primer.model.except_ import ConfigError
from primer.model.provider import PoolConfig, PostgresConfig
from primer.storage.postgres import PostgresStorageProvider


_URL_ENV = "MATRIX_TEST_POSTGRES_URL"


def _parse_url(url: str) -> PostgresConfig:
    p = urlparse(url)
    if p.scheme not in {"postgres", "postgresql"}:
        raise ConfigError(f"unexpected scheme {p.scheme!r} in {_URL_ENV}")
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
async def postgres_storage_provider():
    """An initialised :class:`PostgresStorageProvider` against the URL in
    ``MATRIX_TEST_POSTGRES_URL``. Skips when the variable is unset.

    Drops and recreates the ``rate_limit_lease`` table on entry so each
    test starts clean.
    """
    url = os.environ.get(_URL_ENV)
    if not url:
        import pytest
        pytest.skip(f"set {_URL_ENV} to run Postgres coordinator tests")

    cfg = _parse_url(url)
    sp = PostgresStorageProvider(cfg)
    await sp.initialize()
    # Start each test with empty lease tables.
    async with sp.pool.acquire() as conn:
        await conn.execute("DELETE FROM rate_limit_lease")
        await conn.execute("DELETE FROM leader_lease")
    try:
        yield sp
    finally:
        async with sp.pool.acquire() as conn:
            await conn.execute("DELETE FROM rate_limit_lease")
            await conn.execute("DELETE FROM leader_lease")
        await sp.aclose()
