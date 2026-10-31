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
:meth:`primer.scheduler.postgres.PostgresScheduler._watch_channel`).
Both learn of a dropped connection the same way, from asyncpg's
termination listener (asyncpg never raises into the NOTIFY queue, so
nothing else would wake them), and both then release the dead
connection, wait the reconnect interval (2.0s by default), re-acquire a
connection from the pool, re-register the LISTEN callback, and resume
pushing events. Each open attempt after the first one, failed or
successful, is counted as a reconnect. NOTIFY messages emitted while a
subscriber is reconnecting are lost (postgres LISTEN/NOTIFY is not
durable); this matches the scheduler's best-effort wake-up contract --
the worker's claim loop is the safety net for any missed resume signal.

Multiple subscribers on the same channel each receive every event
(broadcast -- postgres' default LISTEN/NOTIFY semantics).
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Callable
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from primer.bus._mailbox import DEFAULT_QUEUE_MAXSIZE, Mailbox, matches
from primer.int.event_bus import Event, EventBus, EventSubscription
from primer.model.except_ import ProviderError

if TYPE_CHECKING:
    from primer.storage.postgres import PostgresStorageProvider


logger = logging.getLogger(__name__)


YIELD_EVENTS_CHANNEL = "primer_yield_events"

#: How long a caller of ``ready()`` waits for the first LISTEN before carrying on without it.
DEFAULT_READY_TIMEOUT_SECONDS = 5.0
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
        on_event=None,
    ) -> None:
        # ``on_event``: when given, a decoded event goes to it instead of this object's own queue. The bus uses ONE such supervisor per process as its
        # shared listener and fans events out to its subscriptions in process.
        self._on_event = on_event
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
        # Set once the first LISTEN has succeeded (or the subscription is closed, so a waiter never hangs on it).
        self._ready = asyncio.Event()

    async def ready(self, timeout: float | None = DEFAULT_READY_TIMEOUT_SECONDS) -> None:
        try:
            await asyncio.wait_for(self._ready.wait(), timeout=timeout)
        except TimeoutError:
            logger.warning("PostgresEventBus: subscription not listening after %ss; continuing, it keeps retrying", timeout)

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
        self._ready.set()
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
        if self._on_event is not None:
            self._on_event(event)
            return
        # put_nowait is fine -- the queue is unbounded.
        self._queue.put_nowait(event)

    async def _safe_release(self, conn, on_termination=None) -> None:
        """Release the LISTEN connection back to the pool; log + swallow
        failures so a release error doesn't mask the original cause.

        ``on_termination`` is this subscription's termination callback, taken
        off the connection first: asyncpg leaves it attached when a healthy
        connection goes back to the pool, so without this every subscription
        (about one per session turn) would add one more to a hot pooled
        connection until that connection is eventually closed, and then call
        them all at once. A dropped connection has already released and
        cleared everything, so there the removal fails and is only logged.

        The release runs on EVERY exit, in a ``finally``. ``remove_listener`` is
        an UNLISTEN round trip on a connection that is still alive, and a
        further cancel landing there raises CancelledError, which is not an
        ``Exception`` and so is not caught below. Without the ``finally`` that
        skipped the release and stranded the connection: the pool kept it for
        good and ``Pool.close()`` (which waits for every acquired connection,
        with no timeout) hung shutdown. Same shape as the scheduler's
        ``_close_listener``."""
        if conn is None:
            return
        try:
            if on_termination is not None:
                try:
                    conn.remove_termination_listener(on_termination)
                except Exception as exc:  # noqa: BLE001
                    logger.debug(
                        "PostgresEventBus remove_termination_listener failed: %s", exc,
                    )
            try:
                await conn.remove_listener(YIELD_EVENTS_CHANNEL, self._on_notify)
            except Exception:  # noqa: BLE001 -- best-effort on a dropped conn
                pass
        finally:
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
        termination listener trips (an ``asyncio.Event`` here; the scheduler
        pushes a marker into its NOTIFY queue instead); on any drop, release,
        back off, and loop to reconnect. Both also take their termination
        callback off the connection when they release it, because asyncpg
        leaves it attached across a release to the pool.

        ``self._conn`` means "fully listening": it is set only once LISTEN has
        succeeded. A connection that is acquired but not yet listening (or
        whose LISTEN failed) is released by the arms around the LISTEN call,
        not by the final cleanup.
        """
        first_attempt = True
        # The current iteration's termination callback, kept here (not just in
        # the loop body) so the final cleanup can take it off too.
        on_termination = None
        try:
            while not self._closed:
                conn = None
                try:
                    conn = await self._acquire()
                    await conn.add_listener(YIELD_EVENTS_CHANNEL, self._on_notify)
                except asyncio.CancelledError:
                    # Cancelled during acquire or the LISTEN round trip. The
                    # connection is not published to self._conn until LISTEN
                    # succeeds, so the final cleanup below cannot release it;
                    # do it here (a no-op when acquire itself was cancelled,
                    # as conn is still None). _safe_release swallows its own
                    # failures, so nothing here can replace the cancellation.
                    await self._safe_release(conn)
                    raise
                except Exception as exc:  # noqa: BLE001
                    # LISTEN failed on a connection that may still be live (a
                    # dropped one releases itself). Give it back before the
                    # retry, or every attempt would strand one pooled
                    # connection until the pool ran dry.
                    await self._safe_release(conn)
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
                self._ready.set()

                # Block until the connection drops. asyncpg fires
                # termination listeners when the server connection is
                # lost; we wake the supervisor via a private event and
                # reconnect. A healthy connection simply parks here.
                dropped: asyncio.Event = asyncio.Event()

                # Bind this iteration's event via a default argument so the
                # listener sets its OWN event, not whatever ``dropped`` is
                # bound to in a later iteration. The callback is taken off the
                # connection on release (see _safe_release), but if that
                # removal ever fails a stale closure left on a pooled
                # connection could otherwise fire against the current
                # iteration's event and trip a spurious reconnect.
                def _on_termination(_conn, _ev: asyncio.Event = dropped) -> None:
                    _ev.set()

                try:
                    conn.add_termination_listener(_on_termination)
                    on_termination = _on_termination
                except Exception:  # noqa: BLE001 -- not all conns expose this
                    pass

                try:
                    await dropped.wait()
                except asyncio.CancelledError:
                    self._conn = None
                    await self._safe_release(conn, on_termination)
                    raise
                # Connection dropped -- release, back off, reconnect.
                logger.warning(
                    "PostgresEventBus LISTEN dropped -- reconnecting",
                )
                self._conn = None
                await self._safe_release(conn, on_termination)
                on_termination = None
                await asyncio.sleep(self._reconnect_seconds)
        finally:
            # On close/cancel, ensure the live connection is released.
            if self._conn is not None:
                await self._safe_release(self._conn, on_termination)
                self._conn = None

    def _start(self) -> None:
        self._supervisor = asyncio.create_task(self._run())


class _BusSubscription(EventSubscription):
    """One subscriber of a :class:`PostgresEventBus`: a bounded mailbox the bus fans matching events into.

    It holds no connection of its own; the bus keeps a single shared LISTEN per process (``PostgresEventBus._listener``) and routes each decoded
    notification to the mailboxes that want its key.
    """

    def __init__(
        self,
        bus: "PostgresEventBus",
        *,
        key: str | None,
        prefix: str | None,
        suffix: str | None,
        coalesce: bool,
        on_reconnect: Callable[[], None] | None,
        maxsize: int,
    ) -> None:
        self._bus = bus
        self._key = key
        self._prefix = prefix
        self._suffix = suffix
        self._on_reconnect = on_reconnect
        self._mailbox = Mailbox(maxsize, coalesce=coalesce, on_loss=on_reconnect)
        self._closed = False

    def wants(self, event_key: str) -> bool:
        return matches(self._key, self._prefix, event_key, self._suffix)

    def __aiter__(self) -> "_BusSubscription":
        return self

    async def __anext__(self) -> Event:
        return await self._mailbox.next()

    async def ready(self, timeout: float | None = DEFAULT_READY_TIMEOUT_SECONDS) -> None:
        listener = self._bus._listener
        if listener is not None:
            await listener.ready(timeout)

    async def aclose(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._bus._unsubscribe(self)
        self._mailbox.close()
        await self._bus._release_listener_if_idle()


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
        queue_maxsize: int = DEFAULT_QUEUE_MAXSIZE,
    ) -> None:
        self._storage = storage
        self._reconnect_seconds = reconnect_seconds
        self._queue_maxsize = queue_maxsize
        self._closed = False
        # Track live subscriptions so aclose can drop them. A set (not a list)
        # so each subscription's aclose() can deregister itself in O(1) and a
        # double aclose() is a harmless no-op -- without that deregistration
        # the bus accumulated a dead _PostgresSubscription per subscribe()
        # for its entire lifetime (an unbounded leak under high subscribe/
        # close churn, e.g. one short-lived subscription per session turn).
        self._subs: set[_BusSubscription] = set()
        # Routing: subscriptions asking for one exact key, and the rest (all events, or a prefix), checked per event.
        self._by_key: dict[str, set[_BusSubscription]] = {}
        self._other: set[_BusSubscription] = set()
        # The ONE LISTEN connection of this process (a supervised _PostgresSubscription in "fan-out" mode), started by the first subscription and
        # released with the last. Before this every subscription (one per running turn) held its own pooled connection.
        self._listener: _PostgresSubscription | None = None
        # Metric: number of LISTEN reconnects of the shared listener.
        self._listen_reconnects_total: int = 0

    async def initialize(self) -> None:
        # Nothing to set up -- the pool is owned by the storage
        # provider and was initialised at app startup.
        return None

    async def aclose(self) -> None:
        if self._closed:
            return
        self._closed = True
        # Iterate a snapshot: each sub.aclose() deregisters itself, mutating self._subs mid-loop.
        for sub in list(self._subs):
            await sub.aclose()
        self._subs.clear()
        await self._release_listener_if_idle()

    def _unsubscribe(self, sub: "_BusSubscription") -> None:
        """Drop *sub* from the live registry and the routing tables. Idempotent."""
        self._subs.discard(sub)
        self._other.discard(sub)
        if sub._key is not None:
            keyed = self._by_key.get(sub._key)
            if keyed is not None:
                keyed.discard(sub)
                if not keyed:
                    del self._by_key[sub._key]

    def _ensure_listener(self) -> "_PostgresSubscription":
        if self._listener is None:
            self._listener = _PostgresSubscription(
                acquire=self._storage.pool.acquire,
                release=self._storage.pool.release,
                reconnect_seconds=self._reconnect_seconds,
                on_reconnect=self._on_listener_reconnect,
                on_event=self._fan_out,
            )
            self._listener._start()
        return self._listener

    async def _release_listener_if_idle(self) -> None:
        """Stop the shared LISTEN once nothing is subscribed (the connection goes back to the pool)."""
        if self._subs or self._listener is None:
            return
        listener, self._listener = self._listener, None  # a subscribe during the await below starts a fresh listener
        await listener.aclose()

    def _on_listener_reconnect(self) -> None:
        # One reconnect of the shared connection: count it once, tell every subscription (each caller's hook must never break the supervisor).
        self._listen_reconnects_total += 1
        for sub in list(self._subs):
            if sub._on_reconnect is not None:
                try:
                    sub._on_reconnect()
                except Exception:  # noqa: BLE001 -- never break the loop
                    logger.exception("PostgresEventBus: subscribe on_reconnect hook raised; continuing")

    def _fan_out(self, event: Event) -> None:
        """Hand one decoded notification to the subscriptions that want its key (decoded once for the whole process)."""
        targets = list(self._by_key.get(event.event_key, ()))
        targets.extend(sub for sub in self._other if sub.wants(event.event_key))
        for sub in targets:
            sub._mailbox.offer(event)

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

    async def publish_local(self, event_key: str, payload: dict[str, Any] | None = None) -> None:
        """Deliver to this process's subscriptions without a NOTIFY (no other process hears it; ``publish`` still crosses processes)."""
        if self._closed:
            raise RuntimeError("publish on closed PostgresEventBus")
        self._fan_out(Event(event_key=event_key, payload=dict(payload or {}), published_at=datetime.now(timezone.utc)))

    def subscribe(
        self,
        *,
        on_reconnect: Callable[[], None] | None = None,
        key: str | None = None,
        prefix: str | None = None,
        suffix: str | None = None,
        coalesce: bool = False,
    ) -> _BusSubscription:
        if self._closed:
            raise RuntimeError("subscribe on closed PostgresEventBus")
        sub = _BusSubscription(
            self, key=key, prefix=prefix, suffix=suffix, coalesce=coalesce, on_reconnect=on_reconnect, maxsize=self._queue_maxsize,
        )
        self._subs.add(sub)
        if key is not None:
            self._by_key.setdefault(key, set()).add(sub)
        else:
            self._other.add(sub)
        self._ensure_listener()
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
