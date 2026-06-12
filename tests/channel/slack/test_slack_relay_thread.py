"""relay_text forwards thread_ts to a thread-aware post_chat_message."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from primer.channel.chat_dispatcher import ChatChannelDispatcher
from primer.model.channel import ChatChannelAssociation
from primer.model.chats import Chat, ChatChannelBinding
from primer.model.provider import SqliteConfig
from primer.storage.sqlite import SqliteStorageProvider


class _ThreadAdapter:
    def __init__(self):
        self.calls = []

    async def post_chat_message(self, text, *, thread_ts=None):
        self.calls.append((text, thread_ts))
        return {}


class _StubRegistry:
    def __init__(self, a):
        self._a = a

    def peek_adapter(self, channel_id):
        return self._a

    async def get_adapter(self, channel_id):
        return self._a


@pytest.mark.asyncio
async def test_relay_passes_thread_ts(tmp_path: Path):
    p = SqliteStorageProvider(SqliteConfig(path=tmp_path / "r.sqlite"))
    await p.initialize()
    await p.get_storage(ChatChannelAssociation).create(ChatChannelAssociation(
        id="cca-1", channel_id="ch-1", default_agent_id="agent-x"))
    await p.get_storage(Chat).create(Chat(
        id="chat-1", agent_id="agent-x", created_at=datetime.now(timezone.utc),
        channel_binding=ChatChannelBinding(
            channel_id="ch-1", thread_external_id="1700.1")))
    a = _ThreadAdapter()
    d = ChatChannelDispatcher(storage_provider=p, registry=_StubRegistry(a))
    await d.relay_text(chat_id="chat-1", text="done")
    assert a.calls == [("done", "1700.1")]
