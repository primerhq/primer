"""The bounded queue behind a bus subscription (perf plan Task 6, finding H3).

A subscriber that stops reading must not make the bus grow without limit: the queue holds at most ``maxsize`` events and drops the OLDEST when
a newer one arrives, counting what it dropped. A ``coalesce`` mailbox holds one event, the newest: a stop request (cancel, terminal) is never lost
to a backlog of older traffic, because the newest always replaces the one before it. The close sentinel is never dropped or counted.
"""

from __future__ import annotations

import asyncio
from typing import Any

#: How many undelivered events one subscription may hold before the oldest are dropped.
DEFAULT_QUEUE_MAXSIZE = 1000


class Mailbox:
    def __init__(self, maxsize: int = DEFAULT_QUEUE_MAXSIZE, *, coalesce: bool = False) -> None:
        self._queue: asyncio.Queue[Any] = asyncio.Queue()
        self._limit = 1 if coalesce else max(1, maxsize)
        self.dropped = 0
        self.closed = False

    def offer(self, item: Any) -> None:
        """Queue ``item``; when the mailbox is full the oldest queued event makes room for it."""
        while self._queue.qsize() >= self._limit:
            self._queue.get_nowait()
            self.dropped += 1
        self._queue.put_nowait(item)

    def close(self) -> None:
        """Mark closed and queue the sentinel behind whatever is already waiting (events queued before the close are still delivered)."""
        if not self.closed:
            self.closed = True
            self._queue.put_nowait(None)

    def qsize(self) -> int:
        return self._queue.qsize()

    async def next(self) -> Any:
        """The next event; raises ``StopAsyncIteration`` once the mailbox is closed and drained."""
        if self.closed and self._queue.empty():
            raise StopAsyncIteration
        item = await self._queue.get()
        if item is None:
            raise StopAsyncIteration
        return item


def matches(key: str | None, prefix: str | None, event_key: str, suffix: str | None = None) -> bool:
    """Whether a subscription filtered by ``key``, or by ``prefix`` and/or ``suffix`` (nothing: everything), wants an event under ``event_key``."""
    if key is not None:
        return event_key == key
    if prefix is not None and not event_key.startswith(prefix):
        return False
    if suffix is not None and not event_key.endswith(suffix):
        return False
    return True
