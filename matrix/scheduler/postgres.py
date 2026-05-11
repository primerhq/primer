"""Postgres-backed :class:`Scheduler`.

Lease columns + ``SELECT ... FOR UPDATE SKIP LOCKED`` for claim;
``LISTEN/NOTIFY session_ready`` and ``LISTEN/NOTIFY session_cancel``
for low-latency signalling. Reuses the
:class:`PostgresStorageProvider`'s connection pool for everything
except the dedicated LISTEN connections.

The class is stubbed in this task -- tasks 9, 10, 11 fill in the
worker-membership, claim/complete_turn, and LISTEN/NOTIFY surfaces
respectively.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Sequence
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any

from matrix.int.scheduler import (
    CompleteTurnResult,
    FailureRecord,
    Lease,
    Scheduler,
    WorkerInfo,
)
from matrix.model.except_ import ProviderError
from matrix.model.scheduler import PostgresSchedulerConfig
from matrix.model.session import SessionStatus

if TYPE_CHECKING:
    import asyncpg

    from matrix.int.storage_provider import StorageProvider

logger = logging.getLogger(__name__)


_DDL_LEASES = """
CREATE TABLE IF NOT EXISTS session_leases (
    session_id        TEXT PRIMARY KEY,
    worker_id         TEXT,
    leased_at         TIMESTAMPTZ,
    expires_at        TIMESTAMPTZ,
    next_attempt_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    runnable          BOOLEAN NOT NULL DEFAULT FALSE
)
"""

_DDL_LEASES_INDEX = """
CREATE INDEX IF NOT EXISTS idx_session_leases_claimable
    ON session_leases (next_attempt_at)
    WHERE runnable = TRUE
"""

_DDL_WORKERS = """
CREATE TABLE IF NOT EXISTS workers (
    id              TEXT PRIMARY KEY,
    host            TEXT NOT NULL,
    pid             INT NOT NULL,
    capacity        INT NOT NULL,
    started_at      TIMESTAMPTZ NOT NULL,
    last_heartbeat  TIMESTAMPTZ NOT NULL,
    status          TEXT NOT NULL CHECK (status IN ('active','draining','dead'))
)
"""


class _LeaseLostMarker(Exception):
    """Internal: forces transaction rollback when lease was lost mid-CAS."""


class PostgresScheduler(Scheduler):
    """Postgres impl. Tasks 9-11 fill in claim/complete_turn/LISTEN."""

    def __init__(
        self,
        *,
        storage_provider: "StorageProvider",
        config: PostgresSchedulerConfig,
    ) -> None:
        self._storage = storage_provider
        self._config = config
        self._lease_ttl_seconds: int = 30
        self._listen_tasks: list[asyncio.Task] = []
        # ---- metrics (spec §14) ----
        self._notify_received_total: int = 0
        self._listen_reconnects_total: int = 0

    @property
    def lease_ttl_seconds(self) -> int:
        return self._lease_ttl_seconds

    @lease_ttl_seconds.setter
    def lease_ttl_seconds(self, value: int) -> None:
        self._lease_ttl_seconds = value

    async def initialize(self) -> None:
        try:
            async with self._storage.pool.acquire() as conn:
                async with conn.transaction():
                    await conn.execute(_DDL_LEASES)
                    await conn.execute(_DDL_LEASES_INDEX)
                    await conn.execute(_DDL_WORKERS)
                    # Boot-time recovery sweeps (spec section 10):
                    # 1) Mark dead any worker rows that haven't heartbeat
                    #    in 5 minutes -- leases they hold expire naturally,
                    #    but operators want the row marked dead.
                    await conn.execute(
                        "UPDATE workers SET status = 'dead' "
                        "WHERE status != 'dead' "
                        "AND last_heartbeat < now() - interval '5 minutes'"
                    )
                    # 2) Re-enqueue any session whose status is RUNNING /
                    #    CREATED but whose lease row is missing or not
                    #    runnable. Skipped if `sessions` table doesn't
                    #    exist yet -- Storage[Session] creates it lazily.
                    table_exists = await conn.fetchval(
                        "SELECT to_regclass('sessions') IS NOT NULL"
                    )
                    if table_exists:
                        await conn.execute(
                            "INSERT INTO session_leases (session_id, runnable) "
                            "SELECT id, TRUE FROM sessions "
                            "WHERE data->>'status' IN ('created','running') "
                            "ON CONFLICT (session_id) DO UPDATE "
                            "SET runnable = TRUE"
                        )
                        # If the sessions table now exists, ensure the FK from
                        # session_leases.session_id -> sessions.id is in place
                        # with ON DELETE CASCADE. Idempotent: skip if the
                        # constraint already exists. Cleanup orphan lease rows
                        # first so the constraint add can't be rejected by FK
                        # violations.
                        constraint_exists = await conn.fetchval(
                            """
                            SELECT EXISTS (
                                SELECT 1 FROM pg_constraint
                                WHERE conname = 'session_leases_session_id_fkey'
                            )
                            """
                        )
                        if not constraint_exists:
                            await conn.execute(
                                "DELETE FROM session_leases l "
                                "WHERE NOT EXISTS ("
                                "  SELECT 1 FROM sessions s WHERE s.id = l.session_id"
                                ")"
                            )
                            await conn.execute(
                                "ALTER TABLE session_leases "
                                "ADD CONSTRAINT session_leases_session_id_fkey "
                                "FOREIGN KEY (session_id) REFERENCES sessions(id) "
                                "ON DELETE CASCADE"
                            )
        except Exception as exc:
            raise ProviderError(
                f"failed to create scheduler tables: {exc}", cause=exc,
            ) from exc

    async def aclose(self) -> None:
        for task in self._listen_tasks:
            task.cancel()
        for task in self._listen_tasks:
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
        self._listen_tasks.clear()

    # ---- methods filled in by Task 9 -----------------------------------

    async def register_worker(
        self, *, worker_id: str, host: str, pid: int, capacity: int,
    ) -> None:
        async with self._storage.pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO workers (id, host, pid, capacity, started_at,
                                     last_heartbeat, status)
                VALUES ($1, $2, $3, $4, now(), now(), 'active')
                ON CONFLICT (id) DO UPDATE SET
                    host = EXCLUDED.host,
                    pid = EXCLUDED.pid,
                    capacity = EXCLUDED.capacity,
                    last_heartbeat = now(),
                    status = 'active'
                """,
                worker_id, host, pid, capacity,
            )

    async def heartbeat_worker(self, worker_id: str) -> None:
        async with self._storage.pool.acquire() as conn:
            await conn.execute(
                "UPDATE workers SET last_heartbeat = now() WHERE id = $1",
                worker_id,
            )

    async def drain_worker(self, worker_id: str) -> None:
        async with self._storage.pool.acquire() as conn:
            await conn.execute(
                "UPDATE workers SET status = 'draining' WHERE id = $1",
                worker_id,
            )

    async def deregister_worker(self, worker_id: str) -> None:
        async with self._storage.pool.acquire() as conn:
            await conn.execute(
                "DELETE FROM workers WHERE id = $1", worker_id,
            )

    async def list_workers(self) -> list[WorkerInfo]:
        async with self._storage.pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT id, host, pid, capacity, started_at, last_heartbeat, status "
                "FROM workers ORDER BY id"
            )
        return [
            WorkerInfo(
                id=r["id"], host=r["host"], pid=r["pid"],
                capacity=r["capacity"], started_at=r["started_at"],
                last_heartbeat=r["last_heartbeat"], status=r["status"],
            )
            for r in rows
        ]

    # ---- methods filled in by Task 10 ----------------------------------

    async def enqueue(
        self, session_id: str, *, ready_at: datetime | None = None,
    ) -> None:
        async with self._storage.pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute(
                    """
                    INSERT INTO session_leases
                        (session_id, runnable, next_attempt_at)
                    VALUES ($1, TRUE, COALESCE($2, now()))
                    ON CONFLICT (session_id) DO UPDATE SET
                        runnable = TRUE,
                        next_attempt_at = COALESCE($2, EXCLUDED.next_attempt_at)
                    """,
                    session_id, ready_at,
                )
                await conn.execute(
                    "SELECT pg_notify('session_ready', $1)", session_id,
                )

    async def claim(
        self, worker_id: str, *, max_count: int = 1,
    ) -> list[Lease]:
        sql = """
            WITH claimed AS (
              SELECT l.session_id,
                     (data->>'turn_no')::int AS turn_no,
                     COALESCE((data->>'attempt_count')::int, 0) AS attempt_count
              FROM session_leases l
              JOIN sessions s ON s.id = l.session_id
              WHERE l.runnable = TRUE
                AND (l.worker_id IS NULL OR l.expires_at < now())
                AND l.next_attempt_at <= now()
              ORDER BY
                CASE WHEN s.data->>'last_worker_id' = $1 THEN 0 ELSE 1 END,
                l.next_attempt_at
              FOR UPDATE OF l SKIP LOCKED
              LIMIT $2
            )
            UPDATE session_leases l
            SET worker_id = $1,
                leased_at = now(),
                expires_at = now() + ($3 || ' seconds')::interval
            FROM claimed
            WHERE l.session_id = claimed.session_id
            RETURNING l.session_id, l.expires_at,
                      claimed.turn_no, claimed.attempt_count
        """
        async with self._storage.pool.acquire() as conn:
            rows = await conn.fetch(
                sql, worker_id, max_count, str(self._lease_ttl_seconds),
            )
        return [
            Lease(
                session_id=r["session_id"],
                worker_id=worker_id,
                expires_at=r["expires_at"],
                attempt_count=r["attempt_count"],
                turn_no=r["turn_no"],
            )
            for r in rows
        ]

    async def heartbeat_leases(
        self, worker_id: str, session_ids: Sequence[str],
    ) -> list[str]:
        if not session_ids:
            return []
        sql = """
            UPDATE session_leases
            SET expires_at = now() + ($1 || ' seconds')::interval
            WHERE worker_id = $2 AND session_id = ANY($3::text[])
            RETURNING session_id
        """
        async with self._storage.pool.acquire() as conn:
            rows = await conn.fetch(
                sql, str(self._lease_ttl_seconds), worker_id, list(session_ids),
            )
        return [r["session_id"] for r in rows]

    async def complete_turn(
        self,
        worker_id: str,
        session_id: str,
        *,
        expected_turn_no: int,
        new_status: SessionStatus,
        ended_reason: str | None = None,
        re_enqueue: bool,
        backoff: timedelta | None = None,
        record_failure: FailureRecord | None = None,
    ) -> CompleteTurnResult:
        try:
            return await self._complete_turn_inner(
                worker_id, session_id,
                expected_turn_no=expected_turn_no,
                new_status=new_status,
                ended_reason=ended_reason,
                re_enqueue=re_enqueue,
                backoff=backoff,
                record_failure=record_failure,
            )
        except _LeaseLostMarker:
            return CompleteTurnResult.LEASE_LOST

    async def _complete_turn_inner(
        self, worker_id, session_id, *,
        expected_turn_no, new_status, ended_reason,
        re_enqueue, backoff, record_failure,
    ):
        backoff_seconds = (
            int(backoff.total_seconds()) if backoff is not None else 0
        )
        failure_attempt = (
            record_failure.attempt_count if record_failure is not None else 0
        )
        failure_text = (
            record_failure.error_text if record_failure is not None else None
        )
        set_ended_at = (new_status == SessionStatus.ENDED)

        update_session_sql = """
            UPDATE sessions
            SET data = (
                jsonb_set(
                  jsonb_set(
                    jsonb_set(
                      jsonb_set(
                        jsonb_set(
                          jsonb_set(
                            jsonb_set(
                              jsonb_set(data,
                                '{status}', to_jsonb($3::text)),
                              '{turn_no}', to_jsonb(($4::int))),
                            '{last_worker_id}', to_jsonb($1::text)),
                          '{last_turn_at}', to_jsonb(now())),
                        '{ended_reason}',
                        CASE WHEN $5::text IS NULL THEN 'null'::jsonb
                             ELSE to_jsonb($5::text) END),
                      '{ended_at}',
                      CASE WHEN $9::bool THEN to_jsonb(now())
                           ELSE COALESCE(data->'ended_at', 'null'::jsonb) END),
                    '{attempt_count}', to_jsonb($6::int)),
                  '{last_error}',
                  CASE WHEN $7::text IS NULL THEN 'null'::jsonb
                       ELSE to_jsonb($7::text) END
                )
            ),
            updated_at = now()
            WHERE id = $2 AND (data->>'turn_no')::int = $8
            RETURNING id
        """

        update_lease_sql = """
            UPDATE session_leases
            SET worker_id = NULL,
                expires_at = NULL,
                runnable = $1,
                next_attempt_at = now() + ($2 || ' seconds')::interval
            WHERE session_id = $3 AND worker_id = $4
            RETURNING session_id
        """

        async with self._storage.pool.acquire() as conn:
            async with conn.transaction():
                updated = await conn.fetchrow(
                    update_session_sql,
                    worker_id, session_id,
                    new_status.value,
                    expected_turn_no + 1,
                    ended_reason,
                    failure_attempt,
                    failure_text,
                    expected_turn_no,
                    set_ended_at,
                )
                if updated is None:
                    return CompleteTurnResult.TURN_CONFLICT

                lease_row = await conn.fetchrow(
                    update_lease_sql,
                    re_enqueue, str(backoff_seconds),
                    session_id, worker_id,
                )
                if lease_row is None:
                    raise _LeaseLostMarker()

                if re_enqueue:
                    await conn.execute(
                        "SELECT pg_notify('session_ready', $1)", session_id,
                    )
                return CompleteTurnResult.SUCCESS

    # ---- methods filled in by Task 11 ----------------------------------

    async def _open_listen_connection(
        self, channel: str,
    ) -> tuple["asyncpg.Connection", asyncio.Queue]:
        """Acquire a dedicated connection from the pool and add a LISTEN
        callback that pushes payloads onto an asyncio.Queue.

        The connection is held by the caller — they must release it via
        ``self._storage.pool.release(conn)`` when the iterator is closed
        or the connection drops.
        """
        queue: asyncio.Queue[str] = asyncio.Queue()
        conn = await self._storage.pool.acquire()

        def _on_notify(_conn, _pid, _ch, payload):
            queue.put_nowait(payload)

        await conn.add_listener(channel, _on_notify)
        return conn, queue

    def watch_ready(self, worker_id: str) -> AsyncIterator[str]:
        """Stream session_ids from ``pg_notify('session_ready', ...)``.

        Best-effort wake-up hint. The worker's claim loop is the safety net;
        NOTIFY drops during connection reconnects do NOT lose work.
        """
        return self._watch_channel("session_ready")

    def _watch_cancel(self, worker_id: str) -> AsyncIterator[str]:
        """Test-only parallel of watch_ready, scoped to the cancel channel.

        Production wires this through the WorkerPool's cancel loop, which
        fans cancel notifications out to the local ``_active_scopes``
        registry (see spec §7).
        """
        return self._watch_channel("session_cancel")

    def _watch_channel(self, channel: str) -> AsyncIterator[str]:
        """Generic LISTEN-backed iterator with reconnect on drop."""
        config = self._config
        storage = self._storage
        scheduler = self

        async def _iter() -> AsyncIterator[str]:
            first_attempt = True
            while True:
                try:
                    conn, queue = await self._open_listen_connection(channel)
                except Exception as exc:
                    logger.warning(
                        "scheduler LISTEN reconnect on %s: %s", channel, exc,
                    )
                    if not first_attempt:
                        scheduler._listen_reconnects_total += 1
                    first_attempt = False
                    await asyncio.sleep(config.listen_reconnect_seconds)
                    continue
                if not first_attempt:
                    scheduler._listen_reconnects_total += 1
                first_attempt = False
                try:
                    while True:
                        payload = await queue.get()
                        scheduler._notify_received_total += 1
                        yield payload
                except asyncio.CancelledError:
                    try:
                        await storage.pool.release(conn)
                    except Exception:
                        pass
                    raise
                except Exception as exc:
                    logger.warning(
                        "scheduler LISTEN dropped on %s: %s — reconnecting",
                        channel, exc,
                    )
                    try:
                        await storage.pool.release(conn)
                    except Exception:
                        pass
                    await asyncio.sleep(config.listen_reconnect_seconds)

        return _iter()

    async def signal_cancel(self, session_id: str) -> None:
        """Emit pg_notify('session_cancel', $sid). Best-effort hint to
        whichever worker is currently holding the lease."""
        async with self._storage.pool.acquire() as conn:
            await conn.execute(
                "SELECT pg_notify('session_cancel', $1)", session_id,
            )

    # ---- Metrics --------------------------------------------------------

    def metrics_snapshot(self) -> dict[str, Any]:
        """Process-local scheduler counters. See spec §14.

        Sync-only (the ABC contract). DB-derived gauges -- session
        counts by status, runnable queue depth, lease expirations -- are
        served by :meth:`metrics_db_snapshot`, which is async because
        those values require a live SQL round-trip."""
        return {
            "matrix_scheduler_notify_received_total": (
                self._notify_received_total
            ),
            "matrix_scheduler_listen_reconnects_total": (
                self._listen_reconnects_total
            ),
        }

    async def metrics_db_snapshot(self) -> dict[str, Any]:
        """Async companion to :meth:`metrics_snapshot` for DB-side
        aggregates. See spec §14. Two queries: one for sessions by
        status, one for runnable queue depth."""
        async with self._storage.pool.acquire() as conn:
            sessions_table_exists = await conn.fetchval(
                "SELECT to_regclass('sessions') IS NOT NULL"
            )
            sessions_by_status: dict[str, int] = {}
            if sessions_table_exists:
                rows = await conn.fetch(
                    "SELECT data->>'status' AS status, count(*) AS n "
                    "FROM sessions GROUP BY data->>'status'"
                )
                for r in rows:
                    sessions_by_status[r["status"] or "unknown"] = r["n"]
            runnable_depth = await conn.fetchval(
                "SELECT count(*) FROM session_leases "
                "WHERE runnable = TRUE "
                "AND (worker_id IS NULL OR expires_at < now()) "
                "AND next_attempt_at <= now()"
            ) or 0
        return {
            "matrix_sessions_active": sessions_by_status,
            "matrix_sessions_runnable_queue_depth": runnable_depth,
        }
