"""Scenario 6 — Auto-bootstrap exclusivity across racing API processes.

Both API processes start simultaneously on a fresh schema (no existing
rows).  Each process runs its own bootstrap check on startup. Because
the bootstrap uses a combination of an idempotent ``INSERT … ON
CONFLICT DO NOTHING`` guard and a ``system_state.bootstrap_completed_at``
marker, only one of the two startup sequences should write the four
reserved-id rows even under a simultaneous race.

What we assert
--------------
After both APIs are healthy (cluster ready):

1. ``system_state.bootstrap_completed_at IS NOT NULL`` — the marker
   was stamped by exactly one winner.

2. The four bootstrapped provider tables each have exactly **one** row
   with the reserved id:

   * ``embeddingprovider`` — id = ``'huggingface'``
   * ``workspaceprovider`` — id = ``'local'``
   * ``semanticsearchprovider`` — id = ``'lance'``
   * ``crossencoderprovider`` — id = ``'huggingface-ce'``

   A count of 2 would indicate that two simultaneous bootstrap runs
   each wrote the row before either saw the other's write — a bug in
   the exclusivity guard.

The ``fresh_cluster_2x2`` fixture (defined in ``conftest.py``) starts
both API processes with ``MATRIX_AUTO_BOOTSTRAP=true`` on a brand-new
schema, making this an end-to-end exclusivity test.

Requires:
- A live Postgres container + Docker for testcontainers.
- The distributed marker (``pytest -m distributed``).
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
    p = urlparse(pg_url)
    host = p.hostname or "localhost"
    port = p.port or 5432
    user = p.username or "postgres"
    password = p.password or ""
    db = (p.path or "/postgres").lstrip("/") or "postgres"
    return f"postgresql://{user}:{password}@{host}:{port}/{db}"


# ---------------------------------------------------------------------------
# Scenario 6
# ---------------------------------------------------------------------------


@pytest.mark.distributed
@pytest.mark.asyncio
async def test_only_one_bootstrap_runs_on_fresh_db(
    fresh_cluster_2x2: TestCluster,
    postgres_container: str,
) -> None:
    """Bootstrap runs exactly once when two APIs race on a fresh schema.

    Phase 1 — wait for the bootstrap marker:
        Poll ``system_state.bootstrap_completed_at`` until it is
        non-NULL (or 60s timeout).

    Phase 2 — assert uniqueness:
        Each of the four reserved-provider tables must have exactly one
        row with the expected reserved id, not two.
    """
    try:
        import asyncpg  # noqa: PLC0415
    except ImportError:
        pytest.skip("asyncpg not installed")

    cluster = fresh_cluster_2x2
    schema = cluster.schema

    dsn = _asyncpg_dsn(postgres_container)
    conn = await asyncpg.connect(dsn)

    system_state_table = f'"{schema}"."system_state"'

    try:
        # ------------------------------------------------------------------
        # Phase 1: wait for bootstrap_completed_at to be stamped.
        # Both APIs boot with MATRIX_AUTO_BOOTSTRAP=true; one of them
        # will win the race and stamp the marker.  Allow up to 60s.
        # ------------------------------------------------------------------
        deadline = time.monotonic() + 60.0

        bootstrap_completed_at = None
        while True:
            try:
                row = await conn.fetchrow(
                    f"SELECT bootstrap_completed_at FROM {system_state_table}"
                    f" WHERE id = 'singleton'"
                )
                if row is not None:
                    bootstrap_completed_at = row["bootstrap_completed_at"]
                    if bootstrap_completed_at is not None:
                        break
            except asyncpg.UndefinedTableError:
                pass  # schema not yet initialised; keep waiting

            if time.monotonic() > deadline:
                pytest.fail(
                    f"system_state.bootstrap_completed_at was not stamped"
                    f" within 60s on schema {schema!r}. Bootstrap may not"
                    f" have run, or both API processes may have failed."
                )
            await asyncio.sleep(0.5)

        assert bootstrap_completed_at is not None, (
            "bootstrap_completed_at should be non-NULL after the loop"
        )

        # ------------------------------------------------------------------
        # Phase 2: assert that each reserved-provider table has exactly
        # one row with the expected id.
        # ------------------------------------------------------------------
        checks: list[tuple[str, str]] = [
            ("embeddingprovider", "huggingface"),
            ("workspaceprovider", "local"),
            ("semanticsearchprovider", "lance"),
            ("crossencoderprovider", "huggingface-ce"),
        ]

        duplicates: list[str] = []
        missing: list[str] = []

        for table_suffix, reserved_id in checks:
            table = f'"{schema}"."{table_suffix}"'
            try:
                count = await conn.fetchval(
                    f"SELECT COUNT(*) FROM {table} WHERE id = $1",
                    reserved_id,
                )
                count = int(count or 0)
                if count == 0:
                    missing.append(
                        f"{table_suffix}.id={reserved_id!r} (count=0)"
                    )
                elif count > 1:
                    duplicates.append(
                        f"{table_suffix}.id={reserved_id!r} (count={count})"
                        " — bootstrap ran more than once"
                    )
                # count == 1 is the expected outcome; no action needed.
            except asyncpg.UndefinedTableError:
                missing.append(
                    f"{table_suffix}.id={reserved_id!r} (table does not exist)"
                )

    finally:
        await conn.close()

    # ------------------------------------------------------------------
    # Assertions
    # ------------------------------------------------------------------
    assert not duplicates, (
        "Bootstrap exclusivity violated — reserved rows were written more"
        " than once (two simultaneous bootstrap runs both completed):\n"
        + "\n".join(duplicates)
    )
    assert not missing, (
        "Bootstrap did not create expected reserved rows:\n"
        + "\n".join(missing)
    )
