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
from collections.abc import AsyncIterator
from typing import Any, TypeVar

_T = TypeVar("_T")


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
