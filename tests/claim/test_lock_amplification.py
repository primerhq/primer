"""Lock-amplification + concurrent-workers integration test.

Spins up 10 concurrent workers each calling ``engine.claim_due(max_count=5)``
against a Postgres-backed pool of 20 leases (two kinds). Workers HOLD what
they claim until every worker has finished. Verifies:

* No double-claim - no lease is held by two workers at once. Claims are held
  (not released) while the workers race, so a lease appearing twice in the
  claim history can only mean the exclusivity guarantee broke. (Releasing
  immediately would make a legitimate re-claim after release look like a
  double-claim.)
* Total throughput >= 95 % (>= 19 of 20 leases claimed within 2 s).
* No permanently stuck locks - once the test releases everything, no lease
  has ``claimed_by IS NOT NULL AND expires_at > now()``.

Skipped unless PRIMER_TEST_POSTGRES_URL is set (same gate as the other
Postgres live tests in ``tests/claim/test_postgres_engine.py``).

The adapters' eligibility SQL references only the lease alias
(``l.kind IS NOT NULL``), but the claim query still INNER JOINs each
adapter's entity table, so an entity row is seeded for every lease. This
isolates the concurrency invariants from entity-side eligibility logic.
"""

from __future__ import annotations

import asyncio
import os
import time
from collections.abc import AsyncIterator
from urllib.parse import parse_qs, urlparse

import pytest
import pytest_asyncio

from primer.int.claim import ClaimAdapter, ClaimKind, ReleaseOutcome
from primer.claim.postgres import PostgresClaimEngine
from primer.model.provider import PoolConfig, PostgresConfig
from primer.storage.postgres import PostgresStorageProvider
from tests.claim._entity_seed import EntitySeeder


_URL_ENV = "PRIMER_TEST_POSTGRES_URL"

POSTGRES_AVAILABLE = bool(os.environ.get(_URL_ENV))

_needs_pg = pytest.mark.skipif(
    not POSTGRES_AVAILABLE,
    reason=f"set {_URL_ENV} to run Postgres lock-amplification tests",
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _parse_url(url: str) -> PostgresConfig:
    from primer.model.except_ import ConfigError

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
        pool=PoolConfig(min_size=2, max_size=12),
    )


# ---------------------------------------------------------------------------
# Synthetic adapters for the two kinds under test (entity rows are seeded)
# ---------------------------------------------------------------------------


class _SessionNoJoin(ClaimAdapter):
    kind = ClaimKind.SESSION
    entity_table = "sessions"

    def eligibility_sql(self) -> str:
        return "l.kind IS NOT NULL"

    async def on_release(self, conn, entity_id: str, *, outcome: ReleaseOutcome) -> None:
        pass


class _HarnessNoJoin(ClaimAdapter):
    kind = ClaimKind.HARNESS
    entity_table = "harnesses"

    def eligibility_sql(self) -> str:
        return "l.kind IS NOT NULL"

    async def on_release(self, conn, entity_id: str, *, outcome: ReleaseOutcome) -> None:
        pass


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def pg_storage_la() -> AsyncIterator[PostgresStorageProvider]:
    """Initialised PostgresStorageProvider; cleans up leases on entry/exit."""
    url = os.environ.get(_URL_ENV)
    if not url:
        pytest.skip(f"set {_URL_ENV} to run Postgres lock-amplification tests")

    cfg = _parse_url(url)
    sp = PostgresStorageProvider(cfg)
    await sp.initialize()

    # Start each test with empty leases table.
    async with sp.pool.acquire() as conn:
        await conn.execute(f"DELETE FROM {sp.leases_table}")

    try:
        yield sp
    finally:
        async with sp.pool.acquire() as conn:
            await conn.execute(f"DELETE FROM {sp.leases_table}")
        await sp.aclose()


@pytest_asyncio.fixture
async def pg_engine_la(
    pg_storage_la: PostgresStorageProvider,
) -> PostgresClaimEngine:
    """PostgresClaimEngine with a synthetic adapter per kind under test.

    (This dict used to list ``ClaimKind.HARNESS`` twice - a leftover from a
    removed kind - so the first adapter was silently shadowed; the test has
    only ever exercised these two kinds.)
    """
    adapters = {
        ClaimKind.SESSION: _SessionNoJoin(),
        ClaimKind.HARNESS: _HarnessNoJoin(),
    }
    return PostgresClaimEngine(storage_provider=pg_storage_la, adapters=adapters)


@pytest_asyncio.fixture
async def entity_seeder_la(
    pg_storage_la: PostgresStorageProvider,
) -> AsyncIterator[EntitySeeder]:
    seeder = EntitySeeder(pg_storage_la)
    try:
        yield seeder
    finally:
        await seeder.cleanup()


# ---------------------------------------------------------------------------
# Test
# ---------------------------------------------------------------------------


_TOTAL_LEASES = 20
_NUM_WORKERS = 10
_MAX_PER_WORKER = 5
_TIME_LIMIT_S = 2.0
_THROUGHPUT_THRESHOLD = 0.95  # 95 % of 20 leases = 19


def _make_lease_ids() -> list[tuple[ClaimKind, str]]:
    """Return 20 (kind, entity_id) pairs - 10 harness, 10 session."""
    items: list[tuple[ClaimKind, str]] = []
    kinds = [ClaimKind.HARNESS, ClaimKind.SESSION]
    for i in range(_TOTAL_LEASES):
        kind = kinds[i % len(kinds)]
        items.append((kind, f"la-{kind.value}-{i:02d}"))
    return items


@_needs_pg
@pytest.mark.asyncio
async def test_concurrent_workers_no_double_claim(
    pg_engine_la: PostgresClaimEngine,
    pg_storage_la: PostgresStorageProvider,
    entity_seeder_la: EntitySeeder,
) -> None:
    """10 concurrent workers racing over 20 leases.

    Invariants checked:
    - No double-claim: no lease is held by two workers at once. Workers HOLD
      their claims until all have finished, so a lease claimed twice can only
      mean the exclusivity guarantee broke (an immediate release would make
      a legitimate re-claim look like a double-claim).
    - Throughput >= 95 % (>= 19 leases claimed within _TIME_LIMIT_S).
    - No permanently stuck locks once everything is released.
    """
    engine = pg_engine_la
    storage = pg_storage_la

    # Seed 20 leases with mixed kinds, plus the entity row each one's
    # INNER JOIN needs (a lease without one is never claimable).
    lease_ids = _make_lease_ids()
    for entity_table, kind in (("sessions", ClaimKind.SESSION), ("harnesses", ClaimKind.HARNESS)):
        await entity_seeder_la.seed(
            entity_table, [eid for k, eid in lease_ids if k == kind],
        )
    for kind, entity_id in lease_ids:
        await engine.upsert(kind, entity_id, priority=100)

    # Each worker loops until it has claimed at least one lease, every lease
    # is held, or time elapses. Claims are held, not released.
    claimed_by_worker: dict[str, list[str]] = {}
    held_leases: list = []

    async def _worker(worker_id: str) -> None:
        deadline = time.monotonic() + _TIME_LIMIT_S
        my_claims: list[str] = []
        while time.monotonic() < deadline and len(held_leases) < _TOTAL_LEASES:
            leases = await engine.claim_due(worker_id, max_count=_MAX_PER_WORKER)
            for lease in leases:
                my_claims.append(f"{lease.kind.value}:{lease.entity_id}")
                held_leases.append(lease)
            if my_claims:
                # Each worker only needs one successful batch to confirm it works.
                break
            # Brief back-off before retrying.
            await asyncio.sleep(0.02)
        claimed_by_worker[worker_id] = my_claims

    # Launch all workers concurrently.
    await asyncio.gather(*[_worker(f"worker-{i:02d}") for i in range(_NUM_WORKERS)])

    # ------------------------------------------------------------------
    # Query final state directly from the leases table.
    # ------------------------------------------------------------------
    async with storage.pool.acquire() as conn:
        rows = await conn.fetch(
            f"SELECT kind, entity_id, claimed_by, expires_at FROM {storage.leases_table} "
            f"WHERE entity_id LIKE 'la-%'"
        )

    lease_map: dict[str, str | None] = {
        f"{r['kind']}:{r['entity_id']}": r["claimed_by"] for r in rows
    }

    # ------------------------------------------------------------------
    # Invariant 1: No double-claim.
    # Claims are held until the end, so each lease must appear at most once
    # across all workers, and the lease row must name the worker that got it.
    # ------------------------------------------------------------------
    all_claimed_flat: list[str] = [
        item for claims in claimed_by_worker.values() for item in claims
    ]
    seen: set[str] = set()
    doubles: list[str] = []
    for item in all_claimed_flat:
        if item in seen:
            doubles.append(item)
        seen.add(item)
    assert not doubles, (
        f"Double-claims detected (same lease claimed by multiple workers): {doubles}"
    )
    wrong_owner = [
        f"{item}: row says {lease_map.get(item)!r}, claimed by {worker}"
        for worker, claims in claimed_by_worker.items()
        for item in claims
        if lease_map.get(item) != worker
    ]
    assert not wrong_owner, f"Lease rows disagree with who claimed them: {wrong_owner}"

    # ------------------------------------------------------------------
    # Invariant 2: Throughput ≥ 95 %.
    # Count distinct leases that were claimed at least once by any worker.
    # ------------------------------------------------------------------
    distinct_claimed = len(seen)
    min_required = int(_TOTAL_LEASES * _THROUGHPUT_THRESHOLD)
    assert distinct_claimed >= min_required, (
        f"Throughput too low: {distinct_claimed}/{_TOTAL_LEASES} leases claimed "
        f"(need ≥ {min_required} = {_THROUGHPUT_THRESHOLD:.0%} of {_TOTAL_LEASES}). "
        f"Worker breakdown: { {wid: len(v) for wid, v in claimed_by_worker.items()} }"
    )

    # ------------------------------------------------------------------
    # Invariant 3: No permanently stuck locks.
    # Release everything the workers held (drop_lease=False), after which
    # claimed_by should be NULL for all rows. A "stuck" lease is one still
    # showing claimed_by IS NOT NULL with expires_at > now() - a lease
    # that release failed to free.
    # ------------------------------------------------------------------
    for lease in held_leases:
        await engine.release(
            lease, outcome=ReleaseOutcome(success=True, drop_lease=False),
        )
    async with storage.pool.acquire() as conn:
        stuck = await conn.fetch(
            f"SELECT kind, entity_id, claimed_by, expires_at "
            f"FROM {storage.leases_table} "
            f"WHERE entity_id LIKE 'la-%' "
            f"  AND claimed_by IS NOT NULL "
            f"  AND expires_at > now()"
        )

    assert not stuck, (
        "Permanently stuck leases found (claimed but not released): "
        + ", ".join(f"{r['kind']}:{r['entity_id']} → {r['claimed_by']}" for r in stuck)
    )
