"""The three OpenAI-family adapters count off the event loop.

A wrapper-level test would only prove the wrapper. These patch the real
encoder the real adapters call and assert the loop keeps running while a
slow encode is in flight: reverting any adapter to a synchronous call fails
its case.
"""

from __future__ import annotations

import asyncio
import time

import pytest

from primer.llm._tokenizer import _tiktoken_offline
from primer.llm.openchat import OpenChatLLM
from primer.llm.openresponses import OpenResponsesLLM
from primer.llm.openrouter import OpenRouterLLM
from primer.model.chat import Message, TextPart
from tests.llm.test_openchat import _make_provider as chat_provider
from tests.llm.test_openresponses import _make_provider as responses_provider
from tests.llm.test_openrouter import _make_provider as router_provider


class _SlowEncoding:
    def encode_ordinary(self, text: str) -> list[int]:
        time.sleep(0.6)
        return [0] * len(text)


CASES = [
    pytest.param(lambda: OpenChatLLM(chat_provider(models=["gpt-4o"])), "gpt-4o", id="openchat"),
    pytest.param(
        lambda: OpenResponsesLLM(responses_provider(models=["gpt-4o"])), "gpt-4o", id="openresponses",
    ),
    pytest.param(
        lambda: OpenRouterLLM(router_provider(models=["openai/gpt-4o"])), "openai/gpt-4o", id="openrouter",
    ),
]


@pytest.mark.parametrize(("make", "model"), CASES)
async def test_count_tokens_does_not_block_the_event_loop(monkeypatch, make, model):
    monkeypatch.setattr(
        _tiktoken_offline, "load_encoding", lambda name, **_k: _SlowEncoding(),
    )
    llm = make()
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
    await asyncio.sleep(0.05)
    msgs = [Message(role="user", parts=[TextPart(text="hello world")])]
    n = await llm.count_tokens(model=model, messages=msgs, tools=None)
    stop.set()
    await task
    assert n > 0
    assert len(gaps) > 10
    assert max(gaps) < 0.3, f"{make.__name__ if hasattr(make, '__name__') else model}: loop stalled {max(gaps):.3f}s"
