"""ChatTurnRunner with a cancel_event: breaks between LLM events,
persists a 'cancelled' row, and exits cleanly."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import datetime, timezone
from typing import Any

import pytest

from matrix.chat.executor import ChatTurnRunner
from matrix.model.agent import Agent, AgentModel
from matrix.model.chat import Done, Message, StreamEvent, TextDelta
from matrix.model.chats import Chat, ChatMessage
from matrix.model.provider import LLMModel


class _NeverEndingStream:
    """Yields TextDelta forever (until cancelled) so the test can
    pause the runner with a known number of tokens in."""

    def __init__(self) -> None:
        self.yielded = 0

    async def __call__(self) -> AsyncIterator[StreamEvent]:
        while True:
            self.yielded += 1
            yield TextDelta(text=f"tok-{self.yielded} ", index=0)
            await asyncio.sleep(0.01)


class _FakeLLM:
    def __init__(self, stream_factory):
        self._stream_factory = stream_factory
        self.calls: list[dict[str, Any]] = []

    async def list_models(self):
        return ["m"]

    def stream(self, *, model, messages, **kwargs):
        self.calls.append({"model": model, "messages": list(messages), **kwargs})
        return self._stream_factory()

    async def aclose(self):
        return None


class _NullToolManager:
    async def list_tools(self, *, principal=None):
        return []
    async def execute(self, call, *, principal=None, bypass_approval=False):
        raise NotImplementedError


@pytest.mark.asyncio
async def test_cancel_event_set_breaks_stream_and_persists_cancelled(
    fake_storage_provider,
):
    agent = Agent(
        id="ag", description="x",
        model=AgentModel(provider_id="p", model_name="m"),
    )
    chat = Chat(id="c1", agent_id="ag", created_at=datetime.now(timezone.utc))
    chat_store = fake_storage_provider.get_storage(Chat)
    msg_store = fake_storage_provider.get_storage(ChatMessage)
    await chat_store.create(chat)

    cancel_event = asyncio.Event()
    counter = _NeverEndingStream()

    runner = ChatTurnRunner(
        agent=agent,
        llm=_FakeLLM(counter),
        llm_model=LLMModel(name="m", context_length=4096),
        tool_manager=_NullToolManager(),
        chat_storage=chat_store,
        message_storage=msg_store,
        cancel_event=cancel_event,
    )

    async def _set_cancel_after_a_few_tokens():
        await asyncio.sleep(0.05)
        cancel_event.set()

    asyncio.create_task(_set_cancel_after_a_few_tokens())

    rows: list[ChatMessage] = []
    async for r in runner.run_turn(chat, "go"):
        rows.append(r)
    kinds = [r.kind for r in rows]
    assert kinds[0] == "user_message"
    assert "assistant_token" in kinds
    assert kinds[-1] == "cancelled"


@pytest.mark.asyncio
async def test_no_cancel_event_legacy_behaviour(fake_storage_provider):
    """When cancel_event is not provided, the runner ignores cancellation
    entirely — existing callers see no behaviour change."""
    agent = Agent(
        id="ag", description="x",
        model=AgentModel(provider_id="p", model_name="m"),
    )
    chat = Chat(id="c2", agent_id="ag", created_at=datetime.now(timezone.utc))
    chat_store = fake_storage_provider.get_storage(Chat)
    msg_store = fake_storage_provider.get_storage(ChatMessage)
    await chat_store.create(chat)

    async def _short_stream():
        yield TextDelta(text="hello", index=0)
        yield Done(stop_reason="stop", raw_reason="stop")

    runner = ChatTurnRunner(
        agent=agent,
        llm=_FakeLLM(_short_stream),
        llm_model=LLMModel(name="m", context_length=4096),
        tool_manager=_NullToolManager(),
        chat_storage=chat_store,
        message_storage=msg_store,
        # no cancel_event
    )
    rows: list[ChatMessage] = []
    async for r in runner.run_turn(chat, "go"):
        rows.append(r)
    kinds = [r.kind for r in rows]
    assert "cancelled" not in kinds
    assert kinds[-1] == "done"
