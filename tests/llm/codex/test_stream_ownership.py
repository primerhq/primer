"""Public iterator suspension must not outlive its authorization or owner."""
import asyncio
from dataclasses import replace

import pytest

from primer.coordinator.in_memory import InMemoryRateLimiter
from primer.llm.codex.adapter import CodexFoundationLLM
from primer.llm.codex.boundary import BoundaryError, BridgeConfig
from primer.llm.codex.harness import RecordedPolicy
from primer.llm.codex.request import prepare_request
from tests.llm.codex.test_bridge import bridge, frames, message, tool


@pytest.mark.asyncio
@pytest.mark.parametrize("after", ["tool_call_start", "tool_call_end"])
@pytest.mark.parametrize("change", ["close", "suspend", "revision", "expire"])
async def test_terminal_suspension_reauthorizes_before_next_event(after, change):
    policy = RecordedPolicy("model")
    instance, channels = bridge(frames(["files/read", "search"]), authority=policy)
    stream = instance.stream(prepare_request("model", [message()], [tool(), tool("search")]))
    try:
        while (await anext(stream)).type != after:
            pass
        assert channels[0].closed
        if change == "close":
            await instance.aclose()  # Same consumer, idle iterator.
        else:
            patch = {"suspended": True} if change == "suspend" else {"revision": "revoked"} if change == "revision" else {"expires_at": 0}
            policy.snapshot = replace(policy.snapshot, **patch)
        with pytest.raises((BoundaryError, StopAsyncIteration)):
            await anext(stream)
    finally:
        await stream.aclose()
        await instance.aclose()
    assert not instance._active


@pytest.mark.asyncio
@pytest.mark.parametrize("adapter", [False, True])
async def test_close_owns_idle_stream_after_temporary_anext_task(adapter):
    instance, channels = bridge()
    limiter = InMemoryRateLimiter()
    owner = CodexFoundationLLM(instance, provider_id="p", rate_limiter=limiter) if adapter else instance
    stream = owner.stream(model="model", messages=[message()]) if adapter else instance.stream(prepare_request("model", [message()]))
    try:
        task = asyncio.create_task(anext(stream))
        assert (await task).type == "text_delta"
        assert task.done() and not channels[0].closed
        await owner.aclose()
        assert channels[0].closed and not instance._active
        # A leaked adapter scope would block a second slot indefinitely.
        async with asyncio.timeout(.2):
            async with await limiter.acquire("llm:p", max_concurrency=1):
                pass
        with pytest.raises((BoundaryError, StopAsyncIteration)):
            await anext(stream)
    finally:
        await stream.aclose()
        await owner.aclose()


@pytest.mark.asyncio
async def test_cumulative_deadline_survives_temporary_consumer_tasks():
    instance, channels = bridge(config=BridgeConfig(deadline_seconds=.03))
    stream = instance.stream(prepare_request("model", [message()]))
    try:
        assert (await asyncio.wait_for(anext(stream), .2)).type == "text_delta"
        await asyncio.sleep(.05)
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(anext(stream), .2)
        assert channels[0].closed and not instance._active
    finally:
        await stream.aclose()
        await instance.aclose()


@pytest.mark.asyncio
async def test_reentrant_policy_shutdown_refuses_and_unwinds_without_constructing():
    policy = RecordedPolicy("model")
    instance, channels = bridge(authority=policy)
    async def shutdown():
        await instance.aclose()
        return policy.snapshot
    policy.current = shutdown
    with pytest.raises(BoundaryError, match="own advancement"):
        await anext(instance.stream(prepare_request("model", [message()])))
    assert channels == [] and not instance._active
    await instance.aclose()


@pytest.mark.asyncio
async def test_close_of_idle_adapter_drains_cleanup_despite_repeated_cancellation():
    from primer.llm.codex.harness import RecordedChannel
    entered, release = asyncio.Event(), asyncio.Event()
    class HeldClose(RecordedChannel):
        async def aclose(self):
            entered.set()
            await release.wait()
            await super().aclose()
    channel = HeldClose(frames())
    instance, _ = bridge(factory=lambda _: channel)
    limiter = InMemoryRateLimiter()
    owner = CodexFoundationLLM(instance, provider_id="p", rate_limiter=limiter)
    stream = owner.stream(model="model", messages=[message()])
    assert (await asyncio.wait_for(anext(stream), .5)).type == "text_delta"
    closing = asyncio.create_task(owner.aclose())
    try:
        await asyncio.wait_for(entered.wait(), .5)
        closing.cancel()
        await asyncio.sleep(0)
        closing.cancel()
        await asyncio.sleep(0)
        assert not closing.done()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await closing
        assert channel.closed and not instance._active and not owner._active
        async with asyncio.timeout(.2):
            async with await limiter.acquire("llm:p", max_concurrency=1):
                pass
    finally:
        release.set()
        await asyncio.gather(closing, return_exceptions=True)
        await owner.aclose()
