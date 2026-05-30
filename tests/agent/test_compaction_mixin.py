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


class TestApplyCompaction:
    async def test_returns_summary_and_new_history(self) -> None:
        from primer.agent.compaction import CompactionStrategy
        from primer.agent.compaction_mixin import apply_compaction
        from primer.model.chat import Done, Message, TextDelta, TextPart, Usage

        async def fake_stream(**_kw):
            yield TextDelta(text="summary text", index=0)
            yield Usage(input_tokens=10, output_tokens=2, cumulative=False)
            yield Done(stop_reason="stop", raw_reason="stop")

        llm = MagicMock()
        llm.stream = MagicMock(side_effect=lambda **kw: fake_stream(**kw))

        strategy = CompactionStrategy()
        history = [
            Message(role="user", parts=[TextPart(text="msg one")]),
            Message(role="assistant", parts=[TextPart(text="reply one")]),
            Message(role="user", parts=[TextPart(text="msg two")]),
            Message(role="assistant", parts=[TextPart(text="reply two")]),
            Message(role="user", parts=[TextPart(text="msg three")]),
            Message(role="assistant", parts=[TextPart(text="reply three")]),
        ]
        result = await apply_compaction(
            llm=llm, strategy=strategy, history=history,
            compaction_prompt="custom prompt",
            model_name="gpt-4o", context_length=8192,
        )
        assert "summary text" in result.summary_text
        assert result.model == "gpt-4o"
        assert result.replaced_from_seq is None
        assert result.replaced_to_seq is None


class TestForceCompact:
    async def test_runs_even_when_under_threshold(self) -> None:
        from primer.agent.compaction import CompactionStrategy
        from primer.agent.compaction_mixin import force_compact
        from primer.model.chat import Done, Message, TextDelta, TextPart

        async def fake_stream(**_kw):
            yield TextDelta(text="forced summary", index=0)
            yield Done(stop_reason="stop", raw_reason="stop")

        llm = MagicMock()
        llm.stream = MagicMock(side_effect=lambda **kw: fake_stream(**kw))

        strategy = CompactionStrategy()
        history = [
            Message(role="user", parts=[TextPart(text="one")]),
            Message(role="assistant", parts=[TextPart(text="two")]),
            Message(role="user", parts=[TextPart(text="three")]),
            Message(role="assistant", parts=[TextPart(text="four")]),
            Message(role="user", parts=[TextPart(text="five")]),
            Message(role="assistant", parts=[TextPart(text="six")]),
        ]
        result = await force_compact(
            llm=llm, strategy=strategy, history=history,
            compaction_prompt="", model_name="gpt-4o", context_length=8192,
        )
        assert "forced summary" in result.summary_text
