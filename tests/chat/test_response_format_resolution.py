"""Task A2 (chat-refactor plan): resolve the EFFECTIVE `response_format`
per chat turn and thread it into the LLM stream.

Precedence (highest wins): ephemeral (this-send-only, stamped on the
user_message row) -> per-chat ``Chat.response_format`` (A1) -> agent
default (``Agent.response_format``). Exercised end-to-end through
``run_one_chat_turn`` (primer/chat/dispatch.py) so both the dispatch-side
resolution AND the executor-side threading into ``LLM.stream(...)`` are
covered by the same assertions.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any

import pytest
import pytest_asyncio
from pydantic import SecretStr

from primer.bus.in_memory import InMemoryEventBus
from primer.chat.dispatch import ChatDispatchDeps, run_one_chat_turn
from primer.chat.tick_router import ChatTickRouter, Tick
from primer.model.agent import Agent, AgentModel
from primer.model.chat import Done, TextDelta
from primer.model.chats import Chat, ChatMessage
from primer.model.provider import (
    AnthropicConfig, Limits, LLMModel, LLMProvider, LLMProviderType,
)
from primer.model.storage import FieldRef, Op, OffsetPage, OrderBy, Predicate, Value


AGENT_SCHEMA = {"type": "object", "properties": {"a": {"type": "string"}}}
CHAT_SCHEMA = {"type": "object", "properties": {"b": {"type": "string"}}}
EPHEMERAL_SCHEMA = {"type": "object", "properties": {"c": {"type": "string"}}}


class _RFCapturingLLM:
    """Fake LLM that records the ``response_format`` kwarg each
    ``stream(...)`` call is made with, so tests can assert what the
    executor actually threaded through."""

    def __init__(self, tokens=("Hi",)) -> None:
        self._tokens = tokens
        self.calls: list[dict[str, Any]] = []

    async def list_models(self):
        return ["m"]

    def stream(self, *, model, messages, response_format=None, **kwargs):
        self.calls.append({"model": model, "response_format": response_format})
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
    fake_llm = _RFCapturingLLM()

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


async def _seed_agent(deps, *, response_format=None) -> None:
    await deps.storage_provider.get_storage(Agent).create(Agent(
        id="ag", description="x",
        model=AgentModel(provider_id="p", model_name="m"),
        response_format=response_format,
    ))


async def _seed_chat(
    deps, *, chat_response_format=None, ephemeral_response_format=None,
    chat_id="c1",
) -> Chat:
    now = datetime.now(timezone.utc)
    chat = Chat(
        id=chat_id, agent_id="ag", created_at=now,
        turn_status="running", response_format=chat_response_format,
    )
    await deps.storage_provider.get_storage(Chat).create(chat)
    payload: dict[str, Any] = {"content": "Hi"}
    if ephemeral_response_format is not None:
        payload["response_format"] = ephemeral_response_format
    msg = ChatMessage(
        id=ChatMessage.make_id(chat_id, 1),
        chat_id=chat_id, seq=1, kind="user_message",
        payload=payload, created_at=now,
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
class TestResponseFormatResolution:
    async def test_agent_default_only(self, deps):
        """No chat override, no ephemeral override -> the agent's default
        schema is threaded into the LLM stream."""
        await _seed_agent(deps, response_format=AGENT_SCHEMA)
        chat = await _seed_chat(deps)

        disposition = await run_one_chat_turn(deps, chat_id=chat.id, worker_id="w1")

        assert disposition == "idle"
        assert len(deps.fake_llm.calls) == 1
        assert deps.fake_llm.calls[0]["response_format"] == AGENT_SCHEMA

    async def test_chat_response_format_overrides_agent_default(self, deps):
        """A per-chat ``Chat.response_format`` (A1) overrides the agent's
        default for this chat."""
        await _seed_agent(deps, response_format=AGENT_SCHEMA)
        chat = await _seed_chat(deps, chat_response_format=CHAT_SCHEMA)

        disposition = await run_one_chat_turn(deps, chat_id=chat.id, worker_id="w1")

        assert disposition == "idle"
        assert len(deps.fake_llm.calls) == 1
        assert deps.fake_llm.calls[0]["response_format"] == CHAT_SCHEMA

    async def test_ephemeral_response_format_overrides_both(self, deps):
        """An ephemeral ``user_message.payload["response_format"]``
        overrides both the per-chat and the agent-default schema, for
        that one turn only."""
        await _seed_agent(deps, response_format=AGENT_SCHEMA)
        chat = await _seed_chat(
            deps, chat_response_format=CHAT_SCHEMA,
            ephemeral_response_format=EPHEMERAL_SCHEMA,
        )

        disposition = await run_one_chat_turn(deps, chat_id=chat.id, worker_id="w1")

        assert disposition == "idle"
        assert len(deps.fake_llm.calls) == 1
        assert deps.fake_llm.calls[0]["response_format"] == EPHEMERAL_SCHEMA

    async def test_invalid_ephemeral_response_format_fails_turn_closed(self, deps):
        """A malformed ephemeral schema is rejected server-side: an
        ``error`` row (code ``invalid_response_format``) is persisted and
        the LLM stream is never invoked for that turn."""
        await _seed_agent(deps, response_format=AGENT_SCHEMA)
        chat = await _seed_chat(
            deps, ephemeral_response_format={"type": "nonsense-☠"},
        )

        disposition = await run_one_chat_turn(deps, chat_id=chat.id, worker_id="w1")

        assert disposition == "idle"
        assert deps.fake_llm.calls == []
        rows = await _list_all_messages(deps, chat.id)
        error_rows = [r for r in rows if r.kind == "error"]
        assert len(error_rows) == 1
        assert error_rows[0].payload.get("code") == "invalid_response_format"

    async def test_second_turn_does_not_inherit_first_turns_ephemeral_override(
        self, deps,
    ):
        """The runner is reused across every queued turn in one drain
        (see ``run_one_chat_turn``'s while loop). Turn 1's ephemeral
        override must NOT leak onto turn 2, which has none -> turn 2
        falls back to the per-chat/agent-resolved default."""
        await _seed_agent(deps, response_format=AGENT_SCHEMA)
        chat = await _seed_chat(
            deps, ephemeral_response_format=EPHEMERAL_SCHEMA,
        )
        msgs = deps.storage_provider.get_storage(ChatMessage)
        now = datetime.now(timezone.utc)
        await msgs.create(ChatMessage(
            id=ChatMessage.make_id(chat.id, 2),
            chat_id=chat.id, seq=2, kind="user_message",
            payload={"content": "second"}, created_at=now,
        ))
        chats = deps.storage_provider.get_storage(Chat)
        chat.last_seq = 2
        await chats.update(chat)

        disposition = await run_one_chat_turn(deps, chat_id=chat.id, worker_id="w1")

        assert disposition == "idle"
        assert len(deps.fake_llm.calls) == 2
        assert deps.fake_llm.calls[0]["response_format"] == EPHEMERAL_SCHEMA
        assert deps.fake_llm.calls[1]["response_format"] == AGENT_SCHEMA
