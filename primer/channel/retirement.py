"""Own unfinished SDK teardown until it is safe to release or replace a listener."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from functools import wraps

from primer.model.except_ import ProviderError

logger = logging.getLogger(__name__)

# Per-process. Pool instances distinguish independent credentials and test runtimes.
_pending: dict[tuple[object, str], asyncio.Task] = {}
_RETRY_SECONDS = 1.0


class ListenerCallbacks:
    """Application work admitted by one SDK connection on its event loop."""

    def __init__(self) -> None:
        self._closed = False
        self._tasks: set[asyncio.Task] = set()

    def guard(self, callback):
        @wraps(callback)  # Bolt introspects the original named callback arguments.
        async def guarded(*args, **kwargs):
            if self._closed:
                return
            task = asyncio.create_task(callback(*args, **kwargs))
            self._tasks.add(task)
            try:
                return await task
            finally:
                self._tasks.discard(task)

        return guarded

    def begin_close(self) -> Awaitable:
        """Seal synchronously, cancel admitted work, and return its completion."""
        self._closed = True
        for task in self._tasks:
            task.cancel()
        return asyncio.gather(*self._tasks, return_exceptions=True)


async def retry_cleanup(action: Callable[[], Awaitable[None]], label: str) -> None:
    delay = _RETRY_SECONDS
    while True:
        try:
            await action()
            return
        except Exception as exc:
            logger.warning("%s teardown unresolved (%s); retrying", label, type(exc).__name__)
            await asyncio.sleep(delay)
            delay = min(30, delay * 2)


async def finish_cleanup(work: Awaitable[None]) -> None:
    """Defer even repeated caller cancellation through the entire operation."""
    task = asyncio.ensure_future(work)
    cancelled = None
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError as exc:
            cancelled = exc
    task.result()
    if cancelled is not None:
        raise cancelled


def check_available(pool: object, provider_id: str) -> None:
    if (pool, provider_id) in _pending:
        raise ProviderError("channel provider connection is still closing")


def retire(pool: object, provider_id: str, close: Callable[[], Awaitable[None]], *,
           callbacks: ListenerCallbacks | None = None) -> asyncio.Task:
    """Called under the pool lock after detaching the last reference. The task owns the handle."""
    key = (pool, provider_id)
    joined = callbacks.begin_close() if callbacks is not None else None

    async def run() -> None:
        if joined is not None:
            await joined
        await retry_cleanup(close, f"channel provider {provider_id}")
        del _pending[key]

    task = asyncio.create_task(run(), name="channel-provider-retire")
    _pending[key] = task
    return task


async def drain_retirements() -> None:
    """Include abandoned starts and callers that were cancelled while releasing their last handle."""
    while _pending:
        await asyncio.gather(*(asyncio.shield(task) for task in tuple(_pending.values())))


async def retire_failed_start(pool: object, provider_id: str, close: Callable[[], Awaitable[None]]) -> None:
    # A broken SDK must not hold its startup caller forever. The registry still
    # fences reuse and the role owner still joins this cleanup before handoff.
    await asyncio.wait({retire(pool, provider_id, close)}, timeout=10)
