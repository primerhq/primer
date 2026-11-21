"""Hard-process-bound coverage: a cancellation-resistant cycle must not hang CI."""
import os
from pathlib import Path
import subprocess
import sys

import pytest


# Only actual model bridge code and a cooperative callback; no live transport.
PROBE = r'''
import asyncio
from primer.coordinator.in_memory import InMemoryRateLimiter
from primer.llm.codex.adapter import CodexFoundationLLM
from primer.llm.codex.boundary import BoundaryError, BridgeConfig
from primer.llm.codex.harness import RecordedChannel
from primer.llm.codex.lifecycle import OwnedStream
from primer.llm.codex.request import prepare_request
from primer.model.chat import TextDelta
from tests.llm.codex.test_bridge import bridge, frames, message

async def main():
    mode = MODE
    if mode == "source-self":
        registered = set()
        async def source():
            try:
                yield TextDelta(index=0, text="first")
            finally:
                await stream.aclose()
        stream = OwnedStream(source(), on_close=registered.discard)
        registered.add(stream)
        await anext(stream)
        try:
            await stream.aclose()
        except BoundaryError:
            pass
        else:
            raise AssertionError("recursive source close was not refused")
        assert not registered
    else:
        limiter = InMemoryRateLimiter()
        callback_entered = False
        class RecursiveChannel(RecordedChannel):
            async def aclose(self):
                nonlocal callback_entered
                callback_entered = True
                try:
                    await owner.aclose()
                finally:
                    await super().aclose()
        channel = RecursiveChannel(frames())
        instance, _ = bridge(factory=lambda _: channel, config=BridgeConfig(cleanup_seconds=.02))
        owner = CodexFoundationLLM(instance, provider_id="p", rate_limiter=limiter) if mode == "adapter" else instance
        stream = owner.stream(model="model", messages=[message()]) if mode == "adapter" else instance.stream(prepare_request("model", [message()]))
        assert (await asyncio.wait_for(anext(stream), 1)).type == "text_delta"
        try:
            await owner.aclose()
        except BoundaryError:
            pass
        else:
            raise AssertionError("recursive owner close was not refused")
        assert callback_entered and channel.closed and not instance._active
        if mode == "adapter":
            assert not owner._active
        async with asyncio.timeout(.2):
            async with await limiter.acquire("llm:p", max_concurrency=1):
                pass
    await asyncio.sleep(0)
    assert not [t for t in asyncio.all_tasks() if t is not asyncio.current_task() and not t.done()]
    print("CLOSED_NO_PENDING_TASKS", flush=True)

asyncio.run(main())
'''


@pytest.mark.parametrize("mode", ["bridge", "adapter", "source-self"])
def test_cleanup_callback_cannot_join_its_own_ancestor(mode):
    env = os.environ.copy()
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    # TimeoutExpired uses subprocess.run's hard kill + wait, not cooperative
    # asyncio cancellation. This also bounds regression execution on old code.
    completed = subprocess.run(
        [sys.executable, "-B", "-c", "MODE = " + repr(mode) + "\n" + PROBE],
        cwd=Path(__file__).resolve().parents[3], env=env,
        capture_output=True, text=True, timeout=4,
    )
    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip() == "CLOSED_NO_PENDING_TASKS"


@pytest.mark.asyncio
async def test_external_concurrent_close_still_joins_one_cleanup():
    import asyncio
    from primer.llm.codex.harness import RecordedChannel
    from primer.llm.codex.request import prepare_request
    from tests.llm.codex.test_bridge import bridge, frames, message

    entered, release = asyncio.Event(), asyncio.Event()
    class Held(RecordedChannel):
        closes = 0
        async def aclose(self):
            self.closes += 1
            entered.set()
            await release.wait()
            await super().aclose()
    channel = Held(frames())
    instance, _ = bridge(factory=lambda _: channel)
    stream = instance.stream(prepare_request("model", [message()]))
    await anext(stream)
    first = asyncio.create_task(stream.aclose())
    second = None
    try:
        await asyncio.wait_for(entered.wait(), 1)
        second = asyncio.create_task(instance.aclose())
        await asyncio.sleep(0)
        assert not first.done() and not second.done()
        release.set()
        await asyncio.wait_for(asyncio.gather(first, second), 1)
        assert channel.closed and channel.closes == 1 and not instance._active
    finally:
        release.set()
        await asyncio.gather(first, *([second] if second else []), return_exceptions=True)
