"""Tests for primer.agent.compaction."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import pytest

from primer.agent.compaction import (
    DEFAULT_CONTEXT_LIMIT,
    MODEL_CONTEXT_FALLBACK,
    CompactedTurn,
    CompactionStrategy,
    lookup_context_length,
)
from primer.model.agent import Agent, AgentModel
from primer.model.chat import (
    Done,
    Error,
    Message,
    StreamEvent,
    StreamStart,
    TextDelta,
    TextPart,
    ToolCallPart,
    ToolResultPart,
)
from primer.model.except_ import ServerError
from primer.model.provider import LLMModel


# ===========================================================================
# Helpers + fakes
# ===========================================================================


def _u(text: str) -> Message:
    return Message(role="user", parts=[TextPart(text=text)])


def _a(text: str) -> Message:
    return Message(role="assistant", parts=[TextPart(text=text)])


def _t(call_id: str, output: str, *, error: bool = False) -> Message:
    return Message(
        role="tool",
        parts=[ToolResultPart(id=call_id, output=output, error=error)],
    )


def _agent(*, compaction_prompt: list[str] | None = None) -> Agent:
    return Agent(
        id="researcher",
        description="Research agent",
        model=AgentModel(provider_id="openai-1", model_name="gpt-4o-mini"),
        compaction_prompt=list(compaction_prompt or []),
    )


def _model(*, name: str = "gpt-4o-mini", context_length: int = 1000) -> LLMModel:
    return LLMModel(name=name, context_length=context_length)


class _FakeLLM:
    """Stub :class:`LLM` that yields a scripted sequence of events."""

    def __init__(self, *, script: list[StreamEvent]) -> None:
        self._script = script
        self.calls: list[dict[str, Any]] = []

    async def list_models(self):
        return ["gpt-4o-mini"]

    def stream(
        self,
        *,
        model: str,
        messages: list[Message],
        **kwargs: Any,
    ) -> AsyncIterator[StreamEvent]:
        self.calls.append({"model": model, "messages": list(messages), **kwargs})
        return self._stream_impl()

    async def _stream_impl(self) -> AsyncIterator[StreamEvent]:
        for ev in self._script:
            yield ev


# ===========================================================================
# lookup_context_length
# ===========================================================================


class TestLookupContextLength:
    def test_configured_wins(self) -> None:
        assert (
            lookup_context_length(model_name="gpt-4o-mini", configured=42_000)
            == 42_000
        )

    def test_falls_back_to_table(self) -> None:
        assert (
            lookup_context_length(model_name="gpt-4o-mini")
            == MODEL_CONTEXT_FALLBACK["gpt-4o-mini"]
        )

    def test_unknown_model_uses_default(self) -> None:
        assert lookup_context_length(model_name="totally-new-model") == DEFAULT_CONTEXT_LIMIT

    def test_zero_or_negative_configured_falls_back(self) -> None:
        assert (
            lookup_context_length(model_name="gpt-4o-mini", configured=0)
            == MODEL_CONTEXT_FALLBACK["gpt-4o-mini"]
        )
        assert (
            lookup_context_length(model_name="gpt-4o-mini", configured=-5)
            == MODEL_CONTEXT_FALLBACK["gpt-4o-mini"]
        )


# ===========================================================================
# Constructor validation
# ===========================================================================


class TestConstructorValidation:
    def test_invalid_trigger_ratio_rejected(self) -> None:
        with pytest.raises(ValueError):
            CompactionStrategy(trigger_ratio=0)
        with pytest.raises(ValueError):
            CompactionStrategy(trigger_ratio=1.1)
        with pytest.raises(ValueError):
            CompactionStrategy(trigger_ratio=-0.5)

    def test_negative_reserved_rejected(self) -> None:
        with pytest.raises(ValueError):
            CompactionStrategy(reserved_output_tokens=-1)

    def test_negative_tail_turns_rejected(self) -> None:
        with pytest.raises(ValueError):
            CompactionStrategy(tail_turns=-1)


# ===========================================================================
# Token estimator
# ===========================================================================


class TestTokenEstimator:
    def test_text_part(self) -> None:
        msgs = [Message(role="user", parts=[TextPart(text="abcdefgh")])]
        # 8 (envelope) + ceil(8/4)=2 (text) = 10
        assert CompactionStrategy._estimate_tokens(msgs) == 10

    def test_tool_call_part(self) -> None:
        msgs = [
            Message(
                role="assistant",
                parts=[ToolCallPart(id="c", name="grep", arguments={"pattern": "foo"})],
            )
        ]
        # JSON: {"pattern": "foo"} -> 18 chars -> ceil(18/4) = 5
        # Per-part: 50 + 4 (name) + 5 = 59. + 8 envelope = 67.
        assert CompactionStrategy._estimate_tokens(msgs) == 8 + 50 + 4 + 5

    def test_tool_result_part(self) -> None:
        msgs = [_t("c", "hello world!")]  # 12 chars -> ceil(12/4) = 3
        assert CompactionStrategy._estimate_tokens(msgs) == 8 + 20 + 3

    def test_multi_message(self) -> None:
        msgs = [_u("hi"), _a("yo"), _u("ok")]
        # Each: 8 envelope + ceil(2/4)=1 text = 9. 3 messages -> 27.
        assert CompactionStrategy._estimate_tokens(msgs) == 27


# ===========================================================================
# Pruning tier
# ===========================================================================


class TestPruning:
    def test_under_threshold_passthrough(self) -> None:
        msgs = [_u("hi"), _a("yo"), _t("c", "small")]
        new, count = CompactionStrategy._prune_tool_outputs(
            msgs, per_output_threshold=10_000, total_threshold=10_000
        )
        assert count == 0
        assert new == msgs

    def test_over_threshold_replaces_outputs(self) -> None:
        big_output = "x" * 100_000  # ~25k tokens
        msgs = [
            _u("ask"),
            _a("calling tool"),
            Message(role="tool", parts=[ToolResultPart(id="c-1", output=big_output)]),
        ]
        # Single 25k-token output exceeds both per-output AND total
        # thresholds, so it should be pruned.
        new, count = CompactionStrategy._prune_tool_outputs(
            msgs, per_output_threshold=20_000, total_threshold=20_000
        )
        assert count == 1
        assert new[0] == msgs[0]
        assert new[1] == msgs[1]
        result_part = new[2].parts[0]
        assert isinstance(result_part, ToolResultPart)
        assert result_part.id == "c-1"
        assert "omitted" in result_part.output
        assert big_output not in result_part.output

    def test_preserves_error_flag(self) -> None:
        big = "y" * 100_000
        msgs = [
            Message(role="tool", parts=[ToolResultPart(id="c-1", output=big, error=True)])
        ]
        new, count = CompactionStrategy._prune_tool_outputs(
            msgs, per_output_threshold=20_000, total_threshold=10_000
        )
        assert count == 1
        result_part = new[0].parts[0]
        assert isinstance(result_part, ToolResultPart)
        assert result_part.error is True

    def test_only_oversized_get_pruned(self) -> None:
        big = "z" * 100_000
        small = "small"
        msgs = [
            Message(role="tool", parts=[ToolResultPart(id="c-1", output=big)]),
            Message(role="tool", parts=[ToolResultPart(id="c-2", output=small)]),
        ]
        new, count = CompactionStrategy._prune_tool_outputs(
            msgs, per_output_threshold=20_000, total_threshold=10_000
        )
        assert count == 1
        big_part = new[0].parts[0]
        small_part = new[1].parts[0]
        assert isinstance(big_part, ToolResultPart) and big not in big_part.output
        assert isinstance(small_part, ToolResultPart) and small_part.output == small


# ===========================================================================
# maybe_compact
# ===========================================================================


class TestMaybeCompact:
    @pytest.mark.asyncio
    async def test_under_trigger_returns_none(self) -> None:
        strat = CompactionStrategy()
        history = [_u("hi")]
        result = await strat.maybe_compact(
            agent=_agent(),
            llm=_FakeLLM(script=[]),
            model=_model(context_length=128_000),
            history=history,
            new_messages=[_u("hello")],
        )
        assert result is None

    @pytest.mark.asyncio
    async def test_pruning_alone_handles_overflow(self) -> None:
        big = "x" * 200_000  # ~50k tokens of tool output
        history = [
            _u("find slow tests"),
            _a("calling grep"),
            Message(role="tool", parts=[ToolResultPart(id="c-1", output=big)]),
            _a("found something"),
        ]
        strat = CompactionStrategy(
            reserved_output_tokens=0,
            prune_per_output_tokens=1_000,
            prune_total_threshold=1_000,
            tail_turns=4,
        )
        llm = _FakeLLM(script=[])
        # Tight budget so the 50k-token history triggers compaction,
        # but pruning alone (replacing the one big tool output with a
        # placeholder) drops the estimate well below trigger.
        result = await strat.maybe_compact(
            agent=_agent(),
            llm=llm,
            model=_model(context_length=30_000),
            history=history,
            new_messages=[],
        )
        assert result is not None
        assert result.summary_message is None
        assert result.pruned_tool_outputs == 1
        assert result.head_messages_replaced == 0
        assert llm.calls == []

    @pytest.mark.asyncio
    async def test_full_compaction_when_pruning_insufficient(self) -> None:
        # Many large messages, no single oversized tool result.
        big_text = "blah blah blah " * 5000  # ~75k chars per part
        history = [
            _u("g0"),
            _a(big_text),
            _u("g1"),
            _a(big_text),
            _u("g2"),
            _a(big_text),
            _u("g3"),
            _a("recent answer"),
        ]
        strat = CompactionStrategy(
            reserved_output_tokens=0,
            prune_per_output_tokens=1_000_000,  # disable pruning
            prune_total_threshold=1_000_000,
            tail_turns=2,
        )
        llm = _FakeLLM(
            script=[
                StreamStart(model="gpt-4o-mini"),
                TextDelta(text="Goal: find slow test. Decisions: use grep. ", index=0),
                TextDelta(text="Files: tests/test_x.py. Next: edit test.", index=0),
                Done(stop_reason="stop", raw_reason="stop"),
            ]
        )
        result = await strat.maybe_compact(
            agent=_agent(),
            llm=llm,
            model=_model(context_length=50_000),
            history=history,
            new_messages=[],
        )
        assert result is not None
        assert result.summary_message is not None
        assert result.head_messages_replaced > 0
        assert len(llm.calls) == 1
        assert result.new_messages[0] is result.summary_message
        text = result.summary_message.parts[0].text  # type: ignore[union-attr]
        assert "earlier conversation compacted on" in text

    @pytest.mark.asyncio
    async def test_uses_agent_compaction_prompt_when_set(self) -> None:
        history = [_u("g"), _a("answer " * 50000)]
        strat = CompactionStrategy(
            reserved_output_tokens=0,
            prune_per_output_tokens=1_000_000,
            prune_total_threshold=1_000_000,
            tail_turns=0,
        )
        llm = _FakeLLM(
            script=[
                TextDelta(text="custom summary", index=0),
                Done(stop_reason="stop", raw_reason="stop"),
            ]
        )
        await strat.maybe_compact(
            agent=_agent(compaction_prompt=["AGENT_SPECIFIC: keep tool ids"]),
            llm=llm,
            model=_model(context_length=50_000),
            history=history,
            new_messages=[],
        )
        sys_msg = llm.calls[0]["messages"][0]
        assert sys_msg.role == "system"
        sys_text = sys_msg.parts[0].text
        assert "AGENT_SPECIFIC" in sys_text

    @pytest.mark.asyncio
    async def test_empty_summary_raises(self) -> None:
        history = [_u("g"), _a("answer " * 50000)]
        strat = CompactionStrategy(
            reserved_output_tokens=0,
            prune_per_output_tokens=1_000_000,
            prune_total_threshold=1_000_000,
            tail_turns=0,
        )
        llm = _FakeLLM(script=[Done(stop_reason="stop", raw_reason="stop")])
        with pytest.raises(ServerError):
            await strat.maybe_compact(
                agent=_agent(),
                llm=llm,
                model=_model(context_length=50_000),
                history=history,
                new_messages=[],
            )

    @pytest.mark.asyncio
    async def test_fatal_error_event_propagates(self) -> None:
        history = [_u("g"), _a("answer " * 50000)]
        strat = CompactionStrategy(
            reserved_output_tokens=0,
            prune_per_output_tokens=1_000_000,
            prune_total_threshold=1_000_000,
            tail_turns=0,
        )
        llm = _FakeLLM(script=[Error(message="upstream broke", fatal=True)])
        with pytest.raises(ServerError):
            await strat.maybe_compact(
                agent=_agent(),
                llm=llm,
                model=_model(context_length=50_000),
                history=history,
                new_messages=[],
            )

    @pytest.mark.asyncio
    async def test_last_known_input_tokens_overrides_underestimate(self) -> None:
        history = [_u("tiny")]
        strat = CompactionStrategy(reserved_output_tokens=0, tail_turns=0)
        llm = _FakeLLM(
            script=[
                TextDelta(text="forced summary", index=0),
                Done(stop_reason="stop", raw_reason="stop"),
            ]
        )
        # Budget = 1000; trigger = 850. last_known_input_tokens=900 > 850.
        # tail_turns=0 -> head=full, tail=[]; full_compact runs, summary produced.
        result = await strat.maybe_compact(
            agent=_agent(),
            llm=llm,
            model=_model(context_length=1000),
            history=history,
            new_messages=[],
            last_known_input_tokens=900,
        )
        assert result is not None
        assert result.summary_message is not None


# ===========================================================================
# force_compact
# ===========================================================================


class TestForceCompact:
    @pytest.mark.asyncio
    async def test_runs_unconditionally(self) -> None:
        history = [_u("g"), _a("a")]
        strat = CompactionStrategy(tail_turns=0)
        llm = _FakeLLM(
            script=[
                TextDelta(text="forced", index=0),
                Done(stop_reason="stop", raw_reason="stop"),
            ]
        )
        result = await strat.force_compact(
            agent=_agent(),
            llm=llm,
            model=_model(context_length=128_000),
            history=history,
        )
        assert result is not None
        assert result.summary_message is not None
        assert len(llm.calls) == 1


# ===========================================================================
# CompactedTurn
# ===========================================================================


class TestCompactedTurn:
    def test_construction(self) -> None:
        ct = CompactedTurn(
            new_messages=[_u("x")],
            summary_message=None,
            pruned_tool_outputs=0,
            head_messages_replaced=0,
            estimated_tokens_before=100,
            estimated_tokens_after=50,
        )
        assert ct.estimated_tokens_after == 50


class TestDefaultConstants:
    def test_default_trigger_ratio_is_090(self) -> None:
        from primer.agent.compaction import CompactionStrategy
        assert CompactionStrategy.DEFAULT_TRIGGER_RATIO == 0.90

    def test_default_summary_max_tokens_is_4096(self) -> None:
        from primer.agent.compaction import CompactionStrategy
        assert CompactionStrategy.DEFAULT_SUMMARY_MAX_TOKENS == 4096
