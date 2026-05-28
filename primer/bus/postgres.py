"""Postgres LISTEN/NOTIFY-backed event bus for yielding tools.

Spec: ``docs/superpowers/specs/2026-05-22-yielding-tools-design.md`` §6.2.

Uses a sibling channel to the existing scheduler ``session_ready``
NOTIFY: ``primer_yield_events``. Payloads are JSON-encoded dicts of
``{"event_key": ..., "payload": ...}`` so a single channel handles
all yield event_keys without spawning per-key channels.

Publishers call :meth:`publish`, which runs ``NOTIFY
primer_yield_events, '<json>'``. Subscribers acquire a dedicated
connection via :meth:`subscribe`, register an asyncpg ``add_listener``
callback that pushes received payloads into a queue, and iterate the
queue as :class:`Event` instances.

Multiple subscribers on the same channel each receive every event
(broadcast — postgres' default LISTEN/NOTIFY semantics).
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from primer.int.event_bus import Event, EventBus, EventSubscription

if TYPE_CHECKING:
    from primer.storage.postgres import PostgresStorageProvider


logger = logging.getLogger(__name__)


YIELD_EVENTS_CHANNEL = "primer_yield_events"
"""Postgres NOTIFY channel name. Distinct from session_ready so the
existing scheduler wake-up traffic doesn't double-trigger yield
resumes."""


class _PostgresSubscription(EventSubscription):
    """One subscriber connection + queue.

    Holds an asyncpg connection from the bus's pool with a LISTEN
    on the yield events channel. The asyncpg ``add_listener``
    callback pushes the decoded Event onto an asyncio.Queue. Iteration
    drains the queue; close releases the connection.
    """

    def __init__(
        self,
        conn,
        queue: asyncio.Queue,
        release: callable,
    ) -> None:
        self._conn = conn
        self._queue = queue
        self._release = release
        self._closed = False

    def __aiter__(self) -> "_PostgresSubscription":
        return self

    async def __anext__(self) -> Event:
        if self._closed and self._queue.empty():
            raise StopAsyncIteration
        item = await self._queue.get()
        if item is None:  # close sentinel
            raise StopAsyncIteration
        return item

    async def aclose(self) -> None:
        if self._closed:
            return
        self._closed = True
        # Drop the listener + release the connection back to the pool.
        try:
            await self._conn.remove_listener(
                YIELD_EVENTS_CHANNEL, self._on_notify,
            )
        except Exception:  # noqa: BLE001 — best-effort on shutdown
            pass
        try:
            await self._release(self._conn)
        except Exception:  # noqa: BLE001
            pass
        await self._queue.put(None)

    def _on_notify(self, _conn, _pid, _channel, payload):
        """asyncpg listener callback — push decoded Event into queue."""
        try:
            obj = json.loads(payload)
            event = Event(
                event_key=obj["event_key"],
                payload=obj.get("payload") or {},
                published_at=datetime.now(timezone.utc),
            )
        except Exception:  # noqa: BLE001
            logger.warning(
                "PostgresEventBus: malformed NOTIFY payload %r — dropped",
                payload,
            )
            return
        # put_nowait is fine — the queue is unbounded.
        self._queue.put_nowait(event)


class PostgresEventBus(EventBus):
    """LISTEN/NOTIFY-backed bus piggybacking on the existing storage pool.

    Constructor takes the :class:`PostgresStorageProvider` (already
    holding the connection pool) so the bus doesn't need its own
    config. Subscribers acquire dedicated connections from the pool
    via :meth:`PostgresStorageProvider.pool.acquire`.
    """

    def __init__(self, storage: "PostgresStorageProvider") -> None:
        self._storage = storage
        self._closed = False
        # Track live subscriptions so aclose can drop them.
        self._subs: list[_PostgresSubscription] = []

    async def initialize(self) -> None:
        # Nothing to set up — the pool is owned by the storage
        # provider and was initialised at app startup.
        return None

    async def aclose(self) -> None:
        if self._closed:
            return
        self._closed = True
        for sub in list(self._subs):
            await sub.aclose()
        self._subs.clear()

    async def publish(
        self,
        event_key: str,
        payload: dict[str, Any] | None = None,
    ) -> None:
        if self._closed:
            raise RuntimeError("publish on closed PostgresEventBus")
        body = json.dumps({
            "event_key": event_key,
            "payload": payload or {},
        })
        async with self._storage.pool.acquire() as conn:
            await conn.execute(
                f"SELECT pg_notify('{YIELD_EVENTS_CHANNEL}', $1)",
                body,
            )

    def subscribe(self) -> _PostgresSubscription:
        if self._closed:
            raise RuntimeError("subscribe on closed PostgresEventBus")
        # asyncpg add_listener requires a dedicated connection;
        # acquire from the pool and remember the release callback.
        # Note: subscribe() is sync to match the EventBus interface;
        # the actual asyncpg acquire happens on the first __anext__
        # via a lazy initialise path. To keep the impl simple we
        # use a sentinel queue and a task that does the acquire.
        queue: asyncio.Queue = asyncio.Queue()

        sub = _PostgresSubscription(
            conn=None,  # populated by the init task below
            queue=queue,
            release=self._storage.pool.release,
        )

        async def _init() -> None:
            try:
                conn = await self._storage.pool.acquire()
                sub._conn = conn
                await conn.add_listener(
                    YIELD_EVENTS_CHANNEL, sub._on_notify,
                )
            except Exception as exc:  # noqa: BLE001
                logger.exception(
                    "PostgresEventBus: subscribe init failed: %s", exc,
                )
                await queue.put(None)

        # Schedule the init on the running loop.
        asyncio.create_task(_init())
        self._subs.append(sub)
        return sub


__all__ = ["PostgresEventBus", "YIELD_EVENTS_CHANNEL"]
