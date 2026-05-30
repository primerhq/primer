"""Unit tests for the shared compaction mixin functions."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from primer.agent.compaction_mixin import (
    DEFAULT_RESERVED_OUTPUT_TOKENS,
    should_compact,
)
from primer.model.chat import Message, TextPart


class TestShouldCompact:
    async def test_below_threshold_returns_false(self) -> None:
        llm = MagicMock()
        llm.count_tokens = AsyncMock(return_value=1000)
        msgs = [Message(role="user", parts=[TextPart(text="hi")])]
        triggered, count = await should_compact(
            llm=llm, model_name="gpt-4o", context_length=10_000,
            history=msgs, tools=None,
            trigger_ratio=0.90, reserved_output_tokens=2000,
        )
        assert triggered is False
        assert count == 1000

    async def test_at_threshold_returns_true(self) -> None:
        llm = MagicMock()
        llm.count_tokens = AsyncMock(return_value=7300)
        msgs = [Message(role="user", parts=[TextPart(text="hi")])]
        triggered, count = await should_compact(
            llm=llm, model_name="gpt-4o", context_length=10_000,
            history=msgs, tools=None,
            trigger_ratio=0.90, reserved_output_tokens=2000,
        )
        assert triggered is True
        assert count == 7300

    async def test_uses_defaults_when_not_specified(self) -> None:
        assert DEFAULT_RESERVED_OUTPUT_TOKENS == 2000
        llm = MagicMock()
        llm.count_tokens = AsyncMock(return_value=0)
        msgs: list[Message] = []
        triggered, count = await should_compact(
            llm=llm, model_name="gpt-4o", context_length=100_000,
            history=msgs, tools=None,
        )
        assert triggered is False
        assert count == 0
