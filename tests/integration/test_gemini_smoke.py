"""Gated integration smoke test for the Gemini LLM adapter.

NOT run in normal pytest invocations. Enable by setting GEMINI_API_KEY.
"""

from __future__ import annotations

import os
from typing import cast

import pytest
from pydantic import SecretStr

from primer.llm.gemini import GeminiLLM
from primer.model.chat import (
    Done,
    Message,
    StreamEvent,
    TextDelta,
    TextPart,
)
from primer.model.provider import (
    GoogleConfig,
    Limits,
    LLMModel,
    LLMProvider,
    LLMProviderType,
)


@pytest.mark.skipif(
    not os.environ.get("GEMINI_API_KEY"),
    reason="GEMINI_API_KEY not set",
)
async def test_real_gemini_smoke() -> None:
    provider = LLMProvider(
        id="real-gemini",
        provider=LLMProviderType.GEMINI,
        models=[LLMModel(name="gemini-2.5-flash", context_length=1_000_000)],
        config=GoogleConfig(api_key=SecretStr(os.environ["GEMINI_API_KEY"])),
        limits=Limits(max_concurrency=2),
    )
    llm = GeminiLLM(provider)
    events: list[StreamEvent] = []
    async for event in llm.stream(
        model="gemini-2.5-flash",
        messages=[
            Message(
                role="user",
                parts=[TextPart(text="Reply with the single word 'pong'.")],
            )
        ],
        max_output_tokens=10,
    ):
        events.append(cast(StreamEvent, event))
    assert any(isinstance(e, TextDelta) for e in events)
    assert isinstance(events[-1], Done)
    assert events[-1].stop_reason in {"stop", "max_tokens"}
