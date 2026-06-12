"""CommandResult shapes for /list and the /agent picker."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from primer.channel.commands import CommandExecutor, CommandResult
from primer.model.agent import Agent
from primer.model.chats import Chat, ChatChannelBinding
from primer.model.provider import SqliteConfig
from primer.storage.sqlite import SqliteStorageProvider


async def _provider(tmp_path):
    p = SqliteStorageProvider(SqliteConfig(path=tmp_path / "r.sqlite"))
    await p.initialize()
    await p.get_storage(Agent).create(
        Agent(id="agent-x", description="Xavier", model={"provider_id": "lp", "model_name": "m"}))
    await p.get_storage(Agent).create(
        Agent(id="agent-y", description="Yara", model={"provider_id": "lp", "model_name": "m"}))
    return p


@pytest.mark.asyncio
async def test_list_returns_channel_chats(tmp_path: Path):
    p = await _provider(tmp_path)
    now = datetime.now(timezone.utc)
    await p.get_storage(Chat).create(Chat(
        id="chat-1", agent_id="agent-x", created_at=now, title="hi",
        channel_binding=ChatChannelBinding(channel_id="ch-1")))
    await p.get_storage(Chat).create(Chat(
        id="chat-2", agent_id="agent-y", created_at=now,
        channel_binding=ChatChannelBinding(channel_id="ch-OTHER")))
    ex = CommandExecutor(storage_provider=p)
    res = await ex.list_chats(channel_id="ch-1")
    assert isinstance(res, CommandResult)
    assert res.kind == "list"
    assert [c["chat_id"] for c in res.items] == ["chat-1"]
    assert res.items[0]["title"] == "hi"
    assert res.items[0]["agent_id"] == "agent-x"


@pytest.mark.asyncio
async def test_agent_picker_lists_agents(tmp_path: Path):
    p = await _provider(tmp_path)
    ex = CommandExecutor(storage_provider=p)
    res = await ex.agent_picker()
    assert res.kind == "agent_picker"
    ids = {o["agent_id"] for o in res.items}
    assert ids == {"agent-x", "agent-y"}
    labels = {o["label"] for o in res.items}
    assert "Xavier" in labels and "Yara" in labels
