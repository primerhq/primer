"""ChatChannelDispatcher: relay text + gate forwarding keyed on channel_binding."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from primer.channel.adapter import PromptEnvelope
from primer.channel.chat_dispatcher import ChatChannelDispatcher
from primer.channel.null_adapter import NullChannelAdapter
from primer.model.channel import ChatChannelAssociation
from primer.model.chats import Chat, ChatChannelBinding
from primer.model.provider import SqliteConfig
from primer.storage.sqlite import SqliteStorageProvider


class _StubRegistry:
    def __init__(self, adapter):
        self._adapter = adapter

    async def get_adapter(self, channel_id):
        return self._adapter


async def _provider(tmp_path, *, forward_inform=True):
    p = SqliteStorageProvider(SqliteConfig(path=tmp_path / "r.sqlite"))
    await p.initialize()
    await p.get_storage(ChatChannelAssociation).create(ChatChannelAssociation(
        id="cca-1", channel_id="ch-1", default_agent_id="agent-x",
        forward_inform=forward_inform))
    await p.get_storage(Chat).create(Chat(
        id="chat-1", agent_id="agent-x", created_at=datetime.now(timezone.utc),
        channel_binding=ChatChannelBinding(channel_id="ch-1", thread_external_id="t-9")))
    return p


@pytest.mark.asyncio
async def test_relay_text_posts_inform(tmp_path: Path):
    p = await _provider(tmp_path)
    adapter = NullChannelAdapter()
    d = ChatChannelDispatcher(storage_provider=p, registry=_StubRegistry(adapter))
    await d.relay_text(chat_id="chat-1", text="all done")
    assert len(adapter.posted) == 1
    env = adapter.posted[0]
    assert env.kind == "inform"
    assert env.prompt == "all done"
    assert env.session_id == "t-9"  # thread id carried as the routing key


@pytest.mark.asyncio
async def test_relay_text_suppressed_when_forward_inform_off(tmp_path: Path):
    p = await _provider(tmp_path, forward_inform=False)
    adapter = NullChannelAdapter()
    d = ChatChannelDispatcher(storage_provider=p, registry=_StubRegistry(adapter))
    await d.relay_text(chat_id="chat-1", text="all done")
    assert adapter.posted == []


@pytest.mark.asyncio
async def test_dispatch_gate_forwards_prompt_envelope(tmp_path: Path):
    p = await _provider(tmp_path)
    adapter = NullChannelAdapter()
    d = ChatChannelDispatcher(storage_provider=p, registry=_StubRegistry(adapter))
    env = PromptEnvelope(
        kind="ask_user", workspace_id="", session_id="chat-1",
        tool_call_id="tc-1", prompt="continue?", response_schema=None,
        choices=None, timeout_at_iso=None)
    posted = await d.dispatch_gate(chat_id="chat-1", envelope=env)
    assert posted is True
    assert adapter.posted[0].kind == "ask_user"


@pytest.mark.asyncio
async def test_unbound_chat_is_noop(tmp_path: Path):
    p = await _provider(tmp_path)
    await p.get_storage(Chat).create(Chat(
        id="chat-nobind", agent_id="agent-x",
        created_at=datetime.now(timezone.utc)))
    adapter = NullChannelAdapter()
    d = ChatChannelDispatcher(storage_provider=p, registry=_StubRegistry(adapter))
    await d.relay_text(chat_id="chat-nobind", text="x")
    assert adapter.posted == []
