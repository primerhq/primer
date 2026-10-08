"""Postgres-backed :class:`Scheduler`.

``LISTEN/NOTIFY session_ready`` and ``LISTEN/NOTIFY session_cancel``
for low-latency signalling. Reuses the
:class:`PostgresStorageProvider`'s connection pool for everything
except the dedicated LISTEN connections.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Any

from primer.int.scheduler import (
    Scheduler,
    WorkerInfo,
)
from primer.model.except_ import ListenConnectionLost, ProviderError
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


@dataclass(eq=False)
class _Listener:
    """One dedicated pooled connection with a LISTEN callback attached.

    ``queue`` carries NOTIFY payloads, plus a single ``None`` when the server
    connection is lost (pushed by ``on_termination``): asyncpg never raises
    into a NOTIFY queue, so without that marker the watcher would park on
    ``queue.get()`` forever.
    """

    conn: "asyncpg.Connection"
    channel: str
    callback: Callable[..., None]
    queue: "asyncio.Queue[str | None]"
    on_termination: Callable[..., None]


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
        # Every LISTEN connection currently checked out of the pool, so
        # aclose() can release the ones whose watcher was abandoned without
        # being closed (see _close_listener).
        self._listeners: set[_Listener] = set()
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
                    # Added after the table shipped (Lead sweep M2): nullable, no default, so a worker from before load reporting
                    # reads as "never reported" (NULL), not as idle. Idempotent; safe against a rolling upgrade.
                    await conn.execute("ALTER TABLE workers ADD COLUMN IF NOT EXISTS in_flight INT")
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
        """Release every LISTEN connection still checked out of the pool.

        A watcher's own ``finally`` releases its connection whenever the
        generator is closed. A consumer that returns from ``async for``
        leaves the generator suspended rather than closed; if nothing
        references it any more, asyncio's async-generator finalizer closes it
        on the next loop iteration, which is how the production consumer
        (``WorkerPool._cancel_loop``) is cleaned up, well before the pool
        closes. This method is defence in depth for a suspended generator
        that something STILL references: the finalizer never fires for it,
        its connection stays acquired, and ``Pool.close()`` waits forever for
        acquired connections. So the scheduler releases what it handed out;
        a watcher that is closed later finds its listener already gone and
        does nothing.
        """
        for listener in list(self._listeners):
            await self._close_listener(listener)

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
                    status = 'active',
                    in_flight = NULL
                """,
                worker_id, host, pid, capacity,
            )

    async def heartbeat_worker(self, worker_id: str) -> None:
        async with self._storage.pool.acquire() as conn:
            await conn.execute(
                "UPDATE workers SET last_heartbeat = now() WHERE id = $1",
                worker_id,
            )

    async def report_worker_load(self, worker_id: str, *, in_flight: int) -> None:
        async with self._storage.pool.acquire() as conn:
            await conn.execute(
                "UPDATE workers SET in_flight = $2 WHERE id = $1",
                worker_id, in_flight,
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

    async def purge_dead_workers(self) -> int:
        """One DELETE over the whole tombstone set."""
        async with self._storage.pool.acquire() as conn:
            rows = await conn.fetch(
                "DELETE FROM workers WHERE status = 'dead' RETURNING id",
            )
        return len(rows)

    async def list_workers(self) -> list[WorkerInfo]:
        async with self._storage.pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT id, host, pid, capacity, started_at, last_heartbeat, status, in_flight "
                "FROM workers ORDER BY id"
            )
        return [
            WorkerInfo(
                id=r["id"], host=r["host"], pid=r["pid"],
                capacity=r["capacity"], started_at=r["started_at"],
                last_heartbeat=r["last_heartbeat"], status=r["status"],
                in_flight=r["in_flight"],
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

    async def _open_listen_connection(self, channel: str) -> _Listener:
        """Acquire a dedicated connection from the pool and add a LISTEN
        callback that pushes payloads onto an asyncio.Queue.

        The returned listener is registered with the scheduler. The caller
        must hand it back to :meth:`_close_listener` when the iterator is
        closed or the connection drops; :meth:`aclose` does so for any that
        were abandoned.
        """
        queue: asyncio.Queue[str | None] = asyncio.Queue()
        conn = await self._storage.pool.acquire()

        def _on_notify(_conn, _pid, _ch, payload):
            queue.put_nowait(payload)

        def _on_termination(_conn):
            # Called by asyncpg when the server connection is lost (a Postgres
            # restart or failover, a network blip, pg_terminate_backend). It
            # is the ONLY signal: nothing is ever raised into the NOTIFY queue.
            queue.put_nowait(None)

        try:
            # Attached before LISTEN so a drop during the LISTEN round trip is
            # reported too.
            conn.add_termination_listener(_on_termination)
            await conn.add_listener(channel, _on_notify)
        except BaseException:
            self._forget_termination(conn, _on_termination, channel)
            # Not registered yet, so nothing else will ever release it. The
            # release can itself fail (asyncpg re-raises errors from reset or
            # from waiting out a cancelled query); that must be logged and
            # swallowed so the bare `raise` below always re-raises the ORIGINAL
            # exception. A CancelledError replaced by an ordinary Exception
            # would be treated by the watcher as "open failed, retry", using up
            # the task's single cancel() and hanging drain_and_stop.
            try:
                await self._storage.pool.release(conn)
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "scheduler LISTEN pool.release on %s failed after a "
                    "failed LISTEN setup: %s - connection may leak",
                    channel, exc,
                )
            raise
        listener = _Listener(conn, channel, _on_notify, queue, _on_termination)
        self._listeners.add(listener)
        return listener

    @staticmethod
    def _forget_termination(conn, callback, channel: str) -> None:
        """Detach a termination callback, tolerating a connection that is
        already gone (asyncpg raises InterfaceError on a released proxy)."""
        try:
            conn.remove_termination_listener(callback)
        except Exception as exc:  # noqa: BLE001
            logger.debug(
                "scheduler LISTEN remove_termination_listener on %s failed: %s",
                channel, exc,
            )

    async def _close_listener(self, listener: _Listener) -> None:
        """Remove the LISTEN callback and give the connection back to the pool.

        Idempotent: the watcher's ``finally`` and :meth:`aclose` may both
        reach the same listener, and only the first does anything. Failures
        are logged and swallowed so a release error cannot mask the original
        cause of a watcher exiting.
        """
        if listener not in self._listeners:
            return
        self._listeners.discard(listener)
        # asyncpg leaves a termination listener attached when a healthy
        # connection goes back to the pool, so without this a later close of
        # that pooled connection would call it and push a drop marker into a
        # queue nobody reads any more.
        self._forget_termination(listener.conn, listener.on_termination, listener.channel)
        try:
            await listener.conn.remove_listener(listener.channel, listener.callback)
        except Exception as exc:  # noqa: BLE001
            logger.debug(
                "scheduler LISTEN remove_listener on %s failed: %s",
                listener.channel, exc,
            )
        finally:
            try:
                await self._storage.pool.release(listener.conn)
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "scheduler LISTEN pool.release on %s failed: %s - "
                    "connection may leak",
                    listener.channel, exc,
                )

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
        """Generic LISTEN-backed iterator with reconnect on drop.

        A dropped server connection is detected through asyncpg's termination
        listener (see :class:`_Listener`): the watcher logs a WARNING naming the
        channel, releases the dead connection, waits ``listen_reconnect_seconds``,
        and reopens. ``listen_reconnects_total`` counts every open ATTEMPT after
        the first one, failed or successful, so an outage that outlasts several
        intervals reads as several reconnects, not one.

        NOTIFYs sent while it is down are lost (Postgres does not replay them).
        For ``session_ready`` that costs nothing, because claims also poll. For
        ``session_cancel`` it costs the hard preempt of a running turn: the
        cancel is still recorded on the session row (honored at the next turn
        boundary) and is also published on the event bus, which has its own
        LISTEN supervisor.
        """
        config = self._config
        scheduler = self

        async def _iter() -> AsyncIterator[str]:
            first_attempt = True
            while True:
                try:
                    listener = await self._open_listen_connection(channel)
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
                        payload = await listener.queue.get()
                        if payload is None:
                            # Everything received before the drop has been
                            # yielded (the queue is FIFO); now take the
                            # "dropped ... reconnecting" arm below.
                            raise ListenConnectionLost("server connection terminated")
                        scheduler._notify_received_total += 1
                        yield payload
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    logger.warning(
                        "scheduler LISTEN dropped on %s: %s - reconnecting",
                        channel, exc,
                    )
                finally:
                    # CancelledError, a dropped connection, AND GeneratorExit
                    # (a consumer that stops iterating, or aclose()) all land
                    # here. The release used to live in the except arms, which
                    # GeneratorExit skips, leaking the pooled connection.
                    await scheduler._close_listener(listener)
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
