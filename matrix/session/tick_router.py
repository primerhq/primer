"""Process-local pub/sub for session tick events.

The bus (in-memory or Postgres) is broadcast — every process gets
every event. To keep WS handlers from each owning a bus subscription
of their own (one ``LISTEN`` per WebSocket on Postgres = bad), the
process subscribes to the bus ONCE and routes incoming session tick
events through this router to per-session in-process queues.

Lifecycle:
* The router is created in the app lifespan and stashed on
  ``app.state.session_tick_router``.
* A bus listener forwards events with key ``session:{sid}:tick`` to
  ``router._publish(sid, Tick(seq=payload['seq']))``.
* WS handlers call ``router.subscribe(sid)`` to get an
  ``AsyncIterator[Tick]`` for the session they're streaming.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass


@dataclass(frozen=True)
class Tick:
    """One session tick — signals storage has new rows up to ``seq``."""
    seq: int


class _Subscription:
    """Async iterator wrapping one subscriber's queue."""

    def __init__(self, queue: asyncio.Queue[Tick], on_close) -> None:
        self._queue = queue
        self._on_close = on_close
        self._closed = False

    def __aiter__(self) -> "_Subscription":
        return self

    async def __anext__(self) -> Tick:
        if self._closed:
            raise StopAsyncIteration
        return await self._queue.get()

    async def aclose(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._on_close(self._queue)


class SessionTickRouter:
    """In-process fan-out of session tick events to per-session subscribers."""

    def __init__(self) -> None:
        self._subs: dict[str, set[asyncio.Queue[Tick]]] = {}

    def subscribe(self, session_id: str) -> AsyncIterator[Tick]:
        queue: asyncio.Queue[Tick] = asyncio.Queue()
        self._subs.setdefault(session_id, set()).add(queue)

        def _deregister(q: asyncio.Queue[Tick]) -> None:
            subs = self._subs.get(session_id)
            if subs is None:
                return
            subs.discard(q)
            if not subs:
                self._subs.pop(session_id, None)

        return _Subscription(queue, _deregister)

    def _publish(self, session_id: str, tick: Tick) -> None:
        """Fan ``tick`` out to every subscriber for ``session_id``.

        Non-blocking. The per-subscriber queues are unbounded by
        default, so ``put_nowait`` will not raise in practice; if a
        bounded queue is introduced later, callers should handle
        ``asyncio.QueueFull`` gracefully (drop or log) since session
        ticks are advisory — storage is the source of truth.

        Single-process asyncio: ``_publish`` contains no ``await``
        points, so no subscriber can close (via ``aclose``) mid-
        iteration over ``subs``. The set copy avoids any potential
        mutation issues if the caller ever introduces async fan-out.
        """
        subs = self._subs.get(session_id)
        if not subs:
            return
        for q in list(subs):
            q.put_nowait(tick)
