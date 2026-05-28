"""Postgres-backed :class:`ClaimEngine`.

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

*heartbeat*: uses ``UNNEST($1::text[], $2::text[])`` to bulk-match
``(kind, entity_id)`` pairs and only touches rows owned by the
caller (``claimed_by = $3``).  Returns the confirmed pairs.

*release*: runs inside a single Postgres transaction so the lease
mutation and the adapter's ``on_release`` entity-side update are
atomic.  ``drop_lease=True`` → DELETE; otherwise clears claim fields,
resets ``next_attempt_at`` from ``requeue_after``, bumps
``attempt_count`` / ``last_error`` on failure.

*mark_resumable*: ``INSERT … ON CONFLICT DO UPDATE`` that sets
priority and resets ``next_attempt_at = now()``, then notifies the
``claim_ready`` channel.  Uses the ``AS le`` alias trick (same as
upsert).

*watch_ready*: acquires a **dedicated** connection from the pool and
keeps it for the generator's lifetime.  The ``claim_ready`` listener
callback calls ``queue.put_nowait(payload)`` — asyncpg invokes the
callback from within the event-loop read loop, so ``put_nowait`` is
safe without ``call_soon_threadsafe``.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import datetime
from typing import Any

from primer.int.claim import ClaimAdapter, ClaimEngine, ClaimKind, Lease, ReleaseOutcome
from primer.claim.sql import build_claim_query
from primer.observability import tracing as _tracing
import primer.observability.metrics as _metrics


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
        _tracer = _tracing.get_tracer("primer.claim")
        with _tracer.start_as_current_span("claim.due") as _span:
            async with self._storage.pool.acquire() as conn:
                rows = await conn.fetch(
                    self._claim_query,
                    max_count,
                    worker_id,
                    "60",  # TTL in seconds (string cast to interval)
                )
            leases = [
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
            _span.set_attribute("claim.count", len(leases))
            for lease in leases:
                # claimed_at is the moment of claiming; we don't have
                # next_attempt_at in the returned Lease, so we observe 0
                # as the latency placeholder for Postgres-claimed leases.
                _metrics.claim_enqueue_latency_seconds.labels(
                    lease.kind.value
                ).observe(0.0)
                _span.add_event("claim_assigned", {"kind": lease.kind.value})
            return leases

    # ------------------------------------------------------------------
    # heartbeat
    # ------------------------------------------------------------------

    async def heartbeat(
        self,
        worker_id: str,
        kind_ids: list[tuple[ClaimKind, str]],
    ) -> list[tuple[ClaimKind, str]]:
        """Refresh the TTL for every (kind, entity_id) pair we own.

        Uses ``UNNEST`` to pass all pairs in one round-trip.  Only rows
        whose ``claimed_by`` matches ``worker_id`` are touched; the
        ``RETURNING`` clause reports which rows were actually updated so
        the caller knows which pairs were confirmed.

        Returns an empty list immediately when ``kind_ids`` is empty.
        """
        if not kind_ids:
            return []
        kinds = [k.value for k, _ in kind_ids]
        ids = [eid for _, eid in kind_ids]
        async with self._storage.pool.acquire() as conn:
            rows = await conn.fetch(
                f"UPDATE {self._table}"
                f"   SET last_heartbeat_at = now(),"
                f"       expires_at = now() + interval '60 seconds'"
                f" WHERE (kind, entity_id) IN"
                f"       (SELECT * FROM UNNEST($1::text[], $2::text[]))"
                f"   AND claimed_by = $3"
                f" RETURNING kind, entity_id",
                kinds, ids, worker_id,
            )
        return [(ClaimKind(r["kind"]), r["entity_id"]) for r in rows]

    # ------------------------------------------------------------------
    # release
    # ------------------------------------------------------------------

    async def release(self, lease: Lease, *, outcome: ReleaseOutcome) -> None:
        """Release a lease, optionally calling the adapter's on_release hook.

        Both the lease mutation and the adapter's ``on_release`` call
        run inside a single Postgres transaction so entity-state and
        lease-state changes are atomic.

        Parameters
        ----------
        lease:
            The lease returned by :meth:`claim_due`.
        outcome:
            Controls whether to drop the row (``drop_lease=True``) or
            reset it for re-queuing, and carries success / error
            metadata used to update ``attempt_count`` / ``last_error``.
        """
        requeue_secs = (
            outcome.requeue_after.total_seconds()
            if outcome.requeue_after is not None
            else 0
        )
        async with self._storage.pool.acquire() as conn:
            async with conn.transaction():
                if outcome.drop_lease:
                    await conn.execute(
                        f"DELETE FROM {self._table}"
                        f" WHERE kind = $1 AND entity_id = $2",
                        lease.kind.value, lease.entity_id,
                    )
                else:
                    await conn.execute(
                        f"UPDATE {self._table}"
                        f"   SET claimed_by        = NULL,"
                        f"       claimed_at         = NULL,"
                        f"       last_heartbeat_at  = NULL,"
                        f"       expires_at         = NULL,"
                        f"       next_attempt_at    = now() + ($3 || ' seconds')::interval,"
                        f"       attempt_count      = CASE WHEN $4 THEN 0"
                        f"                                 ELSE attempt_count + 1 END,"
                        f"       last_error         = CASE WHEN $4 THEN NULL"
                        f"                                 ELSE $5 END"
                        f" WHERE kind = $1 AND entity_id = $2",
                        lease.kind.value, lease.entity_id,
                        str(requeue_secs), outcome.success, outcome.last_error,
                    )
                adapter = self._adapters.get(lease.kind)
                if adapter is not None:
                    await adapter.on_release(
                        conn, lease.entity_id, outcome=outcome,
                    )

    # ------------------------------------------------------------------
    # mark_resumable
    # ------------------------------------------------------------------

    async def mark_resumable(
        self, kind: ClaimKind, entity_id: str, *, priority: int = 50,
    ) -> None:
        """Upsert a lease as resumable (low priority) and broadcast a notification.

        If a row already exists its ``priority_score`` is lowered to
        ``priority`` and ``next_attempt_at`` is reset to ``now()`` so
        the row becomes immediately claimable.  If no row exists a new
        one is inserted with the given priority.

        ``AS le`` aliases the INSERT target so ``DO UPDATE SET`` can
        reference the existing row via ``le.*`` to avoid ambiguity with
        schema-qualified names.

        After the upsert a ``pg_notify('claim_ready', ...)`` wakes any
        :meth:`watch_ready` consumers.
        """
        async with self._storage.pool.acquire() as conn:
            await conn.execute(
                f"INSERT INTO {self._table} AS le"
                f"  (kind, entity_id, priority_score, next_attempt_at)"
                f" VALUES ($1, $2, $3, now())"
                f" ON CONFLICT (kind, entity_id) DO UPDATE"
                f"   SET priority_score  = EXCLUDED.priority_score,"
                f"       next_attempt_at = now()",
                kind.value, entity_id, priority,
            )
            await conn.execute(
                "SELECT pg_notify($1, $2)",
                "claim_ready",
                f"{kind.value}:{entity_id}",
            )

    # ------------------------------------------------------------------
    # watch_ready
    # ------------------------------------------------------------------

    async def watch_ready(self) -> AsyncIterator[tuple[ClaimKind, str]]:  # type: ignore[override]
        """Yield (ClaimKind, entity_id) tuples as they arrive via LISTEN/NOTIFY.

        Opens a **dedicated** connection from the pool and keeps it for
        the generator's lifetime (LISTEN is per-connection state in
        Postgres).  The asyncpg listener callback is invoked from within
        the event-loop's read loop, so ``queue.put_nowait`` is safe
        without ``loop.call_soon_threadsafe``.

        The generator cleans up by removing the listener and releasing
        the connection when it is closed (``aclose()`` or the enclosing
        ``async for`` exits).
        """
        queue: asyncio.Queue[str] = asyncio.Queue()

        def _on_notify(
            conn: Any,
            pid: int,
            channel: str,
            payload: str,
        ) -> None:
            queue.put_nowait(payload)

        conn = await self._storage.pool.acquire()
        try:
            await conn.add_listener("claim_ready", _on_notify)
            try:
                while True:
                    payload = await queue.get()
                    kind_str, entity_id = payload.split(":", 1)
                    yield (ClaimKind(kind_str), entity_id)
            finally:
                await conn.remove_listener("claim_ready", _on_notify)
        finally:
            await self._storage.pool.release(conn)
