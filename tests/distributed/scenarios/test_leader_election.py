"""Scenario 3 — Leader-election exclusivity and failover.

Starts a cluster with 2 APIs (each runs all background tasks including
leadership-elected ones) and 4 workers.  All processes race for the
``ROLE_TIMER_SCHEDULER`` role.  At steady state exactly one process
holds it.

The test then SIGTERMs the holder, waits up to 45 seconds for another
process to take over, and samples throughout to confirm that no two
processes hold the role simultaneously.

Sampling cadence: every 200ms for the entire observation window.

Parameters
----------
hold_time_s : float
    Time to observe the initial leadership before killing the holder.
failover_timeout_s : float
    Maximum time allowed for failover to complete.

Requires:
- A live Postgres container + Docker for testcontainers.
- The distributed marker (``pytest -m distributed``).
"""

from __future__ import annotations

import asyncio
import signal
import time
from urllib.parse import urlparse

import pytest
import pytest_asyncio

from tests.distributed.cluster import TestCluster
from primer.int.coordinator import ROLE_TIMER_SCHEDULER


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _asyncpg_dsn(pg_url: str) -> str:
    p = urlparse(pg_url)
    host = p.hostname or "localhost"
    port = p.port or 5432
    user = p.username or "postgres"
    password = p.password or ""
    db = (p.path or "/postgres").lstrip("/") or "postgres"
    return f"postgresql://{user}:{password}@{host}:{port}/{db}"


async def _count_holders(conn, schema: str, role: str) -> int:
    """Return the number of non-expired lease rows for *role*."""
    table = f'"{schema}"."leader_lease"'
    row = await conn.fetchval(
        f"SELECT COUNT(*) FROM {table}"
        f" WHERE role = $1 AND expires_at > now()",
        role,
    )
    return int(row or 0)


async def _current_owner(conn, schema: str, role: str) -> str | None:
    """Return owner_id of the current holder, or None if none."""
    table = f'"{schema}"."leader_lease"'
    return await conn.fetchval(
        f"SELECT owner_id FROM {table}"
        f" WHERE role = $1 AND expires_at > now()",
        role,
    )


# ---------------------------------------------------------------------------
# Fixture
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def cluster_with_4_workers_election(
    postgres_container: str, db_schema: str
) -> TestCluster:
    """2 API + 4 worker processes for the leader-election scenario.

    Uses a higher base port to avoid collisions with other scenarios.
    """
    cluster = TestCluster(
        postgres_url=postgres_container,
        api_count=2,
        worker_count=4,
        start_port=8320,
        schema=db_schema,
    )
    await cluster.start()
    try:
        yield cluster
    finally:
        await cluster.stop()


# ---------------------------------------------------------------------------
# Scenario 3
# ---------------------------------------------------------------------------


@pytest.mark.distributed
@pytest.mark.asyncio
async def test_only_one_holder_at_a_time(
    cluster_with_4_workers_election: TestCluster,
    postgres_container: str,
) -> None:
    """Exactly one process holds ROLE_TIMER_SCHEDULER at any point in time.

    Phase 1 — steady state observation (10s):
        Poll the leader_lease table every 200ms and assert holder_count <= 1.

    Phase 2 — kill the holder:
        SIGTERM whichever process currently holds the role.

    Phase 3 — failover observation (up to 45s):
        Continue polling until a new holder appears (or timeout).
        Simultaneously assert holder_count never exceeds 1.
    """
    try:
        import asyncpg  # noqa: PLC0415
    except ImportError:
        pytest.skip("asyncpg not installed")

    cluster = cluster_with_4_workers_election
    role = ROLE_TIMER_SCHEDULER
    schema = cluster.schema

    dsn = _asyncpg_dsn(postgres_container)
    conn = await asyncpg.connect(dsn)

    max_observed: int = 0
    violations: list[str] = []  # timestamped double-hold events

    def _record(count: int) -> None:
        nonlocal max_observed
        if count > max_observed:
            max_observed = count
        if count > 1:
            violations.append(
                f"t={time.monotonic():.2f}: {count} simultaneous holders"
            )

    try:
        # ------------------------------------------------------------------
        # Phase 1: observe steady state for 10s; wait for at least 1 holder.
        # ------------------------------------------------------------------
        initial_holder: str | None = None

        async def _has_holder() -> bool:
            nonlocal initial_holder
            initial_holder = await _current_owner(conn, schema, role)
            return initial_holder is not None

        # Allow up to 30s for the first leader to emerge (processes need
        # time to boot and win their first try_acquire attempt).
        await cluster.wait_for(_has_holder, timeout_s=30.0, interval_s=0.5)

        # Sample the table for 10 more seconds to confirm steady state.
        deadline = time.monotonic() + 10.0
        while time.monotonic() < deadline:
            count = await _count_holders(conn, schema, role)
            _record(count)
            await asyncio.sleep(0.2)

        # ------------------------------------------------------------------
        # Phase 2: identify the holder and SIGTERM it.
        # ------------------------------------------------------------------
        holder_owner_id = await _current_owner(conn, schema, role)
        assert holder_owner_id is not None, (
            "No holder found in leader_lease after steady-state observation"
        )

        # Map owner_id back to a process name.
        # The owner_id prefix is set by the cluster as "api-<schema>-<i>"
        # or "worker-<schema>-<i>" — match by substring.
        all_handles = cluster.apis + cluster.workers
        holder_handle = None
        for h in all_handles:
            owner_prefix = (
                f"api-{schema}-{cluster.apis.index(h)}"
                if h in cluster.apis
                else f"worker-{schema}-{cluster.workers.index(h)}"
            )
            if owner_prefix in holder_owner_id:
                holder_handle = h
                break

        if holder_handle is not None:
            await cluster.kill(holder_handle.name, signal.SIGTERM)
        # If we can't match (shouldn't happen in practice), we still
        # proceed with failover observation — the lease will expire
        # naturally and the test still validates max-1 semantics.

        # ------------------------------------------------------------------
        # Phase 3: wait for a NEW holder to emerge (up to 45s).
        # ------------------------------------------------------------------
        new_holder: str | None = None
        failover_timeout = 45.0
        start = time.monotonic()

        async def _new_holder_took_over() -> bool:
            nonlocal new_holder
            count = await _count_holders(conn, schema, role)
            _record(count)
            owner = await _current_owner(conn, schema, role)
            if owner is not None and owner != holder_owner_id:
                new_holder = owner
                return True
            return False

        try:
            await cluster.wait_for(
                _new_holder_took_over,
                timeout_s=failover_timeout,
                interval_s=0.2,
            )
        except TimeoutError:
            elapsed = time.monotonic() - start
            pytest.fail(
                f"No new holder for role {role!r} emerged within"
                f" {failover_timeout}s after killing {holder_owner_id!r}."
                f" Elapsed: {elapsed:.1f}s."
                f" Violations: {violations}."
            )

    finally:
        await conn.close()

    # ------------------------------------------------------------------
    # Final assertions
    # ------------------------------------------------------------------
    assert not violations, (
        f"Double-hold detected during test:\n" + "\n".join(violations)
    )
    assert new_holder is not None, (
        "Failover succeeded (wait_for passed) but new_holder was not set"
    )
    assert new_holder != holder_owner_id, (
        f"New holder {new_holder!r} is the same process we killed"
    )
