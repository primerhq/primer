"""Postgres-backed :class:`Scheduler`.

``LISTEN/NOTIFY session_ready`` and ``LISTEN/NOTIFY session_cancel``
for low-latency signalling. Reuses the
:class:`PostgresStorageProvider`'s connection pool for everything
except the dedicated LISTEN connections.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from datetime import datetime
from typing import TYPE_CHECKING, Any

from primer.int.scheduler import (
    Scheduler,
    WorkerInfo,
)
from primer.model.except_ import ProviderError
from primer.model.scheduler import PostgresSchedulerConfig
from primer.storage._ddl import CONCURRENT_CREATE_RACE

if TYPE_CHECKING:
    import asyncpg

    from primer.int.storage_provider import StorageProvider

logger = logging.getLogger(__name__)


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


class PostgresScheduler(Scheduler):
    """Postgres impl. Tasks 9-11 fill in claim/LISTEN."""

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
                    await conn.execute(_DDL_WORKERS)
                    # Boot-time recovery: mark dead any worker rows that
                    # haven't heartbeat in 5 minutes.
                    await conn.execute(
                        "UPDATE workers SET status = 'dead' "
                        "WHERE status != 'dead' "
                        "AND last_heartbeat < now() - interval '5 minutes'"
                    )
        except CONCURRENT_CREATE_RACE as exc:
            # Concurrent-creation race (see primer.storage._ddl): another
            # process is creating the `workers` table at the same time. The
            # winner creates it and runs the same boot-recovery sweep, so we
            # can safely continue rather than crashing startup.
            logger.debug(
                "scheduler initialize race (%s); table created by a peer",
                type(exc).__name__,
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
            await conn.execute(
                "SELECT pg_notify('session_ready', $1)", session_id,
            )

    # ---- LISTEN/NOTIFY (Task 11) ----------------------------------------

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

        async def _safe_release(conn) -> None:
            """Release the LISTEN connection back to the pool; log + swallow
            failures so a release error doesn't mask the original cause."""
            try:
                await storage.pool.release(conn)
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "scheduler LISTEN pool.release on %s failed: %s — "
                    "connection may leak",
                    channel, exc,
                )

        async def _iter() -> AsyncIterator[str]:
            first_attempt = True
            while True:
                try:
                    conn, queue = await self._open_listen_connection(channel)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    logger.warning(
                        "scheduler LISTEN reconnect on %s: %s", channel, exc,
                    )
                    if not first_attempt:
                        scheduler._listen_reconnects_total += 1
                    first_attempt = False
                    try:
                        await asyncio.sleep(config.listen_reconnect_seconds)
                    except asyncio.CancelledError:
                        raise
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
                    await _safe_release(conn)
                    raise
                except Exception as exc:
                    logger.warning(
                        "scheduler LISTEN dropped on %s: %s — reconnecting",
                        channel, exc,
                    )
                    await _safe_release(conn)
                    try:
                        await asyncio.sleep(config.listen_reconnect_seconds)
                    except asyncio.CancelledError:
                        raise

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
            "primer_scheduler_notify_received_total": (
                self._notify_received_total
            ),
            "primer_scheduler_listen_reconnects_total": (
                self._listen_reconnects_total
            ),
        }

    async def metrics_db_snapshot(self) -> dict[str, Any]:
        """Async companion to :meth:`metrics_snapshot` for DB-side
        aggregates. See spec §14. Sessions by status."""
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
        return {
            "primer_sessions_active": sessions_by_status,
        }
