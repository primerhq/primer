"""Gated integration smoke test for the Anthropic adapter.

Not run in normal pytest invocations — gated on ``ANTHROPIC_API_KEY``.
Enable by exporting the key and invoking pytest as usual.
"""

from __future__ import annotations

import os
from typing import cast

import pytest
from pydantic import SecretStr

from primer.llm.anthropic import AnthropicLLM
from primer.model.chat import (
    Done,
    Message,
    StreamEvent,
    TextDelta,
    TextPart,
)
from primer.model.provider import (
    AnthropicConfig,
    Limits,
    LLMModel,
    LLMProvider,
    LLMProviderType,
)


@pytest.mark.skipif(
    not os.environ.get("ANTHROPIC_API_KEY"),
    reason="ANTHROPIC_API_KEY not set",
)
async def test_real_anthropic_smoke() -> None:
    provider = LLMProvider(
        id="real-anthropic",
        provider=LLMProviderType.ANTHROPIC,
        models=[LLMModel(name="claude-sonnet-4-5", context_length=200_000)],
        config=AnthropicConfig(
            api_key=SecretStr(os.environ["ANTHROPIC_API_KEY"]),
        ),
        limits=Limits(max_concurrency=2),
    )
    llm = AnthropicLLM(provider)
    events: list[StreamEvent] = []
    async for event in llm.stream(
        model="claude-sonnet-4-5",
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
