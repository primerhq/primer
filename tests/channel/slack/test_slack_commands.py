"""Slack slash-command dispatch -> CommandExecutor."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from primer.channel.slack.commands import handle_slash_command
from primer.model.agent import Agent
from primer.model.channel import (
    Channel, ChannelProviderType, SlackChannelConfig,
)
from primer.model.chats import Chat, ChatChannelBinding
from primer.model.provider import SqliteConfig
from primer.storage.sqlite import SqliteStorageProvider


async def _provider(tmp_path):
    p = SqliteStorageProvider(SqliteConfig(path=tmp_path / "r.sqlite"))
    await p.initialize()
    for aid, nm in [("agent-x", "X"), ("agent-y", "Y")]:
        await p.get_storage(Agent).create(Agent(
            id=aid, description=nm, model={"provider_id": "lp", "model_name": "m"}))
    await p.get_storage(Channel).create(Channel(
        id="ch-1", provider_id="cp-1", provider=ChannelProviderType.SLACK,
        external_id="C123",
        config=SlackChannelConfig(chats={
            "enabled": True, "default_agent": "agent-x"})))
    return p


@pytest.mark.asyncio
async def test_list_command_returns_blocks(tmp_path: Path):
    p = await _provider(tmp_path)
    await p.get_storage(Chat).create(Chat(
        id="chat-1", agent_id="agent-x", created_at=datetime.now(timezone.utc),
        title="hi", channel_binding=ChatChannelBinding(
            channel_id="ch-1", thread_external_id="t-1")))
    res = await handle_slash_command(
        storage_provider=p, command="/list", text="", channel_id="ch-1",
        thread_ts=None)
    assert res.kind == "list"
    assert res.items[0]["chat_id"] == "chat-1"


@pytest.mark.asyncio
async def test_agent_command_returns_picker(tmp_path: Path):
    p = await _provider(tmp_path)
    res = await handle_slash_command(
        storage_provider=p, command="/agent", text="", channel_id="ch-1",
        thread_ts="t-9")
    assert res.kind == "agent_picker"
    assert {o["agent_id"] for o in res.items} == {"agent-x", "agent-y"}
