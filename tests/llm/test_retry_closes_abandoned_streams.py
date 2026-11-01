"""The retry wrapper closes what it abandons (Part 2 T50, AG-L9).

A stream whose first event was a retryable error was dropped without ``aclose``, so the concurrency slot it held stayed
taken until garbage collection; a ``max_concurrency: 1`` provider's retry waited on its own abandoned attempt.
"""

from __future__ import annotations

import asyncio

from primer.coordinator.in_memory import InMemoryRateLimiter
from primer.llm._retry import stream_with_retry
from primer.model.chat import Done, Error as ChatError, StreamStart, TextDelta


class _HeldStreams:
    """An inner LLM that holds the concurrency slot while its stream is open and keeps every stream it handed out
    (so garbage collection cannot release the slot): only an explicit ``aclose`` frees it."""

    def __init__(self, first_events: list) -> None:
        self.limiter = InMemoryRateLimiter()
        self.first_events = first_events
        self.streams: list = []
        self.closed: list[int] = []
        self.open_count_at_close: list[int] = []

    def open(self):
        n = len(self.streams)

        async def gen():
            try:
                async with await self.limiter.acquire("llm:p", max_concurrency=1):
                    events = self.first_events if n == 0 else [
                        StreamStart(model="m"), TextDelta(text="ok", index=0), Done(stop_reason="stop", raw_reason="stop"),
                    ]
                    for event in events:
                        yield event
            finally:
                self.closed.append(n)
                self.open_count_at_close.append(len(self.streams))

        stream = gen()
        self.streams.append(stream)
        return stream


class TestAbandonedStreamsAreClosed:
    async def test_a_retry_gets_the_slot_of_the_attempt_it_abandoned(self) -> None:
        inner = _HeldStreams([ChatError(fatal=True, code="server_error", message="overloaded")])

        async def run():
            return [e async for e in stream_with_retry(
                inner.open, provider_id="p", provider_kind="openchat", max_retries=2,
                base_backoff_seconds=0.0, max_backoff_seconds=0.0,
            )]

        events = await asyncio.wait_for(run(), 2.0)
        assert [type(e).__name__ for e in events] == ["StreamStart", "TextDelta", "Done"]
        assert inner.closed[0] == 0
        assert inner.open_count_at_close[0] == 1, "the abandoned attempt was closed before the retry opened"

    async def test_a_consumer_that_stops_early_closes_the_inner_stream(self) -> None:
        inner = _HeldStreams([StreamStart(model="m"), TextDelta(text="a", index=0), TextDelta(text="b", index=0)])
        outer = stream_with_retry(
            inner.open, provider_id="p", provider_kind="openchat", max_retries=2,
            base_backoff_seconds=0.0, max_backoff_seconds=0.0,
        )
        assert isinstance(await outer.__anext__(), StreamStart)
        await outer.aclose()
        assert inner.closed == [0]
