"""Postgres-backed :class:`ClaimEngine`.

Only :meth:`upsert`, :meth:`delete_lease`, and :meth:`claim_due` are
implemented here (Task 11). The remaining methods
(``heartbeat``, ``release``, ``mark_resumable``, ``watch_ready``) raise
:exc:`NotImplementedError` and are filled in during Task 12.

SQL notes
---------
*upsert*: The ``ON CONFLICT (kind, entity_id) DO UPDATE`` target is a
schema-qualified table (e.g. ``"matrix"."leases"``).  Within the
``DO UPDATE SET`` clause Postgres allows referencing the *existing*
row by the **unqualified** table name (``leases.next_attempt_at``).
We avoid that ambiguity entirely by aliasing the insert target with
``AS le`` and using ``le.next_attempt_at`` for the existing row.
``EXCLUDED.next_attempt_at`` refers to the *proposed* value from the
INSERT (i.e. ``COALESCE($4, now())``).

*claim_due*: delegates to :func:`matrix.claim.sql.build_claim_query`,
which composes one CTE per adapter via UNION ALL and then drives a
single ``UPDATE … RETURNING``.  The query is built once at engine
construction time.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import datetime
from typing import Any

from matrix.int.claim import ClaimAdapter, ClaimEngine, ClaimKind, Lease, ReleaseOutcome
from matrix.claim.sql import build_claim_query


class PostgresClaimEngine(ClaimEngine):
    """Postgres-backed claim engine.

    Parameters
    ----------
    storage_provider:
        An initialised :class:`~matrix.storage.postgres.PostgresStorageProvider`.
        Must expose ``.pool`` (asyncpg pool) and ``.leases_table``
        (schema-qualified name) and ``.schema`` (bare schema name).
    adapters:
        Mapping of :class:`ClaimKind` → :class:`ClaimAdapter` instances.
        Adapters whose eligibility SQL references their entity table are
        included in the ``claim_due`` CTE composition.
    """

    def __init__(
        self,
        *,
        storage_provider: Any,
        adapters: dict[ClaimKind, ClaimAdapter],
    ) -> None:
        self._storage = storage_provider
        self._adapters = adapters
        self._table = storage_provider.leases_table
        # Build once; claim_due uses it on every call.
        self._claim_query = build_claim_query(
            adapters,
            self._table,
            schema=storage_provider.schema,
        )

    # ------------------------------------------------------------------
    # upsert
    # ------------------------------------------------------------------

    async def upsert(
        self,
        kind: ClaimKind,
        entity_id: str,
        *,
        priority: int = 100,
        next_attempt_at: datetime | None = None,
    ) -> None:
        """Insert or update a lease row.

        On conflict (kind, entity_id):
        - Always update ``priority_score`` to the new value.
        - Update ``next_attempt_at`` only when ``$4`` is non-NULL;
          otherwise preserve the existing value (``le.next_attempt_at``).

        The ``AS le`` alias on the INSERT target is used to reference
        the *existing* row in the DO UPDATE SET clause, avoiding any
        ambiguity with the unqualified table name in schema-qualified
        contexts.
        """
        async with self._storage.pool.acquire() as conn:
            await conn.execute(
                f"INSERT INTO {self._table} AS le (kind, entity_id, priority_score, next_attempt_at)"
                f" VALUES ($1, $2, $3, COALESCE($4, now()))"
                f" ON CONFLICT (kind, entity_id) DO UPDATE"
                f"   SET priority_score   = EXCLUDED.priority_score,"
                f"       next_attempt_at  = COALESCE($4, le.next_attempt_at)",
                kind.value, entity_id, priority, next_attempt_at,
            )
            await conn.execute(
                "SELECT pg_notify($1, $2)",
                "claim_ready",
                f"{kind.value}:{entity_id}",
            )

    # ------------------------------------------------------------------
    # delete_lease
    # ------------------------------------------------------------------

    async def delete_lease(self, kind: ClaimKind, entity_id: str) -> None:
        """Remove the lease row for (kind, entity_id).  No-op if missing."""
        async with self._storage.pool.acquire() as conn:
            await conn.execute(
                f"DELETE FROM {self._table} WHERE kind = $1 AND entity_id = $2",
                kind.value, entity_id,
            )

    # ------------------------------------------------------------------
    # claim_due
    # ------------------------------------------------------------------

    async def claim_due(self, worker_id: str, *, max_count: int) -> list[Lease]:
        """Atomically claim up to *max_count* due leases.

        Uses the pre-compiled UNION ALL CTE query that joins each
        adapter's entity table for eligibility filtering.
        """
        async with self._storage.pool.acquire() as conn:
            rows = await conn.fetch(
                self._claim_query,
                max_count,
                worker_id,
                "60",  # TTL in seconds (string cast to interval)
            )
        return [
            Lease(
                kind=ClaimKind(r["kind"]),
                entity_id=r["entity_id"],
                claimed_by=worker_id,
                claimed_at=r["claimed_at"],
                expires_at=r["expires_at"],
                attempt_count=r["attempt_count"],
                last_error=r["last_error"],
            )
            for r in rows
        ]

    # ------------------------------------------------------------------
    # Remaining ABC methods — implemented in Task 12
    # ------------------------------------------------------------------

    async def heartbeat(
        self,
        worker_id: str,
        kind_ids: list[tuple[ClaimKind, str]],
    ) -> list[tuple[ClaimKind, str]]:
        raise NotImplementedError("heartbeat is implemented in Task 12")

    async def release(self, lease: Lease, *, outcome: ReleaseOutcome) -> None:
        raise NotImplementedError("release is implemented in Task 12")

    async def mark_resumable(
        self, kind: ClaimKind, entity_id: str, *, priority: int = 50,
    ) -> None:
        raise NotImplementedError("mark_resumable is implemented in Task 12")

    async def watch_ready(self) -> AsyncIterator[tuple[ClaimKind, str]]:
        raise NotImplementedError("watch_ready is implemented in Task 12")
        yield  # make this an async generator for ABC compatibility
