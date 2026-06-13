"""Discord full-payload outbound relay."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from primer.channel.chat_dispatcher import ChatChannelDispatcher
from primer.model.chats import Chat, ChatChannelBinding
from primer.model.provider import SqliteConfig
from primer.storage.sqlite import SqliteStorageProvider


class _DiscordLike:
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
async def test_full_payload_relay(tmp_path: Path):
    p = SqliteStorageProvider(SqliteConfig(path=tmp_path / "r.sqlite"))
    await p.initialize()
    await p.get_storage(Chat).create(Chat(
        id="chat-1", agent_id="agent-x", created_at=datetime.now(timezone.utc),
        channel_binding=ChatChannelBinding(
            channel_id="ch-1", thread_external_id="9999")))
    a = _DiscordLike()
    d = ChatChannelDispatcher(storage_provider=p, registry=_StubRegistry(a))
    await d.relay_text(chat_id="chat-1", text="all done")
    assert a.calls == [("all done", "9999")]
