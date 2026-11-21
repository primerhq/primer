"""Own an iterator's resources, independently of the tasks advancing it."""
from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator, Callable
from contextvars import ContextVar

from primer.llm.codex.boundary import BoundaryError
from primer.model.chat import StreamEvent


# Immutable ancestry follows tasks created by a source or its finalizer. Task
# identity alone misses callbacks running inside a separate cleanup task.
_OWNERS: ContextVar[tuple[object, ...]] = ContextVar("codex_stream_owners", default=())


async def drain(task: asyncio.Task) -> None:
    """Join owned cleanup despite repeated cancellation; then propagate it."""
    cancelled = False
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            cancelled = True
    try:
        task.result()
    finally:
        if cancelled:
            raise asyncio.CancelledError


class OwnedStream:
    """One advancement at a time, possibly from a different task each time.

    The owner retains this object until exhaustion or deterministic close.
    An absolute deadline is checked on every advance, including consumer idle
    time. No asyncio timeout context survives a yield into another task.
    """
    def __init__(self, source: AsyncGenerator[StreamEvent, None], *,
                 on_close: Callable[[OwnedStream], None], timeout: float | None = None):
        self._source = source
        self._on_close = on_close
        self._timeout = timeout
        self._deadline: float | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._pending: asyncio.Task | None = None
        self._advanced: asyncio.Event | None = None
        self._closing: asyncio.Task | None = None
        self._closed = False
        self._exhausted = False

    def __aiter__(self):
        return self

    async def __anext__(self) -> StreamEvent:
        if self._exhausted:
            raise StopAsyncIteration
        if self._closed:
            raise BoundaryError("Codex stream is closed")
        loop = asyncio.get_running_loop()
        if self._loop is None:
            self._loop = loop
            if self._timeout is not None:
                self._deadline = loop.time() + self._timeout
        if loop is not self._loop or self._pending is not None:
            raise BoundaryError("Codex stream has concurrent or cross-event loop advancement")
        self._pending = asyncio.current_task()
        self._advanced = asyncio.Event()
        try:
            token = _OWNERS.set((*_OWNERS.get(), self))
            try:
                if self._deadline is not None and loop.time() >= self._deadline:
                    raise TimeoutError("Codex stream deadline expired")
                async with asyncio.timeout_at(self._deadline):
                    return await anext(self._source)
            finally:
                _OWNERS.reset(token)
                self._pending = None
                self._advanced.set()
        except StopAsyncIteration:
            self._exhausted = True
            await self.aclose()
            raise
        except BaseException:
            await self.aclose()
            raise

    def ensure_can_close(self) -> None:
        if self in _OWNERS.get():
            raise BoundaryError("Codex stream cannot close inside its own advancement or cleanup ancestry")
        if self._pending is asyncio.current_task():
            raise BoundaryError("Codex stream cannot close inside its own advancement")

    async def aclose(self) -> None:
        # Check ancestry BEFORE joining an existing close task. The callback
        # may run in a child task whose ancestor is waiting for that callback.
        self.ensure_can_close()
        self._closed = True
        if self._closing is None:
            self._closing = asyncio.create_task(self._finish_close())
        await drain(self._closing)

    async def _finish_close(self) -> None:
        token = _OWNERS.set((*_OWNERS.get(), self))
        try:
            if self._pending is not None:
                self._pending.cancel()
                # Join the advance, not its consumer's entire task. That task
                # can itself be joining this same close after cancellation.
                await self._advanced.wait()
            await self._source.aclose()
        finally:
            _OWNERS.reset(token)
            self._on_close(self)
