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

