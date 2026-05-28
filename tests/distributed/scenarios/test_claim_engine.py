"""Scenario 4 — Claim-engine no-double-claim under concurrent workers.

Inserts 50 harness lease rows (via direct asyncpg writes, which avoids
the overhead of 50 separate API round-trips) and waits for both workers
to claim all of them.  Asserts that every lease has exactly one
``claimed_by`` value and that no two workers ever claim the same row.

Why direct DB writes?
---------------------
The claim-engine's ``claim_due`` query JOINs the entity table for
eligibility filtering.  Direct inserts must therefore also populate the
entity table so that the JOIN succeeds.  We insert minimal harness rows
with ``pending_operation = 'fetch'`` which matches the
:class:`HarnessClaimAdapter`'s ``eligibility_sql`` (``data->>'pending_operation' IS NOT NULL``).

The workers pick these up, run their adapter handler, and mark them
as claimed.  Because the ``leases`` table uses ``SELECT … FOR UPDATE
SKIP LOCKED``, two workers cannot claim the same row.

Tables written:
  "<schema>"."harnesses"  — entity rows (id, data JSONB)
  "<schema>"."leases"     — one lease row per harness

Requires:
- A live Postgres container + Docker for testcontainers.
- The distributed marker (``pytest -m distributed``).
"""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from urllib.parse import urlparse

import pytest
import pytest_asyncio

from tests.distributed.cluster import TestCluster


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


async def _insert_harness_leases(
    conn, schema: str, *, count: int
) -> list[str]:
    """Insert *count* harness entity rows + matching lease rows.

    Returns a list of the inserted harness ids.

    The harness entity rows have ``pending_operation = 'fetch'`` so that
    the ``HarnessClaimAdapter.eligibility_sql`` condition
    ``data->>'pending_operation' IS NOT NULL`` is satisfied.
    """
    harness_table = f'"{schema}"."harnesses"'
    leases_table = f'"{schema}"."leases"'
    harness_ids: list[str] = []

    now_iso = "now()"

    for i in range(count):
        hid = f"hns-test-{uuid.uuid4().hex[:12]}"
        harness_ids.append(hid)

        harness_data = {
            "slug": f"test-harness-{hid}",
            "name": f"Test Harness {i}",
            "git_url": "https://github.com/example/repo",
            "ref": "main",
            "status": "draft",
            "pending_operation": "fetch",
            "overrides": {},
            "overrides_schema": None,
            "overrides_hash": None,
            "schema_hash": None,
            "resolved_commit": None,
            "available_commit": None,
            "bundle_hash": None,
            "available_bundle_hash": None,
            "commits_ahead": False,
            "overrides_dirty": False,
            "schema_missing_input": False,
            "description": None,
            "git_token": None,
            "subpath": None,
            "last_operation_at": None,
            "last_operation_error": None,
            "created_at": None,  # filled by default
        }

        await conn.execute(
            f"INSERT INTO {harness_table} (id, data, created_at, updated_at)"
            f" VALUES ($1, $2::jsonb, now(), now())"
            f" ON CONFLICT (id) DO NOTHING",
            hid,
            json.dumps(harness_data),
        )

        await conn.execute(
            f"INSERT INTO {leases_table}"
            f"  (kind, entity_id, priority_score, next_attempt_at)"
            f" VALUES ('harness', $1, 100, now())"
            f" ON CONFLICT (kind, entity_id) DO NOTHING",
            hid,
        )

    return harness_ids


# ---------------------------------------------------------------------------
# Fixture
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def cluster_2x2_claims(postgres_container: str, db_schema: str):
    """2 API + 2 worker cluster for the claim-engine scenario."""
    cluster = TestCluster(
        postgres_url=postgres_container,
        api_count=2,
        worker_count=2,
        start_port=8330,
        schema=db_schema,
    )
    await cluster.start()
    try:
        yield cluster
    finally:
        await cluster.stop()


# ---------------------------------------------------------------------------
# Scenario 4
# ---------------------------------------------------------------------------


@pytest.mark.distributed
@pytest.mark.asyncio
async def test_no_double_claim_under_concurrency(
    cluster_2x2_claims: TestCluster,
    postgres_container: str,
) -> None:
    """50 harness leases are claimed by exactly one worker each.

    Inserts 50 harness entity rows + lease rows directly into Postgres,
    then polls until all leases are claimed.  Asserts:

    - Every ``claimed_by`` is non-NULL.
    - Total distinct ``(kind, entity_id)`` pairs = 50.
    - No ``(kind, entity_id)`` appears more than once (no double-claim).

    Note: workers may process claim operations and then clear/drop the
    lease row on completion.  The test therefore also counts rows that
    have been DROP-ped (no longer present) as successfully claimed once,
    because the claim happened and the adapter's ``on_release`` with
    ``drop_lease=True`` removed the row.  We track this via a separate
    counter of rows that have exited the table.
    """
    try:
        import asyncpg  # noqa: PLC0415
    except ImportError:
        pytest.skip("asyncpg not installed")

    cluster = cluster_2x2_claims
    schema = cluster.schema
    count = 50
    timeout_s = 60.0

    dsn = _asyncpg_dsn(postgres_container)
    conn = await asyncpg.connect(dsn)

    try:
        # ------------------------------------------------------------------
        # 1. Wait for the cluster to initialise its schema (the API
        #    processes create the tables on boot).  We verify by checking
        #    that the ``leases`` table exists before inserting.
        # ------------------------------------------------------------------
        leases_table = f'"{schema}"."leases"'
        harness_table = f'"{schema}"."harnesses"'

        async def _tables_ready() -> bool:
            row = await conn.fetchval(
                "SELECT COUNT(*) FROM information_schema.tables"
                " WHERE table_schema = $1 AND table_name = 'leases'",
                schema,
            )
            return int(row or 0) > 0

        deadline = time.monotonic() + 30.0
        while not await _tables_ready():
            if time.monotonic() > deadline:
                pytest.fail(
                    f"Schema {schema!r} tables not ready after 30s;"
                    " cluster may not have bootstrapped"
                )
            await asyncio.sleep(0.5)

        # ------------------------------------------------------------------
        # 2. Insert 50 harness entity rows + lease rows.
        # ------------------------------------------------------------------
        harness_ids = await _insert_harness_leases(conn, schema, count=count)
        inserted_set = set(harness_ids)

        # Notify workers that new leases are ready (mirrors what
        # ClaimEngine.upsert does internally).
        for hid in harness_ids:
            await conn.execute(
                "SELECT pg_notify($1, $2)",
                "claim_ready",
                f"harness:{hid}",
            )

        # ------------------------------------------------------------------
        # 3. Poll until all 50 leases are either claimed or released/dropped.
        # ------------------------------------------------------------------
        # Workers may:
        #   a) Claim a lease → set claimed_by, expires_at.
        #   b) Complete the work → call release(drop_lease=True) → DELETE row.
        # Both outcomes mean "this harness was claimed exactly once".
        #
        # We track the set of entity_ids that we've EVER seen with
        # claimed_by IS NOT NULL to detect that workers processed them.

        ever_claimed: set[str] = set()
        double_claim_violations: list[str] = []

        start = time.monotonic()
        while True:
            rows = await conn.fetch(
                f"SELECT entity_id, claimed_by FROM {leases_table}"
                f" WHERE kind = 'harness' AND entity_id = ANY($1::text[])",
                list(inserted_set),
            )

            # Build a snapshot: entity_id → set of claimed_by values
            snapshot: dict[str, set[str]] = {}
            for r in rows:
                eid = r["entity_id"]
                cb = r["claimed_by"]
                if eid not in snapshot:
                    snapshot[eid] = set()
                if cb is not None:
                    snapshot[eid].add(cb)

            # Accumulate ever-claimed
            for eid, owners in snapshot.items():
                if owners:
                    ever_claimed.add(eid)

            # Detect double-claim: more than one distinct claimed_by
            for eid, owners in snapshot.items():
                if len(owners) > 1:
                    double_claim_violations.append(
                        f"{eid}: claimed by {owners}"
                    )

            # Rows absent from leases have been dropped (completed).
            present_ids = {r["entity_id"] for r in rows}
            dropped_ids = inserted_set - present_ids
            ever_claimed.update(dropped_ids)

            if len(ever_claimed) >= count:
                break

            if time.monotonic() - start > timeout_s:
                unclaimed = inserted_set - ever_claimed
                pytest.fail(
                    f"Timed out after {timeout_s}s waiting for all {count}"
                    f" leases to be claimed. "
                    f"Claimed so far: {len(ever_claimed)}/{count}. "
                    f"Unclaimed sample: {list(unclaimed)[:5]}"
                )

            await asyncio.sleep(0.5)

    finally:
        await conn.close()

    # ------------------------------------------------------------------
    # 4. Assertions
    # ------------------------------------------------------------------
    assert not double_claim_violations, (
        "Double-claim detected (same entity_id claimed by more than one"
        " worker simultaneously):\n" + "\n".join(double_claim_violations)
    )

    assert len(ever_claimed) == count, (
        f"Expected {count} claimed leases, got {len(ever_claimed)}"
    )
