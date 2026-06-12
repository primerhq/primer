"""Discord slash-command dispatch + agent autocomplete choices."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from primer.channel.discord.commands import (
    agent_autocomplete_choices, handle_app_command,
)
from primer.model.agent import Agent
from primer.model.channel import ChatChannelAssociation
from primer.model.chats import Chat, ChatChannelBinding
from primer.model.provider import SqliteConfig
from primer.storage.sqlite import SqliteStorageProvider


async def _provider(tmp_path):
    p = SqliteStorageProvider(SqliteConfig(path=tmp_path / "r.sqlite"))
    await p.initialize()
    for aid, nm in [("agent-x", "Xavier"), ("agent-y", "Yara")]:
        await p.get_storage(Agent).create(Agent(
            id=aid, description=nm, model={"provider_id": "lp", "model_name": "m"}))
    await p.get_storage(ChatChannelAssociation).create(ChatChannelAssociation(
        id="cca-1", channel_id="ch-1", default_agent_id="agent-x"))
    return p


@pytest.mark.asyncio
async def test_autocomplete_filters_by_prefix(tmp_path: Path):
    p = await _provider(tmp_path)
    choices = await agent_autocomplete_choices(storage_provider=p, current="Ya")
    assert [c["value"] for c in choices] == ["agent-y"]
    assert choices[0]["name"] == "Yara"


@pytest.mark.asyncio
async def test_list_command(tmp_path: Path):
    p = await _provider(tmp_path)
    await p.get_storage(Chat).create(Chat(
        id="chat-1", agent_id="agent-x", created_at=datetime.now(timezone.utc),
        title="hi", channel_binding=ChatChannelBinding(
            channel_id="ch-1", thread_external_id="t-1")))
    res = await handle_app_command(
        storage_provider=p, command="list", channel_id="ch-1", arg=None,
        thread_id=None)
    assert res.kind == "list"
    assert res.items[0]["chat_id"] == "chat-1"


@pytest.mark.asyncio
async def test_agent_command_with_value_switches(tmp_path: Path):
    p = await _provider(tmp_path)
    await p.get_storage(Chat).create(Chat(
        id="chat-1", agent_id="agent-x", created_at=datetime.now(timezone.utc),
        channel_binding=ChatChannelBinding(
            channel_id="ch-1", thread_external_id="t-1")))
    res = await handle_app_command(
        storage_provider=p, command="agent", channel_id="ch-1",
        arg="agent-y", thread_id="t-1")
    assert res.kind == "notice"
    assert (await p.get_storage(Chat).get("chat-1")).agent_id == "agent-y"
