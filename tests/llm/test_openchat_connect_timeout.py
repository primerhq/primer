"""D-A3-01: the openchat adapter must honour the configured connect timeout.

A black-holing upstream (accepts the TCP connection, never writes a byte)
used to hold the rate-limiter slot for the openai SDK's default read
timeout (600 s) while the call hung inside a bare ``create()``: with
``max_concurrency=1`` every other session on the provider queued behind
it. The openresponses adapter already bounds that phase with
``Limits.connect_timeout_seconds``; this test pins the same treatment for
openchat: the call fails at the configured bound with the
``connect_timeout`` failure, and the held slot is released with it.

Loopback only (tests/llm's network guard allows it). No mock LLM: the
house ``_SilentServer`` black hole from test_stop_stalled_stream (a real
socket, a real SDK, a server that reads the request and stays silent).
"""

from __future__ import annotations

import asyncio

import pytest
from pydantic import HttpUrl, SecretStr

from primer.llm.openchat import OpenChatLLM
from primer.model.chat import Message, TextPart
from primer.model.except_ import ProviderTimeoutError
from primer.model.provider import (
    Limits,
    LLMProvider,
    LLMProviderType,
    OpenChatConfig,
    OpenChatFlavor,
)

from tests.llm.test_stop_stalled_stream import _SilentServer


def _openchat_on(port: int) -> OpenChatLLM:
    return OpenChatLLM(LLMProvider(
        id="oc-black-hole", provider=LLMProviderType.OPENCHAT,
        config=OpenChatConfig(url=HttpUrl(f"http://127.0.0.1:{port}/v1/"),
                              api_key=SecretStr("sk-test"), flavor=OpenChatFlavor.LMSTUDIO),
        # max_concurrency=1 so a leaked slot shows up as a second call
        # that never reaches the port. request_timeout_seconds=None is
        # legal only because D-A3-04 keeps the total budget on: this test
        # is about the connect phase, and the stall window must not fire
        # before the connect bound does.
        limits=Limits(max_concurrency=1, connect_timeout_seconds=2,
                      request_timeout_seconds=None, total_timeout_seconds=300.0),
    ))


async def _drain(llm: OpenChatLLM) -> None:
    async for _ in llm.stream(model="m", messages=[Message(role="user", parts=[TextPart(text="hi")])]):
        pass


@pytest.mark.asyncio
async def test_connect_timeout_bounds_the_black_hole_and_releases_the_slot() -> None:
    async with _SilentServer() as server:
        llm = _openchat_on(server.port)
        # 1. The black hole accepts TCP and never answers: the call must
        #    fail with the configured connect_timeout at ~2 s, not the SDK
        #    default (600 s). wait_for is the red-state tripwire: with the
        #    bound missing, create() hangs and this test fails at 6 s.
        with pytest.raises(ProviderTimeoutError) as exc_info:
            await asyncio.wait_for(_drain(llm), timeout=6.0)
        assert exc_info.value.code == "connect_timeout"
        # 2. The slot is released by that failure: once the black hole is
        #    closed, the next call must reach the port (and fail there)
        #    within its own budget instead of queueing behind the slot.
        server._server.close()
        try:
            await asyncio.wait_for(_drain(llm), timeout=5.0)
        except TimeoutError:
            pytest.fail("second call still queued: the first failure did not release its slot")
        except Exception:
            pass  # refused at the closed port: the slot was free
        await llm.aclose()
