"""Transport-level retry for LLM streams.

An LLM call fails two very different ways, and only one of them is worth
retrying:

* The **transport** failed: the connection dropped, the request timed out,
  the upstream returned 5xx or asked us to slow down. Nothing about the
  request was wrong, so the same request is likely to succeed shortly.
* The **API answered, and the answer was no**: bad request, unsupported
  content, unknown model, bad credentials. Retrying replays the same
  rejection and only delays the error the operator needs to see.

:data:`RETRYABLE` encodes that split. ``RateLimitError`` is included
deliberately even though it IS an explicit API answer: a 429 is the one
rejection whose remedy is precisely "wait and try again".

**Retries stop at the first event.** Once any event has been forwarded the
consumer has already seen output, and re-running the call would duplicate
tokens that a user may already be reading. This is the same constraint the
aggregated provider settles with ``failover_point=before_first_token``, and
it is why the wrapper inspects the first event before yielding it: a stream
that fails terminally as its FIRST event is still safely retryable, because
nothing has escaped yet.
"""

from __future__ import annotations

import asyncio
import logging
import random
from collections.abc import AsyncIterator, Callable
from typing import Any

from primer.model.chat import Error as ChatError, StreamEvent
from primer.model.except_ import (
    NetworkError,
    PrimerError,
    ProviderTimeoutError,
    RateLimitError,
    ServerError,
    TransientError,
)
from primer.observability.metrics import llm_retry_total

logger = logging.getLogger(__name__)


#: Failures worth replaying the same request for. Everything else is the
#: upstream telling us the request itself is wrong.
RETRYABLE: tuple[type[PrimerError], ...] = (
    NetworkError,
    ProviderTimeoutError,
    ServerError,
    RateLimitError,
    TransientError,
)

#: Error codes on a yielded terminal Error that mark it as transport-level.
#: The classifiers erase the exception class on the yielded channel, so a
#: null code is treated as transport (that is what a dropped connection
#: looks like) while a populated code is assumed to be a real API answer.
_RETRYABLE_CODES: frozenset[str | None] = frozenset(
    {None, "timeout", "network_error", "server_error", "rate_limit"}
)


def is_retryable(exc: BaseException) -> bool:
    """True when ``exc`` is a transport failure rather than an API answer."""
    return isinstance(exc, RETRYABLE)


def backoff_delay(attempt: int, *, base: float, cap: float) -> float:
    """Exponential backoff with full jitter. ``attempt`` is 1-indexed.

    Jitter matters more here than in the worker's lease backoff: when a
    provider rate-limits, every in-flight turn hits the wall at the same
    instant, and an unjittered backoff marches them all back in lockstep.
    """
    ceiling = min(base * (2 ** (attempt - 1)), cap)
    return random.uniform(0.0, ceiling)


async def stream_with_retry(
    open_stream: Callable[[], AsyncIterator[StreamEvent]],
    *,
    provider_id: str,
    provider_kind: str,
    max_retries: int,
    base_backoff_seconds: float,
    max_backoff_seconds: float,
    sleep: Callable[[float], Any] = asyncio.sleep,
) -> AsyncIterator[StreamEvent]:
    """Yield from ``open_stream()``, replaying it on transport failures.

    ``open_stream`` must return a FRESH iterator each call; a retry
    re-invokes it rather than resuming the exhausted one.

    Only failures that occur before the first event trigger a retry.
    """
    attempt = 0
    while True:
        attempt += 1
        first: StreamEvent | None = None
        agen = open_stream()
        try:
            async for event in agen:
                first = event
                break
        except Exception as exc:  # noqa: BLE001 -- classified below
            if not is_retryable(exc) or attempt > max_retries:
                raise
            await _pause(exc, attempt, provider_id, provider_kind,
                         base_backoff_seconds, max_backoff_seconds, sleep)
            continue

        if first is None:
            return  # empty stream; nothing to retry or forward

        # A stream whose FIRST event is a terminal error has emitted nothing
        # the consumer can have acted on, so it is still safe to replay.
        if (
            isinstance(first, ChatError)
            and getattr(first, "fatal", False)
            and getattr(first, "code", None) in _RETRYABLE_CODES
            and attempt <= max_retries
        ):
            await _pause(first, attempt, provider_id, provider_kind,
                         base_backoff_seconds, max_backoff_seconds, sleep)
            continue

        # Committed: forward the first event, then the rest verbatim. Any
        # failure from here is the consumer's to see -- retrying would
        # duplicate output already delivered.
        yield first
        async for event in agen:
            yield event
        return


async def _pause(
    cause: Any,
    attempt: int,
    provider_id: str,
    provider_kind: str,
    base: float,
    cap: float,
    sleep: Callable[[float], Any],
) -> None:
    delay = backoff_delay(attempt, base=base, cap=cap)
    error_type = type(cause).__name__ if isinstance(cause, BaseException) else "Error"
    # The metric is partitioned by provider only; error_type rides on
    # the log line, where unbounded cardinality is free.
    llm_retry_total.labels(provider=provider_kind).inc()
    logger.warning(
        "LLM stream failed with a transport error; retrying",
        extra={
            "provider_id": provider_id,
            "attempt": attempt,
            "delay_seconds": round(delay, 3),
            "error_type": error_type,
        },
    )
    await sleep(delay)


__all__ = ["RETRYABLE", "backoff_delay", "is_retryable", "stream_with_retry"]
