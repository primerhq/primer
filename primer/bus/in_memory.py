"""In-process event bus backed by asyncio queues.

Used by unit tests and single-process dev mode. Subscribers each get
their own queue (broadcast semantics) so multiple consumers can
observe the same event independently — matches the postgres
LISTEN/NOTIFY behaviour where every listener on a channel sees every
NOTIFY.

Not safe across processes. The production app uses
:class:`primer.bus.postgres.PostgresEventBus`.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import datetime, timezone
from typing import Any

from primer.bus._mailbox import DEFAULT_QUEUE_MAXSIZE, Mailbox, matches
from primer.int.event_bus import Event, EventBus, EventSubscription


class _InMemorySubscription(EventSubscription):
    """One subscriber's view onto the bus.

    Holds a private asyncio.Queue that the bus pushes events into.
    Iteration drains the queue until the subscription is closed.
    """

    def __init__(
        self, bus: "InMemoryEventBus", *, key: str | None = None, prefix: str | None = None, suffix: str | None = None, coalesce: bool = False, maxsize: int = DEFAULT_QUEUE_MAXSIZE,
        on_loss: "Callable[[], None] | None" = None,
    ) -> None:
        self._bus = bus
        self._key = key
        self._prefix = prefix
        self._suffix = suffix
        self._mailbox = Mailbox(maxsize, coalesce=coalesce, on_loss=on_loss)
        self._closed = False

    def wants(self, event_key: str) -> bool:
        return matches(self._key, self._prefix, event_key, self._suffix)

    def __aiter__(self) -> "_InMemorySubscription":
        return self

    async def __anext__(self) -> Event:
        return await self._mailbox.next()

    async def aclose(self) -> None:
        if self._closed:
            return
        self._closed = True
        # Drop ourselves from the bus's subscriber list so we don't
        # receive any further events.
        self._bus._unsubscribe(self)
        # Queue a sentinel so any pending __anext__ unblocks.
        self._mailbox.close()


class InMemoryEventBus(EventBus):
    """Single-process bus backed by per-subscriber asyncio.Queues."""

    def __init__(self, *, queue_maxsize: int = DEFAULT_QUEUE_MAXSIZE) -> None:
        self._queue_maxsize = queue_maxsize
        self._subscribers: list[_InMemorySubscription] = []
        self._closed = False

    async def initialize(self) -> None:
        # Nothing to do; the bus is ready on construction.
        return None

    async def aclose(self) -> None:
        if self._closed:
            return
        self._closed = True
        # Close every live subscription so any consumers
        # iterating with `async for` exit their loops.
        subs = list(self._subscribers)
        for sub in subs:
            await sub.aclose()
        self._subscribers.clear()

    async def publish(
        self,
        event_key: str,
        payload: dict[str, Any] | None = None,
    ) -> None:
        if self._closed:
            raise RuntimeError("publish on closed InMemoryEventBus")
        event = Event(
            event_key=event_key,
            payload=dict(payload or {}),
            published_at=datetime.now(timezone.utc),
        )
        for sub in list(self._subscribers):
            if sub.wants(event_key):
                sub._mailbox.offer(event)

    def subscribe(
        self,
        *,
        on_reconnect: "Callable[[], None] | None" = None,
        key: str | None = None,
        prefix: str | None = None,
        suffix: str | None = None,
        coalesce: bool = False,
    ) -> _InMemorySubscription:
        if self._closed:
            raise RuntimeError("subscribe on closed InMemoryEventBus")
        # The in-memory bus has no droppable transport, so it never reconnects. A bounded queue that overflowed IS a loss though: ``on_reconnect``
        # is called (from the consumer's read) when events were dropped, the same signal a reconnect gives.
        sub = _InMemorySubscription(
            self, key=key, prefix=prefix, suffix=suffix, coalesce=coalesce, maxsize=self._queue_maxsize, on_loss=on_reconnect,
        )
        self._subscribers.append(sub)
        return sub

    def _unsubscribe(self, sub: _InMemorySubscription) -> None:
        try:
            self._subscribers.remove(sub)
        except ValueError:
            pass


__all__ = ["InMemoryEventBus"]
