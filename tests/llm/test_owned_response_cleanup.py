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


@pytest.mark.parametrize("provider", ["anthropic", "openchat"])
@pytest.mark.parametrize("consumer", ["direct", "bounded"])
@pytest.mark.parametrize("ending", ["complete", "cancel", "iteration_error", "close_error", "cancel_close_error"])
async def test_locked_sdk_automatic_close_joins_transport_before_permit_handoff(provider, consumer, ending):
    """Real SDK + HTTPX response: is_closed becomes true before transport close.

    All HTTP is intercepted in memory. The first stream reaches SDK/HTTPX
    automatic disposal, not Primer's explicit finally, before cancellation.
    """
    import json

    import httpx2
    from anthropic import AsyncAnthropic
    from openai import AsyncOpenAI

    from primer.coordinator.in_memory import InMemoryRateLimiter
    from primer.model.chat import Done, Error
    from primer.model.except_ import ServerError

    closing, release, queued = asyncio.Event(), asyncio.Event(), asyncio.Event()
    class Limiter(InMemoryRateLimiter):
        calls = 0
        async def acquire(self, key, *, max_concurrency):
            self.calls += 1
            if self.calls == 2:
                queued.set()
            return await super().acquire(key, max_concurrency=max_concurrency)

    def wire(*, broken):
        if broken:
            return b'event: error\ndata: {"type":"error","error":{"type":"api_error","message":"offline failure"}}\n\n'
        text = '{"run_succeeded":true,"deltas":[]}'
        if provider == "anthropic":
            events = [
                {"type": "message_start", "message": {"id": "msg_offline", "type": "message", "role": "assistant",
                 "model": "offline", "content": [], "stop_reason": None, "stop_sequence": None,
                 "usage": {"input_tokens": 5, "output_tokens": 0}}},
                {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
                {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": text}},
                {"type": "content_block_stop", "index": 0},
                {"type": "message_delta", "delta": {"stop_reason": "end_turn", "stop_sequence": None}, "usage": {"output_tokens": 3}},
                {"type": "message_stop"},
            ]
            return "".join(f"event: {event['type']}\ndata: {json.dumps(event)}\n\n" for event in events).encode()
        events = [{"id": "offline", "object": "chat.completion.chunk", "created": 0, "model": "offline",
                   "choices": [{"index": 0, "delta": {"role": "assistant", "content": text}, "finish_reason": "stop"}]}]
        return ("".join(f"data: {json.dumps(event)}\n\n" for event in events) + "data: [DONE]\n\n").encode()

    class TransportStream(httpx2.AsyncByteStream):
        def __init__(self, first):
            self.first = first
            self.close_calls = 0
            self.finished = self.interrupted = False
        async def __aiter__(self):
            yield wire(broken=self.first and ending == "iteration_error")
        async def aclose(self):
            self.close_calls += 1
            try:
                if self.first:
                    closing.set()
                    await release.wait()
                self.finished = True
                if self.first and ending in {"close_error", "cancel_close_error"}:
                    raise RuntimeError("offline transport cleanup failure")
            except asyncio.CancelledError:
                self.interrupted = True
                raise

    transports, responses = [], []
    async def handler(request):
        assert request.url.host == "offline.invalid"
        if transports:
            assert transports[0].finished  # permit cannot be released early
        transport = TransportStream(first=not transports)
        transports.append(transport)
        response = httpx2.Response(200, headers={"content-type": "text/event-stream"}, stream=transport, request=request)
        responses.append(response)
        return response

    http = httpx2.AsyncClient(transport=httpx2.MockTransport(handler))
    limiter = Limiter()
    if provider == "anthropic":
        llm = AnthropicLLM(anthropic_provider(models=["offline"], max_concurrency=1), rate_limiter=limiter)
        client = AsyncAnthropic(api_key="offline-test", base_url="https://offline.invalid", http_client=http, max_retries=0)
    else:
        llm = OpenChatLLM(openchat_provider(models=["offline"], max_concurrency=1), rate_limiter=limiter)
        client = AsyncOpenAI(api_key="offline-test", base_url="https://offline.invalid", http_client=http, max_retries=0)
    llm._client = client
    async def consume():
        if consumer == "bounded":
            return await judge(llm)
        return [event async for event in llm.stream(
            model="offline", messages=[Message(role="user", parts=[TextPart(text="source")])],
            response_format=SCHEMA, max_output_tokens=4096,
        )]

    first = asyncio.create_task(consume())
    second = None
    try:
        async with asyncio.timeout(5):
            await closing.wait()
            assert responses[0].is_closed and not transports[0].finished
            second = asyncio.create_task(consume())
            await queued.wait()
            if ending.startswith("cancel"):
                first.cancel()
                await asyncio.sleep(0)
                first.cancel()
                await asyncio.sleep(0)
            assert not first.done() and not second.done()
            assert len(transports) == 1 and not transports[0].interrupted
            release.set()
            if ending.startswith("cancel"):
                with pytest.raises(asyncio.CancelledError):
                    await first
            elif ending in {"iteration_error", "close_error"} and consumer == "bounded":
                with pytest.raises(ServerError):
                    await first
            else:
                result = await first
                if consumer == "bounded":
                    assert result["deltas"] == []
                elif ending in {"iteration_error", "close_error"}:
                    assert any(isinstance(event, Error) for event in result)
                else:
                    assert any(isinstance(event, Done) and event.stop_reason == "stop" for event in result)
            result = await second
            if consumer == "bounded":
                assert result["deltas"] == []
            else:
                assert any(isinstance(event, Done) and event.stop_reason == "stop" for event in result)
        assert len(transports) == 2
        assert all(t.finished and not t.interrupted and t.close_calls == 1 for t in transports)
    finally:
        release.set()
        tasks = [task for task in (first, second) if task is not None]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await client.close()


async def test_nested_close_preserves_only_the_cancelled_callers_outcome():
    """SDK finally re-entry cannot replace cancellation with cached close error."""
    from primer.llm._timeout import _protect_sdk_response_close
    closing, release = asyncio.Event(), asyncio.Event()
    calls = 0
    async def close():
        nonlocal calls
        calls += 1
        closing.set()
        await release.wait()
        raise RuntimeError("offline close failure")
    response = NS(aclose=close)
    _protect_sdk_response_close(NS(response=response))
    async def nested():
        try:
            await response.aclose()
        finally:
            await response.aclose()  # SDK finally following HTTPX's finally
    caller = asyncio.create_task(nested())
    try:
        async with asyncio.timeout(5):
            await closing.wait()
            caller.cancel()
            await asyncio.sleep(0)
            assert not caller.done()
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await caller
            # A different, uncancelled caller must still see the close failure.
            with pytest.raises(RuntimeError, match="offline close failure"):
                await response.aclose()
        assert calls == 1
    finally:
        release.set()
        caller.cancel()
        await asyncio.gather(caller, return_exceptions=True)
