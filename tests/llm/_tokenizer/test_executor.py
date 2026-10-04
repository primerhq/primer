"""primer.llm._tokenizer._executor: synchronous counters run off the event loop."""

from __future__ import annotations

import asyncio
import threading
import time

import pytest

from primer.llm._tokenizer._executor import MAX_WORKERS, run_counter
from primer.model.except_ import TokenCounterUnavailable


async def test_a_blocking_counter_does_not_block_the_event_loop():
    gaps: list[float] = []
    stop = asyncio.Event()

    async def ticker():
        last = time.monotonic()
        while not stop.is_set():
            await asyncio.sleep(0.01)
            now = time.monotonic()
            gaps.append(now - last)
            last = now

    task = asyncio.create_task(ticker())
    await asyncio.sleep(0.05)  # let the ticker establish its rhythm
    result = await run_counter(lambda: (time.sleep(0.3), 42)[1])
    stop.set()
    await task
    assert result == 42
    assert len(gaps) > 10
    assert max(gaps) < 0.15, f"the loop stalled for {max(gaps):.3f}s"


async def test_what_the_counter_raises_propagates():
    def bad():
        raise ValueError("boom")

    with pytest.raises(ValueError, match="boom"):
        await run_counter(bad)


async def test_a_count_that_cannot_start_in_time_is_transient_unavailable():
    release = threading.Event()
    started = threading.Barrier(MAX_WORKERS + 1, timeout=5)

    def hold():
        started.wait()
        release.wait(5)

    hogs = [asyncio.ensure_future(run_counter(hold, queue_wait_s=5)) for _ in range(MAX_WORKERS)]
    await asyncio.get_running_loop().run_in_executor(None, started.wait)
    try:
        with pytest.raises(TokenCounterUnavailable, match="did not start") as exc:
            await run_counter(lambda: 1, queue_wait_s=0.05)
        assert exc.value.transient is True
    finally:
        release.set()
        await asyncio.gather(*hogs)


async def test_cancelling_the_caller_propagates_cancellation():
    release = threading.Event()
    task = asyncio.ensure_future(run_counter(lambda: release.wait(5)))
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    release.set()
