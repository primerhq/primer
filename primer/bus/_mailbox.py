"""The bounded queue behind a bus subscription (perf plan Task 6, finding H3).

A subscriber that stops reading must not make the bus grow without limit: the queue holds at most ``maxsize`` events and drops the OLDEST when
a newer one arrives, counting what it dropped. A ``coalesce`` mailbox holds one event, the newest: a stop request (cancel, terminal) is never lost
to a backlog of older traffic, because the newest always replaces the one before it. The close sentinel is never dropped or counted.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable
from typing import Any

logger = logging.getLogger(__name__)

#: The shortest time between two loss signals to one consumer: a stalled consumer that keeps overflowing cannot spin its hook.
LOSS_SIGNAL_MIN_INTERVAL_SECONDS = 1.0

#: How many undelivered events one subscription may hold before the oldest are dropped.
DEFAULT_QUEUE_MAXSIZE = 1000


class Mailbox:
    """``on_loss`` is called (once per burst, at most every ``min_signal_interval`` seconds) from the consumer's own ``next()`` after events were dropped:
    a dropped event is a loss exactly like a reconnect, and a consumer that caches state from events (cache invalidation) must treat that state as
    stale. A ``coalesce`` mailbox drops by design (the newest replaces the one before) and never signals."""

    def __init__(
        self,
        maxsize: int = DEFAULT_QUEUE_MAXSIZE,
        *,
        coalesce: bool = False,
        on_loss: Callable[[], None] | None = None,
        clock: Callable[[], float] = time.monotonic,
        min_signal_interval: float = LOSS_SIGNAL_MIN_INTERVAL_SECONDS,
    ) -> None:
        self._queue: asyncio.Queue[Any] = asyncio.Queue()
        self._limit = 1 if coalesce else max(1, maxsize)
        self._coalesce = coalesce
        self._on_loss = on_loss
        self._clock = clock
        self._min_signal_interval = min_signal_interval
        self._lost = False
        self._last_signal = float("-inf")
        self.dropped = 0
        self.closed = False

    def offer(self, item: Any) -> None:
        """Queue ``item``; when the mailbox is full the oldest queued event makes room for it. A closed mailbox takes nothing (the close sentinel
        is the last thing in it and is never evicted)."""
        if self.closed:
            return
        while self._queue.qsize() >= self._limit:
            self._queue.get_nowait()
            self.dropped += 1
            if not self._coalesce:
                self._lost = True
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
        self._signal_loss_if_due()
        return item

    def _signal_loss_if_due(self) -> None:
        if not self._lost or self._on_loss is None:
            return
        now = self._clock()
        if now - self._last_signal < self._min_signal_interval:
            return  # held back; the loss stays pending and is signalled at the first read after the interval
        self._lost = False
        self._last_signal = now
        try:
            self._on_loss()
        except Exception:  # noqa: BLE001 - a failing hook must not break the consumer
            logger.exception("event bus: the loss hook raised; continuing")


def matches(key: str | None, prefix: str | None, event_key: str, suffix: str | None = None) -> bool:
    """Whether a subscription filtered by ``key``, or by ``prefix`` and/or ``suffix`` (nothing: everything), wants an event under ``event_key``."""
    if key is not None:
        return event_key == key
    if prefix is not None and not event_key.startswith(prefix):
        return False
    if suffix is not None and not event_key.endswith(suffix):
        return False
    return True
