"""Telegram outbound chat relay uses post_chat_message."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from primer.channel.chat_dispatcher import ChatChannelDispatcher
from primer.model.channel import ChatChannelAssociation
from primer.model.chats import Chat, ChatChannelBinding
from primer.model.provider import SqliteConfig
from primer.storage.sqlite import SqliteStorageProvider


class _FakeAdapter:
    def __init__(self):
        self.chat_messages = []
        self.posted = []

    async def post_chat_message(self, text):
        self.chat_messages.append(text)
        return {"message_id": 1}

    async def post_prompt(self, env):
        self.posted.append(env)
        return {}


class _StubRegistry:
    def __init__(self, adapter):
        self._a = adapter

    async def get_adapter(self, channel_id):
        return self._a


@pytest.mark.asyncio
async def test_relay_prefers_post_chat_message(tmp_path: Path):
    p = SqliteStorageProvider(SqliteConfig(path=tmp_path / "r.sqlite"))
    await p.initialize()
    await p.get_storage(ChatChannelAssociation).create(ChatChannelAssociation(
        id="cca-1", channel_id="ch-1", default_agent_id="agent-x"))
    await p.get_storage(Chat).create(Chat(
        id="chat-1", agent_id="agent-x", created_at=datetime.now(timezone.utc),
        channel_binding=ChatChannelBinding(channel_id="ch-1")))
    adapter = _FakeAdapter()
    d = ChatChannelDispatcher(storage_provider=p, registry=_StubRegistry(adapter))
    assert await d.relay_text(chat_id="chat-1", text="hello") is True
    assert adapter.chat_messages == ["hello"]
    assert adapter.posted == []
