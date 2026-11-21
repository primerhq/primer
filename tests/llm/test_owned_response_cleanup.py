"""Real adapter ownership on normal, early-stop and cancelled SDK responses."""
import asyncio
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

import pytest

from primer.llm.anthropic import AnthropicLLM
from primer.llm.openchat import OpenChatLLM
from primer.memory import reflection_input as ri
from primer.model.chat import Message, TextDelta, TextPart
from tests.llm.test_anthropic import _make_provider as anthropic_provider, _ok_events
from tests.llm.test_openchat import _make_provider as openchat_provider
from tests.memory.test_reflection_input import MODEL, SCHEMA, judge


@pytest.mark.parametrize("provider", ["anthropic", "openchat"])
@pytest.mark.parametrize("exit_kind", ["normal", "refusal", "cancel", "consumer_close"])
@pytest.mark.parametrize("close_fails", [False, True])
async def test_adapter_joins_nonexhausted_sdk_close(provider, exit_kind, close_fails):
    text = "x" * (ri.MAX_OUTPUT_BYTES + 1) if exit_kind == "refusal" else '{"run_succeeded":true,"deltas":[]}'
    if provider == "anthropic":
        events = _ok_events()
        events[2].delta.text = text
        if exit_kind != "normal":
            events = events[:3]
        llm = AnthropicLLM(anthropic_provider())
    else:
        def chunk(content, finish):
            return NS(id="offline", model="m", usage=None, choices=[
                NS(index=0, delta=NS(role="assistant", content=content, tool_calls=None), finish_reason=finish),
            ])
        events = [chunk(text, None)]
        if exit_kind == "normal":
            events.append(chunk(None, "stop"))
        llm = OpenChatLLM(openchat_provider())

    blocked, closing, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
    class Response:
        def __init__(self):
            self.offset = self.close_calls = 0
            self.closed = self.exhausted = False
        def __aiter__(self):
            return self
        async def __anext__(self):
            if self.offset < len(events):
                event = events[self.offset]
                self.offset += 1
                return event
            if exit_kind == "normal":
                self.exhausted = True
                raise StopAsyncIteration
            blocked.set()
            await asyncio.Event().wait()
        async def close(self):
            self.close_calls += 1
            closing.set()
            await release.wait()
            self.closed = True
            if close_fails:
                raise RuntimeError("isolated response cleanup failure")
    response = Response()
    create = AsyncMock(return_value=response)
    llm._client = (NS(messages=NS(create=create)) if provider == "anthropic"
                   else NS(chat=NS(completions=NS(create=create))))
    stream = None
    if exit_kind == "consumer_close":
        stream = llm.stream(model=MODEL.model_name, messages=[Message(role="user", parts=[TextPart(text="source")])],
                            response_format=SCHEMA, max_output_tokens=4096)
        async for event in stream:
            if isinstance(event, TextDelta):
                break
        running = asyncio.create_task(stream.aclose())
    else:
        running = asyncio.create_task(judge(llm))
    try:
        async with asyncio.timeout(5):
            if exit_kind == "cancel":
                await blocked.wait()
                running.cancel()
            await closing.wait()
            assert not running.done() and not response.closed
            assert response.exhausted == (exit_kind == "normal")
            if exit_kind == "cancel":
                running.cancel()
                await asyncio.sleep(0)
                assert not running.done()
            release.set()
            if exit_kind == "cancel":
                with pytest.raises(asyncio.CancelledError):
                    await running
            elif exit_kind == "refusal":
                with pytest.raises(ri.InputRefused, match="compaction_output_limit"):
                    await running
            elif exit_kind == "normal":
                assert (await running)["deltas"] == []
            else:
                await running
        assert response.closed and response.close_calls == 1 and create.call_count == 1
    finally:
        release.set()
        running.cancel()
        await asyncio.gather(running, return_exceptions=True)
        if stream is not None:
            await stream.aclose()
