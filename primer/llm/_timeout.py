"""Per-event inactivity timeout for LLM streaming calls.

Shared by every LLM adapter so the stall-detection logic is written once
and tested once. The helper wraps any async iterable and adds a per-item
deadline: if no event arrives within ``timeout_seconds`` the underlying
``__anext__`` call is cancelled and ``asyncio.TimeoutError`` is raised,
propagating to the adapter's caller.

When ``timeout_seconds`` is ``None`` the iterable is passed through
unchanged with no overhead.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any, TypeVar

_T = TypeVar("_T")


async def _open_with_connect_timeout(
    opener: Callable[[], Awaitable[_T]],
    connect_timeout_seconds: float | None,
    *,
    provider_id: str,
    model: str,
) -> _T:
    """Open a provider stream bounded by a CONNECT timeout.

    ``opener`` performs the post-slot stream-open (e.g.
    ``lambda: client.messages.create(stream=True, **request)``). The queue
    wait for a concurrency slot must already have completed -- this only
    bounds OPENING the stream, which on a just-in-time backend includes a
    cold model load. ``connect_timeout_seconds`` of ``None`` means unbounded:
    a slow cold load is never aborted. On timeout, raises
    :class:`primer.model.except_.ProviderTimeoutError` with
    ``code="connect_timeout"``, mirroring the per-event stall timeout's
    failure shape so callers handle both the same way.
    """
    if connect_timeout_seconds is None:
        return await opener()
    try:
        async with asyncio.timeout(connect_timeout_seconds):
            return await opener()
    except TimeoutError as exc:
        from primer.model.except_ import ProviderTimeoutError

        raise ProviderTimeoutError(
            f"upstream did not begin responding within "
            f"{connect_timeout_seconds} s after acquiring a concurrency slot "
            f"(provider_id={provider_id!r}, model={model!r})",
            code="connect_timeout",
        ) from exc


async def _iter_with_timeout(
    aiter: Any,
    timeout_seconds: float | None,
) -> AsyncIterator[Any]:
    """Yield items from ``aiter`` with a per-item inactivity timeout.

    Parameters
    ----------
    aiter:
        Any async iterable (an SDK streaming response, for example).
    timeout_seconds:
        Maximum seconds to wait for the NEXT item. If no item arrives
        within this window ``asyncio.TimeoutError`` is raised. Pass
        ``None`` to disable the timeout entirely (items are awaited
        without a deadline).

    Yields
    ------
    object
        Each item from ``aiter`` in order.

    Raises
    ------
    asyncio.TimeoutError
        When ``timeout_seconds`` is not ``None`` and no item arrives
        within the configured window.
    """
    if timeout_seconds is None:
        async for item in aiter:
            yield item
        return

    it = aiter.__aiter__()
    while True:
        try:
            async with asyncio.timeout(timeout_seconds):
                item = await it.__anext__()
        except StopAsyncIteration:
            return
        yield item
