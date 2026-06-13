"""_find_thread_chat skips ended chats so a live thread reuses its live chat."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from primer.channel.chat_router import ChatChannelRouter
from primer.model.chats import Chat, ChatChannelBinding
from primer.model.provider import SqliteConfig
from primer.storage.sqlite import SqliteStorageProvider


@pytest.mark.asyncio
async def test_find_thread_chat_skips_ended(tmp_path: Path):
    p = SqliteStorageProvider(SqliteConfig(path=tmp_path / "r.sqlite"))
    await p.initialize()
    now = datetime.now(timezone.utc)
    binding = ChatChannelBinding(channel_id="ch-1", thread_external_id="th-1")
    ended = Chat(id="chat-ended", agent_id="a", created_at=now,
                 status="ended", channel_binding=binding)
    live = Chat(id="chat-live", agent_id="a", created_at=now,
                channel_binding=binding)
    await p.get_storage(Chat).create(ended)
    await p.get_storage(Chat).create(live)
    r = ChatChannelRouter(storage_provider=p)
    found = await r._find_thread_chat(channel_id="ch-1", thread_external_id="th-1")
    assert found is not None and found.id == "chat-live"
