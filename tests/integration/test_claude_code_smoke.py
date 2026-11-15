"""Gated integration smoke for the claude_code adapter (the claude_code plan, task 10).

Skipped unless ``PRIMER_TEST_CLAUDE_CODE=1``: enabling it spends the
operator's REAL Claude plan (one short haiku call on the CLI logged in on
this host), so it is only ever CONFIRMED TO SKIP -- never run with the
variable set.
"""

from __future__ import annotations

import os
from typing import cast

import pytest

from primer.llm.claude_code.adapter import ClaudeCodeLLM
from primer.model.chat import (
    Done,
    Message,
    StreamEvent,
    TextDelta,
    TextPart,
    Usage,
)
from primer.model.provider import (
    ClaudeCodeConfig,
    Limits,
    LLMProvider,
    LLMProviderType,
)


@pytest.mark.skipif(
    os.environ.get("PRIMER_TEST_CLAUDE_CODE") != "1",
    reason="PRIMER_TEST_CLAUDE_CODE not set (the smoke spends the operator's real Claude plan)",
)
async def test_real_claude_code_smoke() -> None:
    provider = LLMProvider(
        id="cc-smoke",
        provider=LLMProviderType.CLAUDE_CODE,
        config=ClaudeCodeConfig(binary_path="claude"),
        limits=Limits(max_concurrency=1),
    )
    llm = ClaudeCodeLLM(provider)
    events: list[StreamEvent] = []
    async for event in llm.stream(
        model="haiku",
        messages=[
            Message(role="user", parts=[TextPart(text="Reply with exactly: OK")])
        ],
    ):
        events.append(cast(StreamEvent, event))
    text = "".join(e.text for e in events if isinstance(e, TextDelta))
    assert "OK" in text, f"no TextDelta carrying 'OK' in: {events!r}"
    assert any(isinstance(e, Usage) for e in events), f"no Usage in: {events!r}"
    assert isinstance(events[-1], Done), f"the stream did not end with Done: {events!r}"
