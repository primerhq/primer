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
from datetime import datetime, timezone
from typing import Any

from primer.int.event_bus import Event, EventBus, EventSubscription


class _InMemorySubscription(EventSubscription):
    """One subscriber's view onto the bus.

    Holds a private asyncio.Queue that the bus pushes events into.
    Iteration drains the queue until the subscription is closed.
    """

    def __init__(self, bus: "InMemoryEventBus") -> None:
        self._bus = bus
        self._queue: asyncio.Queue[Event | None] = asyncio.Queue()
        self._closed = False

    def __aiter__(self) -> "_InMemorySubscription":
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
        # Drop ourselves from the bus's subscriber list so we don't
        # receive any further events.
        self._bus._unsubscribe(self)
        # Push a sentinel so any pending __anext__ unblocks.
        await self._queue.put(None)


class InMemoryEventBus(EventBus):
    """Single-process bus backed by per-subscriber asyncio.Queues."""

    def __init__(self) -> None:
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
        for sub in self._subscribers:
            await sub._queue.put(event)

    def subscribe(self) -> _InMemorySubscription:
        if self._closed:
            raise RuntimeError("subscribe on closed InMemoryEventBus")
        sub = _InMemorySubscription(self)
        self._subscribers.append(sub)
        return sub

    def _unsubscribe(self, sub: _InMemorySubscription) -> None:
        try:
            self._subscribers.remove(sub)
        except ValueError:
            pass


__all__ = ["InMemoryEventBus"]
