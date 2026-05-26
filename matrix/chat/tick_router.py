"""Process-local pub/sub for chat tick events.

The bus (in-memory or Postgres) is broadcast — every process gets
every event. To keep WS handlers from each owning a bus subscription
of their own (one ``LISTEN`` per WebSocket on Postgres = bad), the
process subscribes to the bus ONCE and routes incoming chat tick
events through this router to per-chat in-process queues.

Lifecycle:
* The router is created in the app lifespan and stashed on
  ``app.state.chat_tick_router``.
* A bus listener forwards events with key ``chat:{cid}:tick`` to
  ``router.publish(cid, Tick(seq=payload['seq']))``.
* WS handlers call ``router.subscribe(cid)`` to get an
  ``AsyncIterator[Tick]`` for the chat they're streaming.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass


@dataclass(frozen=True)
class Tick:
    """One chat tick — signals storage has new rows up to ``seq``."""
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


class ChatTickRouter:
    """In-process fan-out of chat tick events to per-chat subscribers."""

    def __init__(self) -> None:
        self._chat_subs: dict[str, set[asyncio.Queue[Tick]]] = {}

    def subscribe(self, chat_id: str) -> AsyncIterator[Tick]:
        queue: asyncio.Queue[Tick] = asyncio.Queue()
        self._chat_subs.setdefault(chat_id, set()).add(queue)

        def _deregister(q: asyncio.Queue[Tick]) -> None:
            subs = self._chat_subs.get(chat_id)
            if subs is None:
                return
            subs.discard(q)
            if not subs:
                self._chat_subs.pop(chat_id, None)

        return _Subscription(queue, _deregister)

    def publish(self, chat_id: str, tick: Tick) -> None:
        """Fan ``tick`` out to every subscriber for ``chat_id``.

        Non-blocking. The per-subscriber queues are unbounded by
        default, so ``put_nowait`` will not raise in practice; if a
        bounded queue is introduced later, callers should handle
        ``asyncio.QueueFull`` gracefully (drop or log) since chat
        ticks are advisory — storage is the source of truth.

        Single-process asyncio: ``publish`` contains no ``await``
        points, so no subscriber can close (via ``aclose``) mid-
        iteration over ``subs``. The set copy avoids any potential
        mutation issues if the caller ever introduces async fan-out.
        """
        subs = self._chat_subs.get(chat_id)
        if not subs:
            return
        for q in list(subs):
            q.put_nowait(tick)
