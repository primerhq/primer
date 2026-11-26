"""Postgres-backed :class:`ClaimEngine`.

SQL notes
---------
*upsert*: The ``ON CONFLICT (kind, entity_id) DO UPDATE`` target is a
schema-qualified table (e.g. ``"primer"."leases"``).  Within the
``DO UPDATE SET`` clause Postgres allows referencing the *existing*
row by the **unqualified** table name (``leases.next_attempt_at``).
We avoid that ambiguity entirely by aliasing the insert target with
``AS le`` and using ``le.next_attempt_at`` for the existing row.
``EXCLUDED.next_attempt_at`` refers to the *proposed* value from the
INSERT (i.e. ``COALESCE($4, now())``).

*claim_due*: delegates to :func:`primer.claim.sql.build_claim_query`,
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
safe without ``call_soon_threadsafe``.  A lost connection surfaces as a
``ListenConnectionLost`` out of the generator (see :meth:`PostgresClaimEngine.watch_ready`).
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from collections.abc import AsyncIterator, Sequence
from datetime import datetime
from typing import Any

from primer.int.claim import (
    CLAIM_PRIORITY_FRESH, CLAIM_PRIORITY_RESUME, ClaimAdapter, ClaimEngine, ClaimKind, Lease,
    ReleaseOutcome,
)
from primer.claim.sql import build_claim_query
from primer.claim.statement_stamp import claim_statement_at
from primer.model.work_priority import validate_work_priority
from primer.model.except_ import ListenConnectionLost
from primer.storage._ddl import execute_create_idempotent
from primer.observability import tracing as _tracing
import primer.observability.metrics as _metrics

logger = logging.getLogger(__name__)


class PostgresClaimEngine(ClaimEngine):
    """Postgres-backed claim engine.

    Parameters
    ----------
    storage_provider:
        An initialised :class:`~primer.storage.postgres.PostgresStorageProvider`.
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
        lease_ttl_seconds: int = 60,
    ) -> None:
        self._storage = storage_provider
        self._adapters = adapters
        self._table = storage_provider.leases_table
        # Lease TTL (seconds) fed into the claim_due + heartbeat SQL intervals.
        # Defaults to 60s for a standalone engine; the worker pool overrides it
        # from WorkerConfig.lease_ttl_seconds at start() so the
        # lease_ttl >= 2*heartbeat validator actually governs the engine
        # (arch review A-I1).
        self.lease_ttl_seconds = lease_ttl_seconds
        # Build once; claim_due uses it on every call (the common,
        # unfiltered case - kinds=None).
        self._claim_query = build_claim_query(
            adapters,
            self._table,
            schema=storage_provider.schema,
        )
        # Phase 3 stage 7a (01a0518b) pool-class separation: claim_due's
        # optional kinds filter needs a DIFFERENT compiled query (a
        # different CTE/UNION subset), not a runtime WHERE clause bolted
        # onto the shared one - so cache one compiled query per distinct
        # kind-subset actually requested, keyed by frozenset(kinds).
        # Real callers only ever ask for a small, fixed number of
        # subsets (e.g. "everything but TOOL_CALL" / "TOOL_CALL only"),
        # so this cache never grows unbounded. Seeded with the
        # already-built unfiltered query for the None key.
        self._claim_query_cache: dict[frozenset[ClaimKind] | None, str] = {
            None: self._claim_query,
        }
        # Entity tables are created lazily by the storage layer on first write.
        # The claim query JOINs them unconditionally, so on a fresh DB (before
        # any chat/harness/trigger exists) the JOIN target is missing and the
        # whole query fails with UndefinedTableError. Ensure they exist once,
        # on the first claim, with the same standard (id, data jsonb, ...) shape
        # the storage layer uses.
        self._entity_tables = sorted(
            {a.entity_table for a in adapters.values()}
        )
        if ClaimKind.TOOL_CALL in adapters and "sessions" not in self._entity_tables:
            self._entity_tables.append("sessions")
        self._entity_tables_ensured = False

    def _claim_query_for(self, kinds: list[ClaimKind] | None) -> str:
        """The compiled claim_due query for ``kinds`` (None = every adapter).

        Builds + caches a query scoped to the given kind subset on first
        use; every later call with an equal subset hits the cache.
        """
        if kinds is None:
            return self._claim_query
        key = frozenset(kinds)
        cached = self._claim_query_cache.get(key)
        if cached is not None:
            return cached
        scoped_adapters = {
            kind: adapter for kind, adapter in self._adapters.items()
            if kind in key
        }
        query = build_claim_query(
            scoped_adapters, self._table, schema=self._storage.schema,
        )
        self._claim_query_cache[key] = query
        return query

    def _qualified_entity(self, table: str) -> str:
        schema = self._storage.schema
        return f'"{schema}"."{table}"' if schema else f'"{table}"'

    async def _ensure_entity_tables(self, conn: Any) -> None:
        for table in self._entity_tables:
            qualified = self._qualified_entity(table)
            # Each statement is its own autocommit transaction. A peer's
            # background CREATE INDEX CONCURRENTLY on the same table (lane N's
            # index builder) can deadlock this DDL while holding the table
            # lock: the helper retries it in a fresh transaction, and a peer
            # racing the same object is swallowed by it (see
            # primer.storage._ddl).
            await execute_create_idempotent(
                conn,
                f"CREATE TABLE IF NOT EXISTS {qualified} ("
                "id text PRIMARY KEY, "
                "data jsonb NOT NULL, "
                "created_at timestamptz NOT NULL DEFAULT now(), "
                "updated_at timestamptz NOT NULL DEFAULT now()"
                ")",
            )
            # Create any adapter-declared backing indexes for this table.
            # Each is CREATE INDEX IF NOT EXISTS (idempotent); peer races and
            # deadlocks are handled by the same helper.
            for adapter in self._adapters.values():
                if adapter.entity_table != table:
                    continue
                for ddl in adapter.entity_indexes(qualified):
                    await execute_create_idempotent(conn, ddl)
        self._entity_tables_ensured = True

    # ------------------------------------------------------------------
    # upsert
    # ------------------------------------------------------------------

    async def upsert(
        self,
        kind: ClaimKind,
        entity_id: str,
        *,
        priority: int = CLAIM_PRIORITY_FRESH,
        next_attempt_at: datetime | None = None,
    ) -> None:
        """Insert or update a lease row, snapshotting the entity's work priority.

        The arm reads the entity's authoritative priority (``_arm_priority``);
        a missing or malformed source refuses the arm rather than promoting
        unknown work.
        """
        await self._arm_priority(kind, entity_id, priority=priority, next_attempt_at=next_attempt_at)

    # ------------------------------------------------------------------
    # delete_lease
    # ------------------------------------------------------------------

    async def _arm_priority(self, kind, entity_id, *, priority, next_attempt_at=None, resume=False):
        adapter = self._adapters[kind]
        expression = adapter.work_priority_sql(schema=self._storage.schema)
        due = "EXCLUDED.next_attempt_at" if resume else "COALESCE($4, le.next_attempt_at)"
        async with self._storage.pool.acquire() as conn:
            if not self._entity_tables_ensured:
                await self._ensure_entity_tables(conn)
            async with conn.transaction():
                # Serialize priority-enabled arms before their statement snapshots.
                # No entity row lock: release takes lease then entity, so taking
                # them in reverse order here would introduce a deadlock.
                await conn.execute("SELECT pg_advisory_xact_lock(hashtextextended($1, 0))",
                                   f"{self._table}:{kind.value}:{entity_id}:work-priority")
                written = await conn.fetchval(
                    f"INSERT INTO {self._table} AS le "
                    "(kind, entity_id, priority_score, work_priority, next_attempt_at) "
                    f"SELECT $1, $2, $3, ({expression}), COALESCE($4::timestamptz, now()) "
                    f"FROM {self._qualified_entity(adapter.entity_table)} e WHERE e.id=$2 "
                    "ON CONFLICT (kind, entity_id) DO UPDATE SET "
                    "priority_score=EXCLUDED.priority_score, work_priority=EXCLUDED.work_priority, "
                    f"next_attempt_at={due} RETURNING 1",
                    kind.value, entity_id, priority, next_attempt_at,
                )
                if written is None:
                    raise ValueError("cannot arm work priority for a missing entity")
                await conn.execute("SELECT pg_notify('claim_ready', $1)", f"{kind.value}:{entity_id}")

    @asynccontextmanager
    async def hold_memory_claim(self, lease: Lease, *, conn):
        from primer.model.memory import MemoryJob
        from primer.storage._locking import require_transaction

        db = require_transaction(conn, self._storage, MemoryJob)
        owned = await db.fetchval(
            f"SELECT 1 FROM {self._table} WHERE kind='memory' AND entity_id=$1 AND claimed_by=$2 "
            "AND memory_claim_id=$3 AND memory_occurrence IS NOT DISTINCT FROM $4 FOR UPDATE",
            lease.entity_id, lease.claimed_by, lease.memory_claim_id, lease.memory_occurrence,
        )
        yield bool(owned)

    async def arm_memory_job(self, job, *, conn) -> None:
        # Use this provider's already checked-out connection. Acquiring another
        # pool slot while holding the job lock could starve the entire pool.
        from primer.model.memory import MemoryJob
        from primer.storage._locking import require_transaction

        db = require_transaction(conn, self._storage, MemoryJob)
        changed = await db.fetchval(
            f"INSERT INTO {self._table} AS le "
            "(kind, entity_id, memory_occurrence, next_attempt_at, attempt_count, last_error, work_priority) "
            "VALUES ('memory', $1, $2, $3, $4, $5, $6) ON CONFLICT (kind, entity_id) DO UPDATE SET "
            "memory_occurrence = EXCLUDED.memory_occurrence, next_attempt_at = EXCLUDED.next_attempt_at, "
            "attempt_count = EXCLUDED.attempt_count, last_error = EXCLUDED.last_error, work_priority = EXCLUDED.work_priority "
            "WHERE le.memory_occurrence IS DISTINCT FROM EXCLUDED.memory_occurrence RETURNING 1",
            job.id, job.occurrence_id, job.requested_due_at, job.retry_count, job.retry_error,
            validate_work_priority(job.priority),
        )
        if changed:
            await db.execute("SELECT pg_notify('claim_ready', $1)", f"memory:{job.id}")

    async def discard_memory_lease(self, entity_id: str, occurrence: str | None) -> None:
        async with self._storage.pool.acquire() as conn:
            await conn.execute(
                f"DELETE FROM {self._table} WHERE kind = 'memory' AND entity_id = $1 "
                "AND memory_occurrence IS NOT DISTINCT FROM $2", entity_id, occurrence,
            )

    async def delete_lease(self, kind: ClaimKind, entity_id: str) -> None:
        """Remove the lease row for (kind, entity_id).  No-op if missing."""
        async with self._storage.pool.acquire() as conn:
            await conn.execute(
                f"DELETE FROM {self._table} WHERE kind = $1 AND entity_id = $2",
                kind.value, entity_id,
            )

    # ------------------------------------------------------------------
    # has_live_lease
    # ------------------------------------------------------------------

    async def has_lease(self, kind: ClaimKind, entity_id: str, *, work_priority: int | None = None) -> bool:
        """Whether a lease ROW exists for ``(kind, entity_id)``, claimed or not.

        See :meth:`~primer.int.claim.ClaimEngine.has_lease`: an armed lease nobody has
        claimed yet counts, which is the whole difference from :meth:`has_live_lease`.
        """
        sql = f"SELECT 1 FROM {self._table} WHERE kind = $1 AND entity_id = $2"
        args = [kind.value, entity_id]
        if work_priority is not None:
            sql += " AND work_priority = $3"
            args.append(work_priority)
        async with self._storage.pool.acquire() as conn:
            row = await conn.fetchrow(
                sql, *args,
            )
        return row is not None

    async def lease_exists(self, kind: ClaimKind, entity_ids: Sequence[str]) -> set[str]:
        """The subset of ``entity_ids`` with a lease ROW of ``kind``, claimed or not, in one query.

        See :meth:`~primer.int.claim.ClaimEngine.lease_exists`.
        """
        ids = list(dict.fromkeys(entity_ids))
        if not ids:
            return set()
        async with self._storage.pool.acquire() as conn:
            rows = await conn.fetch(
                f"SELECT entity_id FROM {self._table} "
                f"WHERE kind = $1 AND entity_id = ANY($2::text[])",
                kind.value, ids,
            )
        return {r["entity_id"] for r in rows}

    async def prune_dead_leases(self, kind: ClaimKind) -> int:
        """Delete the unheld lease rows of ``kind`` whose entity is missing or finished.

        One statement. "Finished" is the adapter's :meth:`~primer.int.claim.ClaimAdapter.dead_lease_sql`;
        "missing" is a LEFT JOIN miss against the entity table. A kind whose adapter defines no predicate
        is not pruned at all, so the missing-entity rule cannot delete a lease whose entity this engine
        has no business judging. A held lease (claimed and unexpired) is never touched.

        The candidates come from ONE join of the leases to the entity table (a hash join on a large
        table; the earlier form hashed the entity table twice, once per OR'd sub-select, 420 ms against
        105 ms on 60k tasks and 85k leases), and the outer DELETE re-states the held-lease predicate so
        that, under READ COMMITTED, a heartbeat that extends a lapsed lease while this statement waits on
        the row lock is re-evaluated and the lease survives.

        The entity table is read under the statement's own snapshot: an entity reset from finished to
        queued by a concurrent writer inside that window loses its lease row. Nothing restores it today
        (a planned reconciler rule that re-arms a QUEUED task with no lease row is not built), but no
        production writer resets a finished task to queued either: the adapter's retry branch only
        requeues a live one, and ``durably_mark_tool_call_task_resumable`` now writes only an exact,
        unresolved GATED occurrence (and still has no production caller).
        """
        adapter = self._adapters.get(kind)
        dead = adapter.dead_lease_sql() if adapter is not None else None
        if adapter is None or dead is None:
            return 0
        entity = self._qualified_entity(adapter.entity_table)
        async with self._storage.pool.acquire() as conn:
            if not self._entity_tables_ensured:
                await self._ensure_entity_tables(conn)
            rows = await conn.fetch(
                f"WITH doomed AS ("
                f"  SELECT l.entity_id, l.memory_occurrence FROM {self._table} l"
                f"    LEFT JOIN {entity} e ON e.id = l.entity_id"
                f"   WHERE l.kind = $1"
                f"     AND (l.claimed_by IS NULL OR l.expires_at < now())"
                f"     AND (e.id IS NULL OR ({dead})))"
                f" DELETE FROM {self._table} l USING doomed d"
                f" WHERE l.kind = $1 AND l.entity_id = d.entity_id"
                f"   AND l.memory_occurrence IS NOT DISTINCT FROM d.memory_occurrence"
                f"   AND (l.claimed_by IS NULL OR l.expires_at < now())"
                f" RETURNING 1",
                kind.value,
            )
        return len(rows)

    async def has_live_lease(self, kind: ClaimKind, entity_id: str) -> bool:
        """Whether a worker holds an unexpired lease on (kind, entity_id) right now.

        ``expires_at`` is pushed forward by every heartbeat, so this stays true for as
        long as the worker is alive and false within one lease TTL of it dying — which
        is exactly the line the stuck-session sweeper needs to draw.
        """
        async with self._storage.pool.acquire() as conn:
            row = await conn.fetchrow(
                f"SELECT 1 FROM {self._table} "
                f"WHERE kind = $1 AND entity_id = $2 "
                f"AND claimed_by IS NOT NULL AND expires_at > now()",
                kind.value, entity_id,
            )
        return row is not None

    # ------------------------------------------------------------------
    # claim_due
    # ------------------------------------------------------------------

    async def claim_due(
        self, worker_id: str, *, max_count: int, kinds: list[ClaimKind] | None = None,
        max_work_priority: int | None = None,
    ) -> list[Lease]:
        """Atomically claim up to *max_count* due leases.

        Uses the pre-compiled UNION ALL CTE query that joins each
        adapter's entity table for eligibility filtering. ``kinds``
        (default None = every registered kind) selects a differently
        -scoped compiled query via :meth:`_claim_query_for`.
        """
        if max_work_priority is not None:
            validate_work_priority(max_work_priority)
        _tracer = _tracing.get_tracer("primer.claim")
        with _tracer.start_as_current_span("claim.due") as _span:
            async with self._storage.pool.acquire() as conn:
                if not self._entity_tables_ensured:
                    await self._ensure_entity_tables(conn)
                rows = await conn.fetch(
                    self._claim_query_for(kinds),
                    max_count,
                    worker_id,
                    # TTL in seconds (string cast to interval in the query,
                    # $3). Configured via WorkerConfig.lease_ttl_seconds.
                    str(self.lease_ttl_seconds),
                    max_work_priority,
                )
                # The statement is done (it stamped the lease expiry from its own start): tell the caller, so its
                # staleness clock leaves out the wait for a pool connection above and the hooks below.
                claim_statement_at.set(asyncio.get_running_loop().time())
                leases = [
                    Lease(
                        kind=ClaimKind(r["kind"]),
                        entity_id=r["entity_id"],
                        claimed_by=worker_id,
                        claimed_at=r["claimed_at"],
                        expires_at=r["expires_at"],
                        attempt_count=r["attempt_count"],
                        last_error=r["last_error"],
                        memory_occurrence=r["memory_occurrence"], memory_claim_id=r["memory_claim_id"],
                        work_priority=r.get("work_priority", 0),
                    )
                    for r in rows
                ]
                # The claim-time hooks run on the claim's own connection,
                # before the leases are handed out: an adapter that owns a
                # claim-scoped entity field (the session adapter's
                # last_worker_id stamp) writes it while the claim is fresh,
                # and a failing hook fails the claim, never a half-stamped
                # hand-out.
                for lease in leases:
                    adapter = self._adapters.get(lease.kind)
                    if adapter is not None:
                        await adapter.on_claim(conn, lease)
            _span.set_attribute("claim.count", len(leases))
            for lease in leases:
                # claimed_at is the moment of claiming; we don't have
                # next_attempt_at in the returned Lease, so we observe 0
                # as the latency placeholder for Postgres-claimed leases.
                _metrics.claim_enqueue_latency_seconds.labels(
                    lease.kind.value
                ).observe(0.0)
                _metrics.claim_work_priority_total.labels(
                    lease.kind.value, "foreground" if lease.work_priority == 0 else "background",
                ).inc()
                _span.add_event("claim_assigned", {"kind": lease.kind.value})
            return leases

    # ------------------------------------------------------------------
    # heartbeat
    # ------------------------------------------------------------------

    async def heartbeat(
        self,
        worker_id: str,
        kind_ids: list[tuple[ClaimKind, str]],
        *, memory_claims: dict[str, str] | None = None,
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
                f"UPDATE {self._table} AS l"
                f"   SET last_heartbeat_at = now(),"
                # TTL parameterised ($5) so the refreshed expiry uses the
                # configured lease_ttl_seconds, not a hardcoded 60s.
                f"       expires_at = now() + ($5 || ' seconds')::interval"
                f"  FROM UNNEST($1::text[], $2::text[], $4::text[]) AS h(kind, entity_id, receipt)"
                f" WHERE l.kind=h.kind AND l.entity_id=h.entity_id AND l.claimed_by=$3"
                f"   AND (l.kind <> 'memory' OR l.memory_claim_id=h.receipt)"
                f" RETURNING l.kind, l.entity_id",
                kinds, ids, worker_id, [(memory_claims or {}).get(eid) for eid in ids], str(self.lease_ttl_seconds),
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
        wake_signal = None
        async with self._storage.pool.acquire() as conn:
            async with conn.transaction():
                if lease.kind == ClaimKind.MEMORY:
                    row = await conn.fetchrow(
                        f"SELECT memory_occurrence, memory_claim_id FROM {self._table} "
                        "WHERE kind = 'memory' AND entity_id = $1 AND claimed_by = $2 FOR UPDATE",
                        lease.entity_id, lease.claimed_by,
                    )
                    if row is None or not lease.memory_claim_id or row["memory_claim_id"] != lease.memory_claim_id:
                        return
                    if row["memory_occurrence"] != lease.memory_occurrence:
                        # The old holder hands back its slot. The new request
                        # already owns the deadline, attempts and disposition.
                        outcome = ReleaseOutcome(success=True, entity_noop=True)
                if outcome.drop_lease:
                    owned = await conn.fetchval(
                        f"DELETE FROM {self._table}"
                        f" WHERE kind = $1 AND entity_id = $2 AND claimed_by = $3"
                        f" RETURNING 1",
                        lease.kind.value, lease.entity_id, lease.claimed_by,
                    )
                else:
                    owned = await conn.fetchval(
                        f"UPDATE {self._table}"
                        f"   SET claimed_by        = NULL,"
                        f"       claimed_at         = NULL,"
                        f"       last_heartbeat_at  = NULL,"
                        f"       expires_at         = NULL,"
                        f"       next_attempt_at    = CASE WHEN $8 THEN next_attempt_at"
                        f"                                 ELSE now() + ($3 || ' seconds')::interval END,"
                        f"       attempt_count      = CASE WHEN $7 THEN attempt_count"
                        f"                                 WHEN $4 THEN 0"
                        f"                                 ELSE attempt_count + 1 END,"
                        f"       last_error         = CASE WHEN $7 THEN last_error"
                        f"                                 WHEN $4 THEN NULL"
                        f"                                 ELSE $5 END"
                        f" WHERE kind = $1 AND entity_id = $2 AND claimed_by = $6"
                        f" RETURNING 1",
                        lease.kind.value, lease.entity_id,
                        str(requeue_secs), outcome.success, outcome.last_error,
                        lease.claimed_by,
                        # entity_noop is a lease-only hand-back: it must not look like a run, so it
                        # leaves attempt_count/last_error alone and, without an explicit requeue
                        # delay, next_attempt_at too (the row keeps its place in line).
                        outcome.entity_noop,
                        outcome.entity_noop and outcome.requeue_after is None,
                    )
                # Fence: if no row matched our claimed_by, the lease was
                # re-claimed by another worker (or deleted). Skip on_release
                # so a stale worker cannot clobber the new owner's state.
                if owned is None:
                    logger.warning(
                        "claim release skipped: lease %s/%s no longer owned by %r "
                        "(presumed re-claimed)",
                        lease.kind.value, lease.entity_id, lease.claimed_by,
                    )
                    return
                adapter = self._adapters.get(lease.kind)
                if adapter is not None and not outcome.entity_noop:
                    wake_signal = await adapter.on_release(
                        conn, lease.entity_id, outcome=outcome,
                    )
        # 01a0518b review: fired ONLY after the `async with conn.transaction()`
        # block above has exited (committed) - see PostReleaseWake's own
        # docstring for why an adapter can't call the eventual wake code
        # from inside that transaction.
        if wake_signal is not None and self._post_release_hook is not None:
            await self._post_release_hook(wake_signal)

    # ------------------------------------------------------------------
    # mark_resumable
    # ------------------------------------------------------------------

    async def mark_resumable(
        self, kind: ClaimKind, entity_id: str, *, priority: int = CLAIM_PRIORITY_RESUME,
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
        await self._arm_priority(kind, entity_id, priority=priority, resume=True)

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

        If the server connection is lost (a Postgres restart or failover, a
        network blip) the generator raises :class:`ListenConnectionLost` once
        every wake received before the loss has been yielded. asyncpg never raises
        into the NOTIFY queue itself, so without that the watcher would park on
        a dead connection forever. The caller re-subscribes by calling
        ``watch_ready()`` again (``WorkerPool._engine_bus_loop`` does); wakes
        sent in the meantime are lost, which only costs the claim loop's poll
        interval because ``claim_ready`` is a hint, not the record of a claim.
        """
        queue: asyncio.Queue[str | None] = asyncio.Queue()

        def _on_notify(
            conn: Any,
            pid: int,
            channel: str,
            payload: str,
        ) -> None:
            queue.put_nowait(payload)

        def _on_termination(_conn: Any) -> None:
            # asyncpg's only signal that the server connection is gone.
            queue.put_nowait(None)

        conn = await self._storage.pool.acquire()
        try:
            conn.add_termination_listener(_on_termination)
            try:
                await conn.add_listener("claim_ready", _on_notify)
                while True:
                    payload = await queue.get()
                    if payload is None:
                        raise ListenConnectionLost(
                            "claim_ready LISTEN connection was terminated"
                        )
                    kind_str, entity_id = payload.split(":", 1)
                    yield (ClaimKind(kind_str), entity_id)
            finally:
                # After a loss, asyncpg has already released the connection
                # and every method on it raises InterfaceError. That must not
                # replace the exception in flight (a CancelledError at
                # shutdown), and one failing step must not skip the next, so
                # each is guarded on its own.
                try:
                    conn.remove_termination_listener(_on_termination)
                except Exception as exc:  # noqa: BLE001
                    logger.debug(
                        "claim_ready remove_termination_listener failed: %s", exc,
                    )
                try:
                    await conn.remove_listener("claim_ready", _on_notify)
                except Exception as exc:  # noqa: BLE001
                    logger.debug("claim_ready remove_listener failed: %s", exc)
        finally:
            await self._storage.pool.release(conn)
