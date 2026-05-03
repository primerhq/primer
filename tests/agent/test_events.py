"""Tests for matrix.agent.events."""

from __future__ import annotations

import pytest
from pydantic import TypeAdapter, ValidationError

from matrix.agent.events import (
    AgentEventSubscriber,
    Subscription,
    _ExecutorToolResult,
)
from matrix.agent.prompts import DEFAULT_COMPACTION_PROMPT
from matrix.model.chat import ExtendedEvent, StreamEvent


# ---- _ExecutorToolResult ---------------------------------------------------


class TestExecutorToolResult:
    def test_construction(self) -> None:
        ev = _ExecutorToolResult(call_id="c-1", output="42")
        assert ev.type == "executor_tool_result"
        assert ev.call_id == "c-1"
        assert ev.output == "42"
        assert ev.error is False

    def test_error_flag(self) -> None:
        ev = _ExecutorToolResult(call_id="c", output="boom", error=True)
        assert ev.error is True

    def test_empty_call_id_rejected(self) -> None:
        with pytest.raises(ValidationError):
            _ExecutorToolResult(call_id="", output="x")

    def test_round_trip_through_extended_event(self) -> None:
        wrapped = ExtendedEvent(extended=_ExecutorToolResult(call_id="c", output="x"))
        adapter: TypeAdapter[StreamEvent] = TypeAdapter(StreamEvent)
        parsed = adapter.validate_json(wrapped.model_dump_json())
        assert isinstance(parsed, ExtendedEvent)
        assert isinstance(parsed.extended, _ExecutorToolResult)
        assert parsed.extended.call_id == "c"


# ---- Subscription ----------------------------------------------------------


class _FakeExec:
    """Minimal fake satisfying the executor protocol Subscription forwards to."""

    def __init__(self) -> None:
        self.unsubscribed: list[str] = []

    async def unsubscribe(self, sub: Subscription) -> None:
        self.unsubscribed.append(sub.subscription_id)


class TestSubscription:
    def test_construction(self) -> None:
        ex = _FakeExec()
        sub = Subscription(subscription_id="sub-1", _executor=ex)
        assert sub.subscription_id == "sub-1"

    @pytest.mark.asyncio
    async def test_unsubscribe_calls_executor(self) -> None:
        ex = _FakeExec()
        sub = Subscription(subscription_id="sub-x", _executor=ex)
        await sub.unsubscribe()
        assert ex.unsubscribed == ["sub-x"]

    def test_empty_subscription_id_rejected(self) -> None:
        ex = _FakeExec()
        with pytest.raises(ValidationError):
            Subscription(subscription_id="", _executor=ex)


# ---- AgentEventSubscriber Protocol -----------------------------------------


class TestAgentEventSubscriber:
    @pytest.mark.asyncio
    async def test_protocol_satisfaction(self) -> None:
        events_seen: list[StreamEvent] = []

        class _Capture:
            async def on_event(self, event: StreamEvent) -> None:
                events_seen.append(event)

        sub: AgentEventSubscriber = _Capture()
        ev = ExtendedEvent(extended=_ExecutorToolResult(call_id="c", output="o"))
        await sub.on_event(ev)
        assert events_seen == [ev]


# ---- Sanity: prompts module still importable -------------------------------


def test_default_compaction_prompt_nonempty() -> None:
    assert isinstance(DEFAULT_COMPACTION_PROMPT, str)
    assert len(DEFAULT_COMPACTION_PROMPT) > 100
