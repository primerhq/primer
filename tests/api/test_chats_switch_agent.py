"""Tests for ``POST /v1/chats/{id}/agent`` — switch a chat's agent mid-chat."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import datetime, timezone
from typing import Any

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from pydantic import SecretStr

from primer.api.app import create_test_app
from primer.model.agent import Agent, AgentModel
from primer.model.chat import Done, Message, StreamEvent, TextDelta
from primer.model.chats import Chat, ChatMessage
from primer.model.provider import (
    AnthropicConfig,
    Limits,
    LLMModel,
    LLMProvider,
    LLMProviderType,
)
from primer.model.storage import OrderBy
from primer.storage.q import Q


class _ChatTestFakeLLM:
    """Deterministic fake LLM — mirrors tests/api/test_chats.py."""

    def __init__(self, reply_text: str = "ok") -> None:
        self._reply_text = reply_text
        self.raise_on_stream: BaseException | None = None
        self.calls: list[dict[str, Any]] = []

    async def list_models(self):
        return ["m"]

    def stream(self, *, model: str, messages: list[Message], **kwargs: Any) -> AsyncIterator[StreamEvent]:
        self.calls.append({"model": model, "messages": list(messages), **kwargs})
        return self._stream_impl()

    async def _stream_impl(self) -> AsyncIterator[StreamEvent]:
        if self.raise_on_stream is not None:
            raise self.raise_on_stream
        yield TextDelta(text=self._reply_text, index=0)
        yield Done(stop_reason="stop", raw_reason="stop")

    async def aclose(self) -> None:
        return None


@pytest.fixture
def fake_llm() -> _ChatTestFakeLLM:
    return _ChatTestFakeLLM()


@pytest.fixture
def app(fake_storage_provider, fake_provider_registry, fake_llm) -> FastAPI:
    async def _get_llm(_pid: str) -> _ChatTestFakeLLM:
        return fake_llm

    fake_provider_registry.get_llm = _get_llm  # type: ignore[assignment]
    return create_test_app(
        storage_provider=fake_storage_provider,
        provider_registry=fake_provider_registry,
    )


@pytest_asyncio.fixture
async def client(app):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        try:
            await c.post("/v1/auth/register", json={"username": "testuser", "password": "testpassword"})
        except Exception:
            pass
        yield c


@pytest_asyncio.fixture
async def seeded_provider(app):
    """An LLMProvider carrying the model the seeded agents reference."""
    llm_storage = app.state.storage_provider.get_storage(LLMProvider)
    await llm_storage.create(
        LLMProvider(
            id="llm-p",
            provider=LLMProviderType.ANTHROPIC,
            models=[LLMModel(name="m", context_length=8000)],
            config=AnthropicConfig(api_key=SecretStr("test-only")),
            limits=Limits(max_concurrency=1),
        ),
    )


@pytest_asyncio.fixture
async def make_agent(app, seeded_provider):
    async def _make(agent_id: str) -> Agent:
        agent = Agent(
            id=agent_id,
            description=f"agent {agent_id}",
            model=AgentModel(provider_id="llm-p", model_name="m"),
            tools=[],
            system_prompt=[],
        )
        return await app.state.storage_provider.get_storage(Agent).create(agent)

    return _make


@pytest_asyncio.fixture
async def make_chat(app):
    async def _make(*, agent_id: str, status: str = "active",
                    pending_tool_call: dict[str, Any] | None = None) -> Chat:
        chats = app.state.storage_provider.get_storage(Chat)
        chat = Chat(
            id=f"chat-{agent_id}-{status}",
            agent_id=agent_id,
            created_at=datetime.now(timezone.utc),
            status=status,  # type: ignore[arg-type]
        )
        chat = await chats.create(chat)
        if pending_tool_call is not None:
            # Seed the unpaired tool_call row + the pending marker so the
            # auto-reject path has something to close out.
            messages = app.state.storage_provider.get_storage(ChatMessage)
            next_seq = chat.last_seq + 1
            await messages.create(
                ChatMessage(
                    id=ChatMessage.make_id(chat.id, next_seq),
                    chat_id=chat.id,
                    seq=next_seq,
                    kind="tool_call",
                    payload={"id": pending_tool_call.get("tool_call_id")},
                    created_at=datetime.now(timezone.utc),
                ),
            )
            chat.last_seq = next_seq
            chat.pending_tool_call = pending_tool_call
            chat = await chats.update(chat)
        return chat

    return _make


@pytest_asyncio.fixture
async def get_messages(app):
    async def _get(chat_id: str) -> list[ChatMessage]:
        messages = app.state.storage_provider.get_storage(ChatMessage)
        from primer.model.storage import OffsetPage

        page = await messages.find(
            Q(ChatMessage).where("chat_id", chat_id).build(),
            OffsetPage(offset=0, length=200),
            order_by=[OrderBy(field="seq", direction="asc")],
        )
        return list(page.items)

    return _get


@pytest.mark.asyncio
async def test_switch_agent_updates_agent_id(client, make_agent, make_chat):
    await make_agent("agent-A")
    await make_agent("agent-B")
    chat = await make_chat(agent_id="agent-A")
    resp = await client.post(f"/v1/chats/{chat.id}/agent", json={"agent_id": "agent-B"})
    assert resp.status_code == 200, resp.text
    assert resp.json()["agent_id"] == "agent-B"


@pytest.mark.asyncio
async def test_switch_agent_unknown_agent_404(client, make_agent, make_chat):
    await make_agent("agent-A")
    chat = await make_chat(agent_id="agent-A")
    resp = await client.post(f"/v1/chats/{chat.id}/agent", json={"agent_id": "nope"})
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_switch_agent_unknown_chat_404(client, make_agent):
    await make_agent("agent-A")
    resp = await client.post("/v1/chats/no-such-chat/agent", json={"agent_id": "agent-A"})
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_switch_agent_on_ended_chat_409(client, make_agent, make_chat):
    await make_agent("agent-A")
    await make_agent("agent-B")
    chat = await make_chat(agent_id="agent-A", status="ended")
    resp = await client.post(f"/v1/chats/{chat.id}/agent", json={"agent_id": "agent-B"})
    assert resp.status_code == 409


@pytest.mark.asyncio
async def test_switch_agent_same_agent_is_idempotent_noop(
    client, make_agent, make_chat, get_messages,
):
    await make_agent("agent-A")
    chat = await make_chat(agent_id="agent-A")
    resp = await client.post(f"/v1/chats/{chat.id}/agent", json={"agent_id": "agent-A"})
    assert resp.status_code == 200, resp.text
    assert resp.json()["agent_id"] == "agent-A"
    # No-op: no extra message rows written.
    assert await get_messages(chat.id) == []


@pytest.mark.asyncio
async def test_switch_agent_auto_rejects_pending_gate(
    client, make_agent, make_chat, get_messages,
):
    await make_agent("agent-A")
    await make_agent("agent-B")
    chat = await make_chat(
        agent_id="agent-A",
        pending_tool_call={"tool_call_id": "tc1", "mode": "approval"},
    )
    resp = await client.post(f"/v1/chats/{chat.id}/agent", json={"agent_id": "agent-B"})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["agent_id"] == "agent-B"
    assert body["pending_tool_call"] is None
    kinds = [m.kind for m in await get_messages(chat.id)]
    assert "tool_result" in kinds
    assert "cancelled" in kinds
