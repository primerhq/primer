"""When a user_message lands while a turn is running, the dispatcher
must pick it up on the next iteration of its drain loop.

The motivating case is compaction: the first turn may run pre-turn
compaction (slow LLM round-trip) while the user types and hits send a
second time. The second user_message is appended to ``chat_messages``
via the WS receive loop, but the worker is mid-stream on the first
turn and doesn't poll storage until that turn finishes.

This test pins the contract that the dispatcher's post-turn drain
loop re-queries storage and immediately runs a second turn for any
queued user_message — without releasing the chat lease back to
``idle`` between the two.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import datetime, timezone
from typing import Any

import pytest
from pydantic import SecretStr

from primer.bus.in_memory import InMemoryEventBus
from primer.chat.dispatch import ChatDispatchDeps, run_one_chat_turn
from primer.chat.tick_router import ChatTickRouter
from primer.model.agent import Agent, AgentModel
from primer.model.chat import Done, StreamEvent, TextDelta
from primer.model.chats import Chat, ChatMessage
from primer.model.provider import (
    AnthropicConfig, Limits, LLMModel, LLMProvider, LLMProviderType,
)


class _MidStreamSeederLLM:
    """Fake LLM that — during the FIRST turn's stream — appends a second
    queued user_message to storage to simulate the user hitting send
    while the worker is busy on turn 1.

    Subsequent turns yield a plain ``TextDelta`` + ``Done`` so the
    drain loop terminates cleanly.
    """

    def __init__(self, *, on_first_stream) -> None:
        self._on_first_stream = on_first_stream
        self._calls_seen = 0
        self.calls: list[dict[str, Any]] = []

    async def list_models(self):
        return ["m"]

    def stream(self, *, model, messages, **kwargs):
        self._calls_seen += 1
        self.calls.append({"model": model, "messages": list(messages)})
        return self._stream_impl(first=self._calls_seen == 1)

    async def _stream_impl(self, *, first: bool) -> AsyncIterator[StreamEvent]:
        # Emit a single token before the side effect so the runner has
        # had time to enter its stream loop, matching the real ordering.
        yield TextDelta(text="hi", index=0)
        if first:
            await self._on_first_stream()
        yield Done(stop_reason="stop", raw_reason="stop")

    async def aclose(self) -> None:
        return None


@pytest.mark.asyncio
async def test_user_message_queued_mid_turn_runs_a_second_turn(
    fake_storage_provider, fake_provider_registry,
) -> None:
    """Seed one user_message; while its turn streams, persist a second
    user_message directly to storage; assert the dispatcher's drain
    loop picks it up and calls ``llm.stream`` a second time, and the
    chat ends with ``turn_status='idle'``."""
    # --- Seed provider + agent rows.
    await fake_storage_provider.get_storage(LLMProvider).create(
        LLMProvider(
            id="p", provider=LLMProviderType.ANTHROPIC,
            models=[LLMModel(name="m", context_length=8192)],
            config=AnthropicConfig(api_key=SecretStr("test")),
            limits=Limits(max_concurrency=1),
        ),
    )
    await fake_storage_provider.get_storage(Agent).create(Agent(
        id="ag", description="x",
        model=AgentModel(provider_id="p", model_name="m"),
    ))

    chats = fake_storage_provider.get_storage(Chat)
    msgs = fake_storage_provider.get_storage(ChatMessage)

    # --- Seed the chat row + first user_message.
    now = datetime.now(timezone.utc)
    chat = Chat(
        id="c1", agent_id="ag", created_at=now,
        turn_status="running",  # the worker pool has already claimed it
        last_seq=1,
    )
    await chats.create(chat)
    await msgs.create(ChatMessage(
        id=ChatMessage.make_id("c1", 1),
        chat_id="c1", seq=1, kind="user_message",
        payload={"content": "first"}, created_at=now,
    ))

    # --- Mid-stream side effect: simulate a second user_message landing
    #     while the first turn is still running.
    async def _seed_second_user_message() -> None:
        cur = await chats.get("c1")
        next_seq = cur.last_seq + 1
        await msgs.create(ChatMessage(
            id=ChatMessage.make_id("c1", next_seq),
            chat_id="c1", seq=next_seq, kind="user_message",
            payload={"content": "second"},
            created_at=datetime.now(timezone.utc),
        ))
        cur.last_seq = next_seq
        await chats.update(cur)

    fake_llm = _MidStreamSeederLLM(on_first_stream=_seed_second_user_message)

    async def _get_llm(_pid):
        return fake_llm
    fake_provider_registry.get_llm = _get_llm  # type: ignore[assignment]

    bus = InMemoryEventBus()
    await bus.initialize()
    router = ChatTickRouter()
    deps = ChatDispatchDeps(
        storage_provider=fake_storage_provider,
        provider_registry=fake_provider_registry,
        event_bus=bus,
        chat_tick_router=router,
        fake_llm=fake_llm,
    )

    try:
        disposition = await run_one_chat_turn(deps, chat_id="c1", worker_id="w1")
    finally:
        await bus.aclose()

    # The drain loop must have re-queried storage after the first turn
    # and dispatched the mid-stream-queued user_message as turn 2.
    assert fake_llm._calls_seen == 2, (
        f"expected 2 LLM stream calls (one per queued user_message); "
        f"got {fake_llm._calls_seen}"
    )

    # Both user_messages are accompanied by a ``done`` terminal row.
    from primer.model.storage import (
        FieldRef, Op, OffsetPage, OrderBy, Predicate, Value,
    )
    page = await msgs.find(
        Predicate(
            left=FieldRef(name="chat_id"), op=Op.EQ,
            right=Value(value="c1"),
        ),
        OffsetPage(offset=0, length=200),
        order_by=[OrderBy(field="seq", direction="asc")],
    )
    kinds = [r.kind for r in page.items]
    assert kinds.count("user_message") == 2, kinds
    assert kinds.count("done") == 2, kinds

    # Clean drain -> idle disposition; the fenced adapter applies it.
    assert disposition == "idle"
