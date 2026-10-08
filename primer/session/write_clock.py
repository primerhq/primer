"""Tells the message writer when a batch's request is actually sent (ticket 01a11b58, review of #545).

``WorkspaceMessageWriter`` abandons a batch the workspace has not answered within its write bound. Measured from the moment the batch was
handed over, that bound also counts the time the append spends waiting for the session's ``messages_lock``, which a turn persist holds
across its read, its rewrite and its git commit: a slow commit of a large ``messages.jsonl`` would make the writer drop records and fail
the turn although the workspace is healthy. The backends that take that lock say so here, and the writer starts its clock when the lock
is taken (the request is sent). A backend that does not report (a fake, a future backend) keeps the old clock, from the hand-over.

The writer binds a :class:`WriteClock` in the context of the task that runs the append (a task copies the context of the code that
creates it), and the backend calls :func:`locked_for_write` instead of ``async with lock``. No clock bound, no effect.

A leaf module on purpose: the workspace backends import it and must not pull in the session package's weight.
"""

from __future__ import annotations

import time
from collections.abc import AsyncIterator
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from contextvars import ContextVar, Token


class WriteClock:
    """What a backend has reported about one batch's append: that it queued for its lock, and when the lock was taken."""

    __slots__ = ("queued", "sent_at")

    def __init__(self) -> None:
        self.queued = False
        self.sent_at: float | None = None


_CURRENT: ContextVar[WriteClock | None] = ContextVar("message_write_clock", default=None)


def bind(clock: WriteClock) -> Token[WriteClock | None]:
    """Make ``clock`` the one :func:`locked_for_write` reports to, in the current context (and the tasks created from it)."""
    return _CURRENT.set(clock)


def unbind(token: Token[WriteClock | None]) -> None:
    _CURRENT.reset(token)


@asynccontextmanager
async def locked_for_write(lock: AbstractAsyncContextManager[None]) -> AsyncIterator[None]:
    """``async with lock``, reporting to the bound clock that the append queued for it and when it was taken."""
    clock = _CURRENT.get()
    if clock is not None:
        clock.queued = True
    async with lock:
        if clock is not None:
            clock.sent_at = time.monotonic()
        yield


__all__ = ["WriteClock", "bind", "locked_for_write", "unbind"]
