"""Streaming timeouts for LLM adapter calls.

Shared by every LLM adapter so the deadline logic is written once and
tested once. :func:`_iter_with_timeout` wraps any async iterable and adds
two independent deadlines: a per-item STALL window (``timeout_seconds`` --
no event within the window raises ``asyncio.TimeoutError``) and a TOTAL
generation budget (``total_timeout_seconds`` -- the whole iteration
exceeding it raises :class:`GenerationBudgetExceeded`, catching runaway
generations whose steady token trickle never trips the stall window).
:func:`_open_with_connect_timeout` bounds opening the stream itself.

When every deadline is ``None`` the iterable is passed through unchanged
with no overhead.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any, TypeVar

_T = TypeVar("_T")


class GenerationBudgetExceeded(TimeoutError):
    """A streamed generation exceeded its total wall-clock budget.

    Subclasses :class:`TimeoutError` so every adapter's existing
    ``except TimeoutError`` stall handler catches it unchanged; adapters
    that want a precise error message ``isinstance``-check for this class
    first. Raised by :func:`_iter_with_timeout` when
    ``total_timeout_seconds`` elapses while the stream is still emitting
    events -- the runaway-generation case the per-event stall timeout
    cannot detect (events keep arriving, so the stall window never trips).
    """


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
    total_timeout_seconds: float | None = None,
) -> AsyncIterator[Any]:
    """Yield items from ``aiter`` with per-item and total-budget deadlines.

    Parameters
    ----------
    aiter:
        Any async iterable (an SDK streaming response, for example).
    timeout_seconds:
        Maximum seconds to wait for the NEXT item (stall detection). If no
        item arrives within this window ``asyncio.TimeoutError`` is raised.
        Pass ``None`` to disable stall detection.
    total_timeout_seconds:
        Wall-clock ceiling for the WHOLE iteration, measured from the first
        wait. When it elapses :class:`GenerationBudgetExceeded` is raised --
        even if items are still arriving. This is the runaway-generation
        backstop; stall detection alone never fires while tokens trickle.
        Pass ``None`` (the default) to disable.

    Yields
    ------
    object
        Each item from ``aiter`` in order.

    Raises
    ------
    GenerationBudgetExceeded
        When ``total_timeout_seconds`` is not ``None`` and the iteration has
        run longer than that budget.
    asyncio.TimeoutError
        When ``timeout_seconds`` is not ``None`` and no item arrives within
        the configured window.
    """
    if timeout_seconds is None and total_timeout_seconds is None:
        async for item in aiter:
            yield item
        return

    loop = asyncio.get_running_loop()
    deadline = (
        loop.time() + total_timeout_seconds
        if total_timeout_seconds is not None
        else None
    )
    it = aiter.__aiter__()
    while True:
        wait = timeout_seconds
        if deadline is not None:
            remaining = deadline - loop.time()
            if remaining <= 0:
                raise GenerationBudgetExceeded(
                    f"generation exceeded its total budget of "
                    f"{total_timeout_seconds} s"
                )
            wait = remaining if wait is None else min(wait, remaining)
        try:
            async with asyncio.timeout(wait):
                item = await it.__anext__()
        except StopAsyncIteration:
            return
        except TimeoutError:
            if deadline is not None and loop.time() >= deadline:
                raise GenerationBudgetExceeded(
                    f"generation exceeded its total budget of "
                    f"{total_timeout_seconds} s"
                ) from None
            raise
        yield item
