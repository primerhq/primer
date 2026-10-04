"""An in-task interrupt scope: stop waiting on one await when an Event is set.

Stop (interrupt) is delivered as an :class:`asyncio.Event` that the session dispatch sets.
The agent loop used to read it only between a stream's events, so a model that had not
produced its first token could not be stopped. :func:`interruptible` wraps a single await
(``await stream.__anext__()``) and raises :class:`Interrupted` out of it when the event is set.

Why a scope on ``asyncio.timeout`` and not a task racing ``__anext__``:

* It runs in the SAME task. ``primer.llm._timeout`` already cancels a stream wait with
  ``asyncio.timeout``, so anyio cancel scopes and contextvars inside provider streams behave
  exactly as they do today. Awaiting ``__anext__`` from a different task on every step would
  not have that property.
* It reuses CPython's ``uncancel()`` accounting: a hard Cancel (``task.cancel()`` from the
  worker pool) that races the Stop is never swallowed, and a provider stall ``TimeoutError`` is
  never mistaken for a Stop.

Wrap an await, never a ``yield``: ``asyncio.timeout`` cancels the task that entered it, and a
scope left open across a ``yield`` would cancel whatever the consumer is awaiting at that moment.
"""

from __future__ import annotations

import asyncio
import contextlib


class Interrupted(Exception):
    """The interrupt event fired while the scope was waiting (or was already set on entry)."""


class interruptible:
    """``async with interruptible(event): await something()``.

    ``event=None`` is a transparent no-op, so callers that were never given an interrupt event
    pay nothing. A class rather than ``@asynccontextmanager``: that wrapper treats a
    ``StopAsyncIteration`` raised by the body specially, and the loop drives a stream's
    ``__anext__`` inside this scope, whose end is exactly that exception.
    """

    def __init__(self, event: asyncio.Event | None) -> None:
        self._event = event
        self._timeout: asyncio.Timeout | None = None
        self._waiter: asyncio.Task | None = None

    async def __aenter__(self) -> "interruptible":
        if self._event is None:
            return self
        if self._event.is_set():
            raise Interrupted
        loop = asyncio.get_running_loop()
        self._timeout = asyncio.timeout(None)
        await self._timeout.__aenter__()
        self._waiter = loop.create_task(self._event.wait())
        self._waiter.add_done_callback(self._fire)
        return self

    def _fire(self, waiter: asyncio.Future) -> None:
        if waiter.cancelled() or self._timeout is None:
            return
        # RuntimeError: the scope already exited, so there is nothing left to interrupt.
        with contextlib.suppress(RuntimeError):
            self._timeout.reschedule(asyncio.get_running_loop().time())

    async def __aexit__(self, exc_type, exc, tb) -> bool | None:
        if self._timeout is None:
            return None
        assert self._waiter is not None
        self._waiter.cancel()
        try:
            return await self._timeout.__aexit__(exc_type, exc, tb)
        except TimeoutError:
            # Raised by Timeout.__aexit__ only when ITS deadline fired, i.e. our event did. A
            # stall TimeoutError raised by the body is not converted: it reaches us as ``exc``
            # and Timeout.__aexit__ lets it propagate untouched.
            if self._timeout.expired():
                raise Interrupted from None
            raise


__all__ = ["Interrupted", "interruptible"]
