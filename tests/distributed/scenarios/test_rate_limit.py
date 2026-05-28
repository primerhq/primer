"""Scenario 1 — Rate-limit max-concurrency across OS processes.

Fires 10 concurrent ``/v1/_test/acquire_rate_limit`` requests split
across two API processes, with ``max_concurrency=3`` and a 500ms hold.
Throughout the burst we poll the ``rate_limit_lease`` table and assert
that the peak observed concurrent count never exceeds 3.

Requires:
- ``MATRIX_ENABLE_TEST_ENDPOINTS=1`` (set by the cluster fixture via
  env_overrides).
- A live Postgres container (session-scoped ``postgres_container``
  fixture).
- Docker for testcontainers.

Skip gracefully when the above are unavailable.
"""

from __future__ import annotations

import asyncio
import time
from urllib.parse import urlparse

import pytest
import pytest_asyncio

from tests.distributed.cluster import TestCluster


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _asyncpg_dsn(pg_url: str) -> str:
    """Strip driver prefix; return a bare ``postgresql://`` DSN."""
    p = urlparse(pg_url)
    host = p.hostname or "localhost"
    port = p.port or 5432
    user = p.username or "postgres"
    password = p.password or ""
    db = (p.path or "/postgres").lstrip("/") or "postgres"
    return f"postgresql://{user}:{password}@{host}:{port}/{db}"


async def _peak_concurrency(
    conn,
    schema: str,
    key: str,
    *,
    duration_s: float,
    interval_s: float = 0.05,
) -> int:
    """Poll ``rate_limit_lease`` for *duration_s*; return max observed count."""
    table = f'"{schema}"."rate_limit_lease"'
    deadline = time.monotonic() + duration_s
    peak = 0
    while time.monotonic() < deadline:
        row = await conn.fetchval(
            f"SELECT COUNT(*) FROM {table}"
            f" WHERE key = $1 AND expires_at > now()",
            key,
        )
        count = int(row or 0)
        if count > peak:
            peak = count
        await asyncio.sleep(interval_s)
    return peak


# ---------------------------------------------------------------------------
# Fixture: cluster with test-endpoints enabled
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def cluster_2x2_with_test_endpoints(postgres_container: str, db_schema: str):
    """2 API + 2 worker cluster with test-instrumentation endpoints enabled."""
    cluster = TestCluster(
        postgres_url=postgres_container,
        api_count=2,
        worker_count=2,
        start_port=8300,
        schema=db_schema,
        env_overrides={"MATRIX_ENABLE_TEST_ENDPOINTS": "1"},
    )
    await cluster.start()
    try:
        yield cluster
    finally:
        await cluster.stop()


# ---------------------------------------------------------------------------
# Scenario 1
# ---------------------------------------------------------------------------


@pytest.mark.distributed
@pytest.mark.asyncio
async def test_rate_limit_max_concurrency_3_across_2_apis(
    cluster_2x2_with_test_endpoints: TestCluster,
    postgres_container: str,
) -> None:
    """Fire 10 concurrent acquires across both API processes with max=3.

    Asserts that peak concurrency observed in the ``rate_limit_lease``
    table never exceeds 3, even though 10 callers all start simultaneously.
    """
    try:
        import asyncpg  # noqa: PLC0415
    except ImportError:
        pytest.skip("asyncpg not installed")

    cluster = cluster_2x2_with_test_endpoints
    key = "test-rl-scenario-1"
    max_concurrency = 3
    sleep_ms = 500

    # Build 10 request coroutines, split evenly across api[0] and api[1].
    async def _call(api_index: int) -> None:
        async with cluster.client(api_index) as c:
            resp = await c.post(
                "/_test/acquire_rate_limit",
                params={
                    "key": key,
                    "max_concurrency": max_concurrency,
                    "sleep_ms": sleep_ms,
                },
                timeout=30.0,
            )
            assert resp.status_code == 200, (
                f"acquire_rate_limit on api-{api_index} returned"
                f" {resp.status_code}: {resp.text}"
            )

    tasks = [_call(i % 2) for i in range(10)]

    # Open a monitoring connection to Postgres.
    dsn = _asyncpg_dsn(postgres_container)
    conn = await asyncpg.connect(dsn)
    try:
        # Run the burst + the poller concurrently.
        # The poller observes the table for slightly longer than the
        # expected hold time (sleep_ms) so it catches all slots.
        monitor_duration = (sleep_ms / 1000) + 1.0  # seconds

        burst_task = asyncio.create_task(
            asyncio.gather(*tasks),
            name="rate-limit-burst",
        )
        peak = await _peak_concurrency(
            conn,
            cluster.schema,
            key,
            duration_s=monitor_duration,
        )
        await burst_task  # propagates any request errors
    finally:
        await conn.close()

    assert peak <= max_concurrency, (
        f"Peak concurrency was {peak}, expected <= {max_concurrency}."
        " The Postgres rate-limiter is not enforcing the slot cap."
    )
    assert peak >= 1, (
        "Peak concurrency was 0 — no leases were ever recorded."
        " Check that the test endpoint is reachable."
    )
