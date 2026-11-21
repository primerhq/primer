"""Bounded, explicitly invoked priority preparation for a quiescent database.

Never called by startup or the production factory. A table/advisory lock is
not an old-binary fence: operators must quiesce all writers before preparing
nonzero snapshots. A returned cursor is progress, not activation authority.
"""

from primer.int.claim import ClaimKind


async def backfill_priority_batch(storage, adapters, *, after: tuple[str, str] | None = None, limit: int = 100):
    """Commit one keyset page; return its last key or None at end.

    Failure rolls back the whole page, so retry its input cursor. Replaying a
    page is idempotent and never changes ownership, retry state or due time.
    """
    if type(limit) is not int or not 1 <= limit <= 1000:
        raise ValueError("priority backfill page must contain 1..1000 rows")
    table = storage.leases_table
    async with storage.pool.acquire() as conn, conn.transaction():
        await conn.execute("SET LOCAL lock_timeout = '2s'")
        await conn.execute("SET LOCAL statement_timeout = '10s'")
        await conn.execute("SELECT pg_advisory_xact_lock(hashtextextended($1, 0))", table + ":work-priority-migration-v1")
        await conn.execute(f"LOCK TABLE {table} IN SHARE ROW EXCLUSIVE MODE")
        if await conn.fetchval(f"SELECT 1 FROM {table} WHERE claimed_by IS NOT NULL LIMIT 1"):
            raise ValueError("priority preparation requires drained leases")
        rows = await conn.fetch(
            f"SELECT kind, entity_id FROM {table} "
            "WHERE ($1::text IS NULL OR (kind, entity_id) > ($1, $2)) "
            "ORDER BY kind, entity_id LIMIT $3",
            *(after or (None, None)), limit,
        )
        for row in rows:
            adapter = adapters[ClaimKind(row["kind"])]
            entity = f'"{storage.schema}"."{adapter.entity_table}"'
            expression = adapter.work_priority_sql(schema=storage.schema)
            # Scalar subquery yields NULL for a missing source. The NOT NULL
            # constraint refuses that page rather than promoting unknown work.
            await conn.execute(
                f"UPDATE {table} SET work_priority=(SELECT {expression} FROM {entity} e WHERE e.id=$2) "
                "WHERE kind=$1 AND entity_id=$2",
                row["kind"], row["entity_id"],
            )
        return (rows[-1]["kind"], rows[-1]["entity_id"]) if rows else None


async def prepare_priority_indexes(storage) -> None:
    """Versioned offline indexes; timeout/rollback leaves no partial index.

    Retain the old indexes for production ordering and rollback. Large stores
    that cannot fit this bounded operation need a separately reviewed online
    migration, not a startup-time unbounded retry.
    """
    schema = storage.schema
    async with storage.pool.acquire() as conn, conn.transaction():
        await conn.execute("SET LOCAL lock_timeout = '2s'")
        await conn.execute("SET LOCAL statement_timeout = '30s'")
        await conn.execute("SELECT pg_advisory_xact_lock(hashtextextended($1, 0))",
                           storage.leases_table + ":work-priority-migration-v1")
        await conn.execute(f"LOCK TABLE {storage.leases_table} IN SHARE ROW EXCLUSIVE MODE")
        if await conn.fetchval(f"SELECT 1 FROM {storage.leases_table} WHERE claimed_by IS NOT NULL LIMIT 1"):
            raise ValueError("priority preparation requires drained leases")
        for suffix, extra, predicate in (
            ("unclaimed", "", "claimed_by IS NULL"),
            ("expired", ", expires_at", "claimed_by IS NOT NULL"),
        ):
            name = f"leases_work_priority_{suffix}_v1"
            valid = await conn.fetchval(
                "SELECT i.indisvalid FROM pg_index i JOIN pg_class c ON c.oid=i.indexrelid "
                "JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname=$1 AND c.relname=$2",
                schema, name,
            )
            if valid is False:
                await conn.execute(f'DROP INDEX "{schema}"."{name}"')
            await conn.execute(
                f'CREATE INDEX IF NOT EXISTS "{name}" ON {storage.leases_table} '
                f"(kind, work_priority, priority_score, next_attempt_at{extra}) WHERE {predicate}"
            )
