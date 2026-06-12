"""Chat -> channel relay + gate forwarding wired into run_one_chat_turn.

Task 13: a chat with a channel_binding relays its finalized turn output
(relay_mode 'final' = the joined final assistant text) to the bound channel
and forwards a freshly-set pending gate.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any

import pytest
import pytest_asyncio
from pydantic import SecretStr

from primer.bus.in_memory import InMemoryEventBus
from primer.chat.dispatch import (
    ChatDispatchDeps, _relay_final_text, run_one_chat_turn,
)
from primer.chat.tick_router import ChatTickRouter
from primer.model.agent import Agent, AgentModel
from primer.model.chat import Done, TextDelta
from primer.model.chats import Chat, ChatChannelBinding, ChatMessage
from primer.model.provider import (
    AnthropicConfig, Limits, LLMModel, LLMProvider, LLMProviderType,
)


class _FakeLLM:
    def __init__(self, tokens=("all", " ", "done")):
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


class _RecordingDispatcher:
    """Records relay_text / dispatch_gate calls (stands in for
    ChatChannelDispatcher)."""

    def __init__(self) -> None:
        self.relayed: list[tuple[str, str]] = []
        self.gates: list[tuple[str, Any]] = []

    async def relay_text(self, *, chat_id: str, text: str) -> bool:
        self.relayed.append((chat_id, text))
        return True

    async def dispatch_gate(self, *, chat_id: str, envelope: Any) -> bool:
        self.gates.append((chat_id, envelope))
        return True


@pytest_asyncio.fixture
async def relay_deps(fake_storage_provider, fake_provider_registry):
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
    recorder = _RecordingDispatcher()
    deps_obj = ChatDispatchDeps(
        storage_provider=fake_storage_provider,
        provider_registry=fake_provider_registry,
        event_bus=bus,
        chat_tick_router=ChatTickRouter(),
        fake_llm=fake_llm,
        chat_channel_dispatcher=recorder,
    )
    deps_obj._recorder = recorder  # type: ignore[attr-defined]
    yield deps_obj
    await bus.aclose()


async def _seed_bound_chat(deps, chat_id="cb1") -> Chat:
    now = datetime.now(timezone.utc)
    chat = Chat(
        id=chat_id, agent_id="ag", created_at=now,
        turn_status="running",
        channel_binding=ChatChannelBinding(
            channel_id="ch1", thread_external_id="t1",
        ),
    )
    await deps.storage_provider.get_storage(Chat).create(chat)
    await deps.storage_provider.get_storage(ChatMessage).create(ChatMessage(
        id=ChatMessage.make_id(chat_id, 1),
        chat_id=chat_id, seq=1, kind="user_message",
        payload={"content": "Hi"}, created_at=now,
    ))
    chat.last_seq = 1
    await deps.storage_provider.get_storage(Chat).update(chat)
    return chat


@pytest.mark.asyncio
async def test_completed_turn_relays_final_text(relay_deps):
    chat = await _seed_bound_chat(relay_deps)
    disposition = await run_one_chat_turn(
        relay_deps, chat_id=chat.id, worker_id="w1",
    )
    assert disposition == "idle"
    recorder = relay_deps._recorder  # type: ignore[attr-defined]
    assert recorder.relayed == [(chat.id, "all done")]


@pytest.mark.asyncio
async def test_relay_noop_without_dispatcher(relay_deps):
    relay_deps.chat_channel_dispatcher = None
    chat = await _seed_bound_chat(relay_deps, chat_id="cb2")
    # Should not raise; nothing to record.
    await run_one_chat_turn(relay_deps, chat_id=chat.id, worker_id="w1")
    assert relay_deps._recorder.relayed == []  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_relay_final_text_joins_last_turn_only(relay_deps):
    """_relay_final_text relays only the deltas between the previous and
    final done rows (the last turn's text)."""
    now = datetime.now(timezone.utc)
    chat = Chat(
        id="cb3", agent_id="ag", created_at=now, turn_status="running",
        channel_binding=ChatChannelBinding(
            channel_id="ch1", thread_external_id="t1"),
    )
    await relay_deps.storage_provider.get_storage(Chat).create(chat)
    msgs = relay_deps.storage_provider.get_storage(ChatMessage)
    rows = [
        ("user_message", {"content": "first"}),
        ("assistant_token", {"delta": "old"}),
        ("done", {}),
        ("user_message", {"content": "second"}),
        ("assistant_token", {"delta": "new "}),
        ("assistant_token", {"delta": "text"}),
        ("done", {}),
    ]
    for seq, (kind, payload) in enumerate(rows, start=1):
        await msgs.create(ChatMessage(
            id=ChatMessage.make_id("cb3", seq),
            chat_id="cb3", seq=seq, kind=kind, payload=payload,
            created_at=now,
        ))
    await _relay_final_text(relay_deps, "cb3")
    assert relay_deps._recorder.relayed == [("cb3", "new text")]  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_forward_chat_gate_ask_user(relay_deps):
    """_forward_chat_gate maps an ask_user pending_tool_call to an
    ask_user gate envelope and dispatches it to the bound channel.

    Soft-yield is invoked directly in the runner (the executor turns an
    LLM-stream YieldToWorker into an error row), so the faithful seam is
    the pending_tool_call the runner persists. We seed it as soft_yield
    would and call the helper the two soft_yield branches now invoke."""
    from primer.chat.dispatch import _forward_chat_gate

    now = datetime.now(timezone.utc)
    chat = Chat(
        id="cb4", agent_id="ag", created_at=now, turn_status="running",
        channel_binding=ChatChannelBinding(
            channel_id="ch1", thread_external_id="t1"),
        pending_tool_call={
            "tool_call_id": "tc1", "mode": "ask_user",
            "response_schema": None,
        },
    )
    await relay_deps.storage_provider.get_storage(Chat).create(chat)

    await _forward_chat_gate(relay_deps, "cb4")

    recorder = relay_deps._recorder  # type: ignore[attr-defined]
    assert len(recorder.gates) == 1
    posted_chat_id, env = recorder.gates[0]
    assert posted_chat_id == "cb4"
    assert env.kind == "ask_user"
    assert env.tool_call_id == "tc1"


@pytest.mark.asyncio
async def test_forward_chat_gate_approval(relay_deps):
    """An approval-mode pending_tool_call maps to a tool_approval gate."""
    from primer.chat.dispatch import _forward_chat_gate

    now = datetime.now(timezone.utc)
    chat = Chat(
        id="cb5", agent_id="ag", created_at=now, turn_status="running",
        channel_binding=ChatChannelBinding(
            channel_id="ch1", thread_external_id="t1"),
        pending_tool_call={
            "tool_call_id": "tc2", "mode": "approval",
            "original_call": {"name": "fs__write_file"},
        },
    )
    await relay_deps.storage_provider.get_storage(Chat).create(chat)

    await _forward_chat_gate(relay_deps, "cb5")

    recorder = relay_deps._recorder  # type: ignore[attr-defined]
    assert len(recorder.gates) == 1
    _, env = recorder.gates[0]
    assert env.kind == "tool_approval"
    assert "fs__write_file" in env.prompt


@pytest.mark.asyncio
async def test_forward_chat_gate_noop_without_pending(relay_deps):
    """No pending gate -> nothing forwarded."""
    from primer.chat.dispatch import _forward_chat_gate

    now = datetime.now(timezone.utc)
    chat = Chat(
        id="cb6", agent_id="ag", created_at=now, turn_status="running",
        channel_binding=ChatChannelBinding(
            channel_id="ch1", thread_external_id="t1"),
    )
    await relay_deps.storage_provider.get_storage(Chat).create(chat)

    await _forward_chat_gate(relay_deps, "cb6")
    assert relay_deps._recorder.gates == []  # type: ignore[attr-defined]
