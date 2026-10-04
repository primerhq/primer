"""``interruptible``: stop waiting on an await when an Event is set, in the SAME task.

Stop used to be read only between a stream's events, so a model that had not produced its
first token (a cold model load can take minutes) could not be stopped. The scope wraps ONE
await, never a ``yield``: ``asyncio.timeout`` cancels the task that entered it, and a scope
left open across a ``yield`` would cancel whatever the consumer happens to be awaiting.

It is built on ``asyncio.timeout`` for the one property that matters here: CPython's
``uncancel()`` accounting. A hard Cancel (``task.cancel()`` from the worker pool) that races
the Stop is never swallowed, and a provider stall ``TimeoutError`` is never mistaken for a Stop.
"""

from __future__ import annotations

import asyncio

import pytest

from primer.agent.interrupt import Interrupted, interruptible


async def _never() -> None:
    await asyncio.Event().wait()


async def test_it_interrupts_a_wait_that_never_returns_when_the_event_is_set():
    event = asyncio.Event()
    asyncio.get_running_loop().call_later(0.05, event.set)

    async def wait_forever() -> None:
        async with interruptible(event):
            await _never()

    with pytest.raises(Interrupted):
        await asyncio.wait_for(wait_forever(), 2.0)


async def test_an_event_that_is_already_set_stops_before_the_body_runs():
    event = asyncio.Event()
    event.set()
    ran = []

    with pytest.raises(Interrupted):
        async with interruptible(event):
            ran.append("body")

    assert ran == []


async def test_no_event_is_a_transparent_no_op():
    async with interruptible(None):
        await asyncio.sleep(0)
        value = 7

    assert value == 7


async def test_an_event_that_never_fires_leaves_the_body_alone_and_no_task_behind():
    event = asyncio.Event()
    before = len(asyncio.all_tasks())

    async with interruptible(event):
        await asyncio.sleep(0.01)

    await asyncio.sleep(0)
    assert len(asyncio.all_tasks()) == before, "the scope left its watcher task running"


async def test_the_task_is_usable_after_an_interrupt():
    """The cancel the scope delivered is withdrawn (uncancel), so the caller can keep awaiting:
    the loop closes the stream and returns cleanly after an Interrupted."""
    event = asyncio.Event()
    asyncio.get_running_loop().call_later(0.02, event.set)

    with pytest.raises(Interrupted):
        async with interruptible(event):
            await _never()

    await asyncio.sleep(0.01)                      # would raise CancelledError if still cancelling
    assert asyncio.current_task().cancelling() == 0


async def test_a_provider_stall_timeout_inside_the_scope_is_not_a_stop():
    event = asyncio.Event()

    with pytest.raises(TimeoutError) as info:
        async with interruptible(event):
            raise TimeoutError("no event within the stall window")

    assert not isinstance(info.value, Interrupted)


async def test_stop_iteration_from_the_body_passes_through_unchanged():
    """The loop drives ``stream.__anext__()`` inside the scope; the end of the stream must
    surface as StopAsyncIteration, not be swallowed or turned into a RuntimeError."""

    async def empty():
        return
        yield

    it = empty().__aiter__()
    with pytest.raises(StopAsyncIteration):
        async with interruptible(asyncio.Event()):
            await it.__anext__()


async def test_a_hard_cancel_racing_the_stop_is_never_swallowed():
    """Both land in the same tick: the task must end cancelled, not turn into a quiet Interrupted."""
    event = asyncio.Event()
    entered = asyncio.Event()

    async def body() -> None:
        async with interruptible(event):
            entered.set()
            await _never()

    task = asyncio.create_task(body())
    await entered.wait()
    event.set()
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, 2.0)
    assert task.cancelled()


async def test_a_hard_cancel_alone_passes_through_the_scope():
    event = asyncio.Event()
    entered = asyncio.Event()

    async def body() -> None:
        async with interruptible(event):
            entered.set()
            await _never()

    task = asyncio.create_task(body())
    await entered.wait()
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, 2.0)


async def test_the_scope_can_be_entered_again_after_an_event_that_fired_late():
    """An event that fires just after the body finished must not poison the next wait: the next
    entry sees it already set and stops at once, instead of hanging on a stale watcher."""
    event = asyncio.Event()

    async with interruptible(event):
        await asyncio.sleep(0)
    event.set()

    with pytest.raises(Interrupted):
        async with interruptible(event):
            await _never()
