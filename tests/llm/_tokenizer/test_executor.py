"""primer.llm._tokenizer._executor: synchronous counters run off the event loop."""

from __future__ import annotations

import asyncio
import threading
import time

import pytest

from primer.llm._tokenizer import _executor
from primer.llm._tokenizer._executor import MAX_WORKERS, run_counter
from primer.model.except_ import TokenCounterUnavailable


@pytest.fixture(autouse=True)
def _fresh_executor():
    """The pool is a process-wide singleton: a slow count left by an earlier test
    must not occupy a worker these tests are about to saturate."""
    _executor.shutdown_counter_executor()
    yield
    _executor.shutdown_counter_executor()


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
    result = await run_counter(lambda: (time.sleep(0.6), 42)[1])
    stop.set()
    await task
    assert result == 42
    assert len(gaps) > 10
    # A blocked loop shows a gap of the full 0.6 s; 0.3 s leaves generous room for
    # scheduler jitter on a loaded host (this once flaked at a 0.15 s bound).
    assert max(gaps) < 0.3, f"the loop stalled for {max(gaps):.3f}s"


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


async def _saturate(release: threading.Event):
    """Occupy every worker; returns the running tasks once they are all inside."""
    entered = threading.Barrier(MAX_WORKERS + 1, timeout=5)

    def hold():
        entered.wait()
        release.wait(10)

    hogs = [asyncio.ensure_future(run_counter(hold, queue_wait_s=10)) for _ in range(MAX_WORKERS)]
    await asyncio.get_running_loop().run_in_executor(None, entered.wait)
    return hogs


async def test_a_queued_count_that_times_out_never_runs_after_the_workers_free_up():
    """The cancel on the timeout path is load-bearing: without it the abandoned
    count would still run once a worker frees, burning CPU for nobody."""
    release = threading.Event()
    hogs = await _saturate(release)
    ran: list[str] = []
    try:
        with pytest.raises(TokenCounterUnavailable):
            await run_counter(lambda: ran.append("late"), queue_wait_s=0.05)
    finally:
        release.set()
        await asyncio.gather(*hogs)
    await asyncio.sleep(0.2)  # give a wrongly-surviving job every chance to run
    assert ran == []


async def test_a_queued_count_cancelled_by_its_caller_never_runs():
    release = threading.Event()
    hogs = await _saturate(release)
    ran: list[str] = []
    task = asyncio.ensure_future(run_counter(lambda: ran.append("late"), queue_wait_s=10))
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    release.set()
    await asyncio.gather(*hogs)
    await asyncio.sleep(0.2)
    assert ran == []


async def test_shutting_the_executor_down_drops_queued_counts_and_a_later_count_restarts_it():
    assert await run_counter(lambda: 1) == 1
    first = _executor.counter_executor()
    _executor.shutdown_counter_executor()
    assert _executor._executor is None
    assert await run_counter(lambda: 2) == 2, "a count after teardown starts a fresh pool"
    assert _executor.counter_executor() is not first
    _executor.shutdown_counter_executor()
    _executor.shutdown_counter_executor()  # idempotent
