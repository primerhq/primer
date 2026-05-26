"""Unit tests for matrix.chat.dispatch.run_one_chat_turn — the
worker-side per-turn task."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import datetime, timezone
from typing import Any

import pytest
import pytest_asyncio
from pydantic import SecretStr

from matrix.bus.in_memory import InMemoryEventBus
from matrix.chat.dispatch import ChatDispatchDeps, run_one_chat_turn
from matrix.chat.tick_router import ChatTickRouter, Tick
from matrix.model.agent import Agent, AgentModel
from matrix.model.chat import Done, Message, StreamEvent, TextDelta
from matrix.model.chats import Chat, ChatMessage
from matrix.model.provider import (
    AnthropicConfig, Limits, LLMModel, LLMProvider, LLMProviderType,
)
from matrix.model.storage import (
    FieldRef, Op, OffsetPage, OrderBy, Predicate, Value,
)
from matrix.scheduler.in_memory import InMemoryScheduler


class _FakeLLM:
    def __init__(self, tokens=("Hi", " there", "!")):
        self._tokens = tokens
        self.calls: list[dict[str, Any]] = []

    async def list_models(self):
        return ["m"]

    def stream(self, *, model, messages, **kwargs):
        self.calls.append({"model": model, "messages": list(messages)})
        return self._stream_impl()

    async def _stream_impl(self):
        for t in self._tokens:
            yield TextDelta(text=t, index=0)
        yield Done(stop_reason="stop", raw_reason="stop")

    async def aclose(self):
        return None


@pytest_asyncio.fixture
async def deps(fake_storage_provider, fake_provider_registry):
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
    fake_llm = _FakeLLM()
    async def _get_llm(_pid):
        return fake_llm
    fake_provider_registry.get_llm = _get_llm  # type: ignore[assignment]

    bus = InMemoryEventBus()
    await bus.initialize()
    router = ChatTickRouter()

    async def _forward_ticks():
        sub = bus.subscribe()
        try:
            async for event in sub:
                k = event.event_key
                if k.startswith("chat:") and k.endswith(":tick"):
                    cid = k[len("chat:"):-len(":tick")]
                    seq = event.payload.get("seq") if event.payload else None
                    if isinstance(seq, int):
                        router.publish(cid, Tick(seq=seq))
        except asyncio.CancelledError:
            pass
        finally:
            await sub.aclose()

    fwd = asyncio.create_task(_forward_ticks())
    scheduler = InMemoryScheduler(storage_provider=fake_storage_provider)
    deps_obj = ChatDispatchDeps(
        storage_provider=fake_storage_provider,
        provider_registry=fake_provider_registry,
        scheduler=scheduler,
        event_bus=bus,
        chat_tick_router=router,
        fake_llm=fake_llm,
    )
    yield deps_obj
    fwd.cancel()
    try:
        await fwd
    except asyncio.CancelledError:
        pass
    await bus.aclose()


async def _seed_chat_with_one_message(deps, chat_id="c1") -> Chat:
    now = datetime.now(timezone.utc)
    chat = Chat(
        id=chat_id, agent_id="ag", created_at=now,
        turn_status="running",
        claimed_by="w1",
        claimed_at=now, last_heartbeat_at=now,
    )
    await deps.storage_provider.get_storage(Chat).create(chat)
    msg = ChatMessage(
        id=ChatMessage.make_id(chat_id, 1),
        chat_id=chat_id, seq=1, kind="user_message",
        payload={"content": "Hi"},
        created_at=now,
    )
    await deps.storage_provider.get_storage(ChatMessage).create(msg)
    chat.last_seq = 1
    await deps.storage_provider.get_storage(Chat).update(chat)
    return chat


async def _list_all_messages(deps, chat_id):
    msgs = deps.storage_provider.get_storage(ChatMessage)
    pred = Predicate(left=FieldRef(name="chat_id"), op=Op.EQ,
                     right=Value(value=chat_id))
    page = await msgs.find(pred, OffsetPage(offset=0, length=200),
                           order_by=[OrderBy(field="seq", direction="asc")])
    return page.items


@pytest.mark.asyncio
class TestDrainLoop:
    async def test_single_user_message_processed_to_done(self, deps):
        chat = await _seed_chat_with_one_message(deps)
        await run_one_chat_turn(deps, chat_id=chat.id, worker_id="w1")
        chats = deps.storage_provider.get_storage(Chat)
        row = await chats.get(chat.id)
        assert row.turn_status == "idle"
        assert row.claimed_by is None
        all_rows = await _list_all_messages(deps, chat.id)
        kinds = [r.kind for r in all_rows]
        assert kinds[0] == "user_message"
        assert "assistant_token" in kinds
        assert kinds[-1] == "done"

    async def test_three_queued_user_messages_processed_fifo(self, deps):
        chat = await _seed_chat_with_one_message(deps)
        msgs = deps.storage_provider.get_storage(ChatMessage)
        now = datetime.now(timezone.utc)
        for seq, text in [(2, "second"), (3, "third")]:
            await msgs.create(ChatMessage(
                id=ChatMessage.make_id(chat.id, seq),
                chat_id=chat.id, seq=seq, kind="user_message",
                payload={"content": text}, created_at=now,
            ))
        chats = deps.storage_provider.get_storage(Chat)
        chat.last_seq = 3
        await chats.update(chat)

        await run_one_chat_turn(deps, chat_id=chat.id, worker_id="w1")

        assert len(deps.fake_llm.calls) == 3
        all_rows = await _list_all_messages(deps, chat.id)
        user_msgs = [r for r in all_rows if r.kind == "user_message"]
        assert [m.payload.get("content") for m in user_msgs] == ["Hi", "second", "third"]
        done_rows = [r for r in all_rows if r.kind == "done"]
        assert len(done_rows) == 3
        row = await chats.get(chat.id)
        assert row.turn_status == "idle"
        assert row.claimed_by is None
