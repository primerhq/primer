"""Pytest fixtures for the distributed test suite.

Session-scoped Postgres container
----------------------------------
``postgres_container`` boots a ``testcontainers.PostgresContainer`` once
per pytest run. All distributed tests share it; per-test isolation is
achieved via the ``db_schema`` fixture.

Per-test schema isolation
--------------------------
``db_schema`` creates a unique Postgres schema (``test_<8-hex-chars>``)
before each test and drops it after.  ``TestCluster`` instances are
always constructed with this schema so concurrent runs don't collide.

Cluster fixtures
-----------------
* ``cluster_2x2``              — 2 API + 2 worker processes (function-scoped)
* ``cluster_with_4_workers``   — 2 API + 4 worker processes (function-scoped)
* ``fresh_cluster_2x2``        — 2 API + 2 workers on a brand-new schema
                                  (no shared state); intended for auto-
                                  bootstrap exclusivity tests.
"""

from __future__ import annotations

import asyncio
import uuid

import pytest
import pytest_asyncio

from tests.distributed.cluster import TestCluster


# ---------------------------------------------------------------------------
# Postgres container (session-scoped)
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def postgres_container() -> str:  # type: ignore[return]
    """Boot a Postgres 16 container; yield the connection URL.

    Session-scoped so the ~3 second container start-up cost is paid once.
    Skips the test session gracefully when testcontainers or Docker are
    unavailable.
    """
    try:
        from testcontainers.postgres import PostgresContainer  # noqa: PLC0415
    except ImportError:
        pytest.skip("testcontainers[postgres] not installed")

    try:
        with PostgresContainer("postgres:16-alpine") as pg:
            # testcontainers returns a SQLAlchemy-style URL; strip any
            # driver prefix (e.g. ``postgresql+psycopg2://``) so the
            # cluster's URL parser handles it correctly.
            url: str = pg.get_connection_url()
            # Replace driver-qualified schemes with plain postgresql://.
            for driver_prefix in (
                "postgresql+psycopg2://",
                "postgresql+asyncpg://",
                "postgresql+pg8000://",
            ):
                if url.startswith(driver_prefix):
                    url = "postgresql://" + url[len(driver_prefix):]
                    break
            yield url
    except Exception as exc:
        pytest.skip(
            f"Docker/testcontainers not available: {exc}"
        )


# ---------------------------------------------------------------------------
# Per-test schema isolation
# ---------------------------------------------------------------------------


@pytest.fixture
def db_schema(postgres_container: str):
    """Create a unique Postgres schema; drop it after the test.

    The schema is injected into each ``TestCluster`` via the ``schema``
    parameter so every test gets a clean data surface.
    """
    from urllib.parse import urlparse  # noqa: PLC0415

    schema = f"test_{uuid.uuid4().hex[:8]}"

    def _get_asyncpg_dsn(pg_url: str) -> str:
        """Convert a postgres:// URL to a plain asyncpg DSN."""
        p = urlparse(pg_url)
        host = p.hostname or "localhost"
        port = p.port or 5432
        user = p.username or "postgres"
        password = p.password or ""
        db = (p.path or "/postgres").lstrip("/") or "postgres"
        return f"postgresql://{user}:{password}@{host}:{port}/{db}"

    async def _create_schema() -> None:
        import asyncpg  # noqa: PLC0415

        dsn = _get_asyncpg_dsn(postgres_container)
        conn = await asyncpg.connect(dsn)
        try:
            await conn.execute(
                f'CREATE SCHEMA IF NOT EXISTS "{schema}"'
            )
        finally:
            await conn.close()

    async def _drop_schema() -> None:
        import asyncpg  # noqa: PLC0415

        dsn = _get_asyncpg_dsn(postgres_container)
        conn = await asyncpg.connect(dsn)
        try:
            await conn.execute(
                f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'
            )
        finally:
            await conn.close()

    asyncio.run(_create_schema())
    yield schema
    asyncio.run(_drop_schema())


# ---------------------------------------------------------------------------
# Cluster fixtures
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def cluster_2x2(postgres_container: str, db_schema: str) -> TestCluster:
    """2 API + 2 worker processes sharing *db_schema*."""
    cluster = TestCluster(
        postgres_url=postgres_container,
        api_count=2,
        worker_count=2,
        start_port=8200,
        schema=db_schema,
    )
    await cluster.start()
    try:
        yield cluster
    finally:
        await cluster.stop()


@pytest_asyncio.fixture
async def cluster_with_4_workers(
    postgres_container: str, db_schema: str
) -> TestCluster:
    """2 API + 4 worker processes sharing *db_schema*."""
    cluster = TestCluster(
        postgres_url=postgres_container,
        api_count=2,
        worker_count=4,
        start_port=8210,
        schema=db_schema,
    )
    await cluster.start()
    try:
        yield cluster
    finally:
        await cluster.stop()


@pytest_asyncio.fixture
async def fresh_cluster_2x2(postgres_container: str) -> TestCluster:
    """2 API + 2 workers on a brand-new schema.

    Unlike ``cluster_2x2``, this fixture generates its own schema
    (rather than receiving one from ``db_schema``) and does NOT pre-run
    bootstrap.  Both APIs will race the bootstrap on first start,
    making this suitable for the auto-bootstrap exclusivity scenario.
    """
    schema = f"test_{uuid.uuid4().hex[:8]}"
    cluster = TestCluster(
        postgres_url=postgres_container,
        api_count=2,
        worker_count=2,
        start_port=8220,
        schema=schema,
        env_overrides={"MATRIX_AUTO_BOOTSTRAP": "true"},
    )
    await cluster.start()
    try:
        yield cluster
    finally:
        await cluster.stop()
