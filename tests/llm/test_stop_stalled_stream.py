"""Stop against a REAL provider SDK whose server has not answered at all.

The loop stops a waiting model by cancelling ``stream.__anext__()`` from an ``interruptible`` scope
(``primer.agent.interrupt``). That is only safe if cancelling a provider stream mid-wait is clean:
the HTTP connection must close, the concurrency slot must be returned, and nothing may be left
behind to fail later (a task nobody retrieves, an anyio cancel scope exited in the wrong task, an
OpenTelemetry context token reset in the wrong context). The unit tests fake the stream; this one
does not: the real adapter, the real SDK, a real socket, and a server that accepts the request and
never writes a byte (what a cold model load looks like).

Loopback only (tests/llm's network guard allows it). ``max_concurrency=1`` so a leaked slot shows up
as a second request that never reaches the server.
"""

from __future__ import annotations

import asyncio
import gc

import pytest
from pydantic import HttpUrl, SecretStr

from primer.agent.interrupt import Interrupted, interruptible
from primer.llm.anthropic import AnthropicLLM
from primer.llm.openchat import OpenChatLLM
from primer.model.chat import Message, TextPart
from primer.model.model_profile import ModelProfileConfig
from primer.model.provider import (
    AnthropicConfig,
    Limits,
    LLMProvider,
    LLMProviderType,
    OpenChatConfig,
    OpenChatFlavor,
)
from primer.model_profile import ResolvedModel


class _SilentServer:
    """Accepts connections, reads each request up to the end of its headers, then says nothing."""

    def __init__(self, reply: bytes = b"") -> None:
        self._reply = reply                    # written once the request has been read; then silence
        self.requests = 0
        self.requested = asyncio.Event()
        self.closed = asyncio.Event()
        self._server: asyncio.Server | None = None
        self.port = 0

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            await reader.readuntil(b"\r\n\r\n")
            self.requests += 1
            self.requested.set()
            if self._reply:
                writer.write(self._reply)
                await writer.drain()
            await reader.read()                # returns b"" once the client closes its end
            self.closed.set()
        except (asyncio.IncompleteReadError, ConnectionError):
            self.closed.set()
        finally:
            writer.close()

    async def __aenter__(self) -> "_SilentServer":
        self._server = await asyncio.start_server(self._handle, "127.0.0.1", 0)
        self.port = self._server.sockets[0].getsockname()[1]
        return self

    async def __aexit__(self, *exc) -> None:
        assert self._server is not None
        self._server.close()
        await self._server.wait_closed()


def _models(name: str) -> list[ResolvedModel]:
    return [ResolvedModel(profile_id="p", provider_id="prov", model_name=name, context_length=8192,
                          config=ModelProfileConfig())]


def _openchat(port: int) -> OpenChatLLM:
    return OpenChatLLM(LLMProvider(
        id="openchat-stop", provider=LLMProviderType.OPENCHAT, models=_models("m"),
        config=OpenChatConfig(url=HttpUrl(f"http://127.0.0.1:{port}/v1/"),
                              api_key=SecretStr("sk-test"), flavor=OpenChatFlavor.LMSTUDIO),
        limits=Limits(max_concurrency=1, request_timeout_seconds=None),
    ))


def _anthropic(port: int, monkeypatch: pytest.MonkeyPatch) -> AnthropicLLM:
    monkeypatch.setenv("ANTHROPIC_BASE_URL", f"http://127.0.0.1:{port}")
    return AnthropicLLM(LLMProvider(
        id="anthropic-stop", provider=LLMProviderType.ANTHROPIC, models=_models("claude-test"),
        config=AnthropicConfig(api_key=SecretStr("sk-ant-test")),
        limits=Limits(max_concurrency=1, request_timeout_seconds=None),
    ))


ADAPTERS = ["openchat", "anthropic"]


def _build(kind: str, port: int, monkeypatch: pytest.MonkeyPatch):
    return _openchat(port) if kind == "openchat" else _anthropic(port, monkeypatch)


def _model_name(kind: str) -> str:
    return "m" if kind == "openchat" else "claude-test"


async def _stop_first_wait(llm, server: _SilentServer, model: str) -> None:
    """Start a stream, wait until the server has the request (so the SDK is parked waiting for a
    response), then interrupt that wait exactly as the agent loop does."""
    stream = llm.stream(model=model, messages=[Message(role="user", parts=[TextPart(text="hi")])])
    it = stream.__aiter__()
    event = asyncio.Event()

    async def stop_once_the_server_has_the_request() -> None:
        await asyncio.wait_for(server.requested.wait(), 5.0)
        await asyncio.sleep(0.05)
        event.set()

    stopper = asyncio.create_task(stop_once_the_server_has_the_request())
    with pytest.raises(Interrupted):
        async with interruptible(event):
            await asyncio.wait_for(it.__anext__(), 10.0)
    await stopper
    await it.aclose()


@pytest.mark.parametrize("kind", ADAPTERS)
async def test_interrupting_a_stream_that_never_answers_is_clean(kind, monkeypatch) -> None:
    loop = asyncio.get_running_loop()
    unretrieved: list[dict] = []
    loop.set_exception_handler(lambda _loop, ctx: unretrieved.append(ctx))
    try:
        async with _SilentServer() as server:
            llm = _build(kind, server.port, monkeypatch)

            await _stop_first_wait(llm, server, _model_name(kind))

            # The connection is closed from the client side (not left to time out).
            await asyncio.wait_for(server.closed.wait(), 3.0)
            # The task is usable again: the cancel the scope delivered was withdrawn.
            await asyncio.sleep(0.01)
            assert asyncio.current_task().cancelling() == 0

            # The concurrency slot (max_concurrency=1) was returned: a second call reaches the server.
            server.requested.clear()
            server.closed.clear()
            first_requests = server.requests
            await _stop_first_wait(llm, server, _model_name(kind))
            assert server.requests == first_requests + 1, (
                "the second call never reached the server: the first call leaked its concurrency slot"
            )
            await asyncio.wait_for(server.closed.wait(), 3.0)

        gc.collect()
        await asyncio.sleep(0.05)
        assert unretrieved == [], f"something was left behind by the cancelled stream: {unretrieved}"
    finally:
        loop.set_exception_handler(None)


_ONE_CHUNK = (
    b"HTTP/1.1 200 OK\r\ncontent-type: text/event-stream\r\ncache-control: no-cache\r\n\r\n"
    b'data: {"id":"c1","object":"chat.completion.chunk","created":1,"model":"m",'
    b'"choices":[{"index":0,"delta":{"role":"assistant","content":"he"},"finish_reason":null}]}\n\n'
)


async def test_interrupting_between_chunks_of_a_stream_that_went_quiet_is_clean() -> None:
    """The between-chunk window, with a real SSE body that sent one chunk and then stalled."""
    loop = asyncio.get_running_loop()
    unretrieved: list[dict] = []
    loop.set_exception_handler(lambda _loop, ctx: unretrieved.append(ctx))
    try:
        async with _SilentServer(_ONE_CHUNK) as server:
            llm = _openchat(server.port)
            stream = llm.stream(model="m", messages=[Message(role="user", parts=[TextPart(text="hi")])])
            it = stream.__aiter__()
            seen = []
            event = asyncio.Event()
            loop.call_later(0.5, event.set)

            with pytest.raises(Interrupted):
                while True:
                    async with interruptible(event):
                        seen.append(await asyncio.wait_for(it.__anext__(), 10.0))
            await it.aclose()

            assert seen, "the first chunk should have been delivered before the stream went quiet"
            await asyncio.wait_for(server.closed.wait(), 3.0)
            await asyncio.sleep(0.01)
            assert asyncio.current_task().cancelling() == 0

        gc.collect()
        await asyncio.sleep(0.05)
        assert unretrieved == [], f"something was left behind by the cancelled stream: {unretrieved}"
    finally:
        loop.set_exception_handler(None)
