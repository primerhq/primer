"""Unit tests for primer.chat.dispatch.run_one_chat_turn — the
worker-side per-turn task."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import datetime, timezone
from typing import Any

import pytest
import pytest_asyncio
from pydantic import SecretStr

from primer.bus.in_memory import InMemoryEventBus
from primer.chat.dispatch import ChatDispatchDeps, run_one_chat_turn
from primer.chat.tick_router import ChatTickRouter, Tick
from primer.model.agent import Agent, AgentModel
from primer.model.chat import Done, Message, StreamEvent, TextDelta
from primer.model.chats import Chat, ChatMessage
from primer.model.provider import (
    AnthropicConfig, Limits, LLMModel, LLMProvider, LLMProviderType,
)
from primer.model.storage import (
    FieldRef, Op, OffsetPage, OrderBy, Predicate, Value,
)

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
    deps_obj = ChatDispatchDeps(
        storage_provider=fake_storage_provider,
        provider_registry=fake_provider_registry,
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

    async def test_queued_user_messages_do_not_contaminate_earlier_prompts(self, deps):
        """Turn N's LLM prompt must NOT carry queued user_messages from
        positions > N. Each call to ``deps.fake_llm.stream`` records the
        messages it received; we inspect them after the drain completes
        and assert each turn only saw its own user message + history of
        already-finished turns.
        """
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

        def _user_texts(call):
            out = []
            for m in call["messages"]:
                if m.role != "user":
                    continue
                for p in m.parts:
                    text = getattr(p, "text", None)
                    if text:
                        out.append(text)
            return out

        assert _user_texts(deps.fake_llm.calls[0]) == ["Hi"]
        assert _user_texts(deps.fake_llm.calls[1]) == ["Hi", "second"]
        assert _user_texts(deps.fake_llm.calls[2]) == ["Hi", "second", "third"]


class _CancelOnFirstTokenLLM:
    """Fake LLM that sets a supplied asyncio.Event on first call so the
    cancel_event fires mid-stream of turn 1 only. Subsequent turns use
    the plain token sequence."""

    def __init__(self, *, on_first_call):
        self._on_first_call = on_first_call
        self._calls_seen = 0
        self.calls: list[dict[str, Any]] = []

    async def list_models(self):
        return ["m"]

    def stream(self, *, model, messages, **kwargs):
        self.calls.append({"model": model, "messages": list(messages)})
        self._calls_seen += 1
        first = self._calls_seen == 1
        return self._stream_impl(first)

    async def _stream_impl(self, first):
        if first:
            await self._on_first_call()
            yield TextDelta(text="partial", index=0)
            return
        yield TextDelta(text="ok", index=0)
        yield Done(stop_reason="stop", raw_reason="stop")

    async def aclose(self):
        return None


@pytest.mark.asyncio
class TestCancelLifecycle:
    async def test_cancel_does_not_carry_across_queued_turns(
        self, fake_storage_provider, fake_provider_registry,
    ):
        """A cancel on turn 1 produces a 'cancelled' row, but turn 2
        (already queued) runs to 'done' — cancel is per-turn, never
        per-queue.
        """
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
        bus = InMemoryEventBus()
        await bus.initialize()

        async def _on_first_call():
            row = await chats.get("c1")
            row.cancel_requested_at = datetime.now(timezone.utc)
            await chats.update(row)
            await bus.publish("chat:c1:cancel", {})
            for _ in range(50):
                await asyncio.sleep(0.01)
                cur = await chats.get("c1")
                if cur.cancel_requested_at is not None:
                    break

        cancel_llm = _CancelOnFirstTokenLLM(on_first_call=_on_first_call)
        async def _get_llm(_pid):
            return cancel_llm
        fake_provider_registry.get_llm = _get_llm  # type: ignore[assignment]

        router = ChatTickRouter()
        deps_obj = ChatDispatchDeps(
            storage_provider=fake_storage_provider,
            provider_registry=fake_provider_registry,
            event_bus=bus,
            chat_tick_router=router,
            fake_llm=cancel_llm,
        )

        now = datetime.now(timezone.utc)
        chat = Chat(
            id="c1", agent_id="ag", created_at=now,
            turn_status="running",
            claimed_by="w1",
            claimed_at=now, last_heartbeat_at=now,
            last_seq=2,
        )
        await chats.create(chat)
        for seq, text in [(1, "first"), (2, "second")]:
            await msgs.create(ChatMessage(
                id=ChatMessage.make_id("c1", seq),
                chat_id="c1", seq=seq, kind="user_message",
                payload={"content": text}, created_at=now,
            ))

        try:
            await run_one_chat_turn(deps_obj, chat_id="c1", worker_id="w1")
        finally:
            await bus.aclose()

        all_rows = await _list_all_messages(deps_obj, "c1")
        kinds = [r.kind for r in all_rows]
        assert kinds.count("cancelled") == 1, kinds
        assert kinds.count("done") == 1, kinds
        final = await chats.get("c1")
        assert final.turn_status == "idle"
        assert final.cancel_requested_at is None

    async def test_runner_append_preserves_concurrent_cancel_flag(
        self, fake_storage_provider, fake_provider_registry,
    ):
        """When the WS sets cancel_requested_at concurrently with a runner
        token write, the runner's chat update must NOT clobber it back
        to None. This pins the spec's 'storage is durable cancel state'
        contract — bus drop must not lose the cancel signal.
        """
        from primer.chat.executor import ChatTurnRunner

        await fake_storage_provider.get_storage(LLMProvider).create(
            LLMProvider(
                id="p", provider=LLMProviderType.ANTHROPIC,
                models=[LLMModel(name="m", context_length=8192)],
                config=AnthropicConfig(api_key=SecretStr("test")),
                limits=Limits(max_concurrency=1),
            ),
        )
        agent = Agent(
            id="ag", description="x",
            model=AgentModel(provider_id="p", model_name="m"),
        )
        await fake_storage_provider.get_storage(Agent).create(agent)
        chats = fake_storage_provider.get_storage(Chat)
        msgs = fake_storage_provider.get_storage(ChatMessage)

        now = datetime.now(timezone.utc)
        chat = Chat(
            id="c2", agent_id="ag", created_at=now,
            turn_status="running",
            claimed_by="w1",
            claimed_at=now, last_heartbeat_at=now,
            last_seq=0,
        )
        await chats.create(chat)

        runner = ChatTurnRunner(
            agent=agent,
            llm=_FakeLLM(),
            llm_model=LLMModel(name="m", context_length=8192),
            tool_manager=None,
            chat_storage=chats,
            message_storage=msgs,
        )

        # Simulate the WS having set the cancel flag after the runner
        # already captured its in-memory chat snapshot.
        cancel_time = datetime.now(timezone.utc)
        chat_in_storage = await chats.get("c2")
        chat_in_storage.cancel_requested_at = cancel_time
        await chats.update(chat_in_storage)

        # _append must refresh cancel_requested_at from storage and
        # preserve it on write.
        await runner._append(chat, kind="assistant_token", payload={"delta": "x"})

        after = await chats.get("c2")
        assert after.cancel_requested_at == cancel_time
