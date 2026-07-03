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

The subscriber connection is supervised by a reconnect loop that
mirrors the scheduler's LISTEN reconnect (see
:meth:`primer.scheduler.postgres.PostgresScheduler._watch_channel`):
if the dedicated connection drops, the loop re-acquires a connection
from the pool, re-registers the LISTEN callback, and resumes pushing
events. NOTIFY messages emitted while a subscriber is reconnecting are
lost (postgres LISTEN/NOTIFY is not durable); this matches the
scheduler's best-effort wake-up contract -- the worker's claim loop is
the safety net for any missed resume signal.

Multiple subscribers on the same channel each receive every event
(broadcast -- postgres' default LISTEN/NOTIFY semantics).
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from primer.int.event_bus import Event, EventBus, EventSubscription
from primer.model.except_ import ProviderError

if TYPE_CHECKING:
    from primer.storage.postgres import PostgresStorageProvider


logger = logging.getLogger(__name__)


YIELD_EVENTS_CHANNEL = "primer_yield_events"
"""Postgres NOTIFY channel name. Distinct from session_ready so the
existing scheduler wake-up traffic doesn't double-trigger yield
resumes."""


MAX_NOTIFY_PAYLOAD_BYTES = 7900
"""Safe upper bound (UTF-8 bytes) for a single NOTIFY payload.

Postgres caps NOTIFY payloads at 8000 bytes and raises an opaque
``payload string too long`` above that. We guard slightly under 8000 (a
little headroom) and raise a clear domain error instead. Yield payloads
are meant to be small routing keys -- the real data is re-read from
storage -- so exceeding this signals a caller sending far too much, not a
legitimate large message."""


DEFAULT_LISTEN_RECONNECT_SECONDS = 2.0
"""Backoff between LISTEN reconnect attempts. Mirrors the scheduler's
``PostgresSchedulerConfig.listen_reconnect_seconds`` default (2.0s)."""


class _PostgresSubscription(EventSubscription):
    """One subscriber: a supervised LISTEN connection + queue.

    A background task acquires an asyncpg connection from the bus's
    pool, registers a LISTEN on the yield events channel, and keeps it
    alive across drops via a reconnect loop (mirrors
    :meth:`primer.scheduler.postgres.PostgresScheduler._watch_channel`).
    The asyncpg ``add_listener`` callback pushes the decoded Event onto
    an asyncio.Queue. Iteration drains the queue; close cancels the
    supervisor and releases the connection.
    """

    def __init__(
        self,
        *,
        acquire,
        release,
        reconnect_seconds: float,
        on_reconnect=None,
        on_close=None,
    ) -> None:
        self._acquire = acquire
        self._release = release
        self._reconnect_seconds = reconnect_seconds
        self._on_reconnect = on_reconnect
        # Called once on aclose so the parent bus can drop this subscription
        # from its registry (prevents an unbounded _subs leak when many
        # short-lived subscriptions open and close over the bus's lifetime).
        self._on_close = on_close
        self._queue: asyncio.Queue = asyncio.Queue()
        self._conn = None
        self._closed = False
        self._supervisor: asyncio.Task | None = None

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
        # Drop ourselves from the parent bus's registry so a long-lived bus
        # doesn't accumulate dead subscriptions. Idempotent + guarded by the
        # _closed flag above so a double aclose() only deregisters once.
        if self._on_close is not None:
            self._on_close(self)
        # Stop the supervisor; it drops the listener + releases the
        # connection in its finally block.
        if self._supervisor is not None:
            self._supervisor.cancel()
            try:
                await self._supervisor
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        await self._queue.put(None)

    def _on_notify(self, _conn, _pid, _channel, payload):
        """asyncpg listener callback -- push decoded Event into queue."""
        try:
            obj = json.loads(payload)
            event = Event(
                event_key=obj["event_key"],
                payload=obj.get("payload") or {},
                published_at=datetime.now(timezone.utc),
            )
        except Exception:  # noqa: BLE001
            logger.warning(
                "PostgresEventBus: malformed NOTIFY payload %r -- dropped",
                payload,
            )
            return
        # put_nowait is fine -- the queue is unbounded.
        self._queue.put_nowait(event)

    async def _safe_release(self, conn) -> None:
        """Release the LISTEN connection back to the pool; log + swallow
        failures so a release error doesn't mask the original cause."""
        if conn is None:
            return
        try:
            await conn.remove_listener(YIELD_EVENTS_CHANNEL, self._on_notify)
        except Exception:  # noqa: BLE001 -- best-effort on a dropped conn
            pass
        try:
            await self._release(conn)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "PostgresEventBus LISTEN pool.release failed: %s -- "
                "connection may leak",
                exc,
            )

    async def _run(self) -> None:
        """Supervise the LISTEN connection, reconnecting on drop.

        Mirrors PostgresScheduler._watch_channel's ``_iter`` loop:
        acquire + LISTEN, then block on a sentinel that the connection's
        termination listener trips; on any drop, release, back off, and
        loop to reconnect.
        """
        first_attempt = True
        try:
            while not self._closed:
                try:
                    conn = await self._acquire()
                    await conn.add_listener(YIELD_EVENTS_CHANNEL, self._on_notify)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # noqa: BLE001
                    logger.warning(
                        "PostgresEventBus LISTEN reconnect: %s", exc,
                    )
                    if not first_attempt and self._on_reconnect is not None:
                        self._on_reconnect()
                    first_attempt = False
                    await asyncio.sleep(self._reconnect_seconds)
                    continue
                if not first_attempt and self._on_reconnect is not None:
                    self._on_reconnect()
                first_attempt = False
                self._conn = conn

                # Block until the connection drops. asyncpg fires
                # termination listeners when the server connection is
                # lost; we wake the supervisor via a private event and
                # reconnect. A healthy connection simply parks here.
                dropped: asyncio.Event = asyncio.Event()

                # Bind this iteration's event via a default argument so the
                # listener sets its OWN event, not whatever ``dropped`` is
                # bound to in a later iteration. The listener is never
                # de-registered, so a stale closure left on a pooled
                # connection could otherwise fire against the current
                # iteration's event and trip a spurious reconnect.
                def _on_termination(_conn, _ev: asyncio.Event = dropped) -> None:
                    _ev.set()

                try:
                    conn.add_termination_listener(_on_termination)
                except Exception:  # noqa: BLE001 -- not all conns expose this
                    pass

                try:
                    await dropped.wait()
                except asyncio.CancelledError:
                    self._conn = None
                    await self._safe_release(conn)
                    raise
                # Connection dropped -- release, back off, reconnect.
                logger.warning(
                    "PostgresEventBus LISTEN dropped -- reconnecting",
                )
                self._conn = None
                await self._safe_release(conn)
                await asyncio.sleep(self._reconnect_seconds)
        finally:
            # On close/cancel, ensure the live connection is released.
            if self._conn is not None:
                await self._safe_release(self._conn)
                self._conn = None

    def _start(self) -> None:
        self._supervisor = asyncio.create_task(self._run())


class PostgresEventBus(EventBus):
    """LISTEN/NOTIFY-backed bus piggybacking on the existing storage pool.

    Constructor takes the :class:`PostgresStorageProvider` (already
    holding the connection pool) so the bus doesn't need its own
    config. Subscribers acquire dedicated connections from the pool
    via :meth:`PostgresStorageProvider.pool.acquire`.
    """

    def __init__(
        self,
        storage: "PostgresStorageProvider",
        *,
        reconnect_seconds: float = DEFAULT_LISTEN_RECONNECT_SECONDS,
    ) -> None:
        self._storage = storage
        self._reconnect_seconds = reconnect_seconds
        self._closed = False
        # Track live subscriptions so aclose can drop them. A set (not a list)
        # so each subscription's aclose() can deregister itself in O(1) and a
        # double aclose() is a harmless no-op -- without that deregistration
        # the bus accumulated a dead _PostgresSubscription per subscribe()
        # for its entire lifetime (an unbounded leak under high subscribe/
        # close churn, e.g. one short-lived subscription per session turn).
        self._subs: set[_PostgresSubscription] = set()
        # Metric: number of LISTEN reconnects across all subscriptions.
        self._listen_reconnects_total: int = 0

    async def initialize(self) -> None:
        # Nothing to set up -- the pool is owned by the storage
        # provider and was initialised at app startup.
        return None

    async def aclose(self) -> None:
        if self._closed:
            return
        self._closed = True
        # Iterate a snapshot: each sub.aclose() deregisters itself via the
        # on_close hook, mutating self._subs mid-loop, so we must not iterate
        # the live set directly.
        for sub in list(self._subs):
            await sub.aclose()
        self._subs.clear()

    def _unsubscribe(self, sub: "_PostgresSubscription") -> None:
        """Drop *sub* from the live registry. Idempotent (discard, not
        remove) so a sub that closes after the bus already cleared the set
        -- or closes twice -- never raises."""
        self._subs.discard(sub)

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
        # Postgres NOTIFY caps payloads at 8000 bytes; beyond that asyncpg
        # raises an opaque "payload string too long". Guard here so an
        # oversized body surfaces as a clear domain error BEFORE we acquire a
        # connection or hit the wire (BE10b). Yield payloads are meant to be
        # small routing keys (large data is re-read from storage), so this
        # only fires on a caller bug -- and we raise rather than silently
        # dropping the payload, which could strip routing fields a subscriber
        # depends on.
        encoded_len = len(body.encode("utf-8"))
        if encoded_len > MAX_NOTIFY_PAYLOAD_BYTES:
            raise ProviderError(
                f"PostgresEventBus NOTIFY payload for event_key "
                f"{event_key!r} is {encoded_len} bytes, over the "
                f"{MAX_NOTIFY_PAYLOAD_BYTES}-byte safe limit (Postgres caps "
                f"NOTIFY at 8000 bytes). Yield payloads must be small routing "
                f"keys; re-read large data from storage instead.",
                code="notify_payload_too_long",
            )
        async with self._storage.pool.acquire() as conn:
            await conn.execute(
                f"SELECT pg_notify('{YIELD_EVENTS_CHANNEL}', $1)",
                body,
            )

    def subscribe(self) -> _PostgresSubscription:
        if self._closed:
            raise RuntimeError("subscribe on closed PostgresEventBus")

        def _bump_reconnects() -> None:
            self._listen_reconnects_total += 1

        sub = _PostgresSubscription(
            acquire=self._storage.pool.acquire,
            release=self._storage.pool.release,
            reconnect_seconds=self._reconnect_seconds,
            on_reconnect=_bump_reconnects,
            on_close=self._unsubscribe,
        )
        sub._start()
        self._subs.add(sub)
        return sub

    def metrics_snapshot(self) -> dict[str, Any]:
        """Process-local bus counters.

        ``primer_yield_bus_listen_reconnects_total`` mirrors the
        scheduler's ``primer_scheduler_listen_reconnects_total`` so the
        two LISTEN supervisors expose comparable reconnect telemetry."""
        return {
            "primer_yield_bus_listen_reconnects_total": (
                self._listen_reconnects_total
            ),
        }


__all__ = ["PostgresEventBus", "YIELD_EVENTS_CHANNEL"]
