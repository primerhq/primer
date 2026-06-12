"""DiscordChannelAdapter._chat_thread_name builds a friendly thread name
"{agent description}: {first words of the first user message}"."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest
from pydantic import SecretStr

from primer.channel.discord.adapter import DiscordChannelAdapter
from primer.chat.enqueue import append_user_message
from primer.model.agent import Agent
from primer.model.channel import (
    Channel,
    ChannelProvider,
    ChannelProviderType,
    DiscordChannelProviderConfig,
)
from primer.model.chat import TextPart
from primer.model.chats import Chat, ChatChannelBinding
from primer.model.provider import SqliteConfig
from primer.storage.sqlite import SqliteStorageProvider


async def _setup(tmp_path: Path):
    p = SqliteStorageProvider(SqliteConfig(path=tmp_path / "r.sqlite"))
    await p.initialize()
    await p.get_storage(Agent).create(Agent(
        id="agent-x", description="Deploy Bot",
        model={"provider_id": "lp", "model_name": "m"}))
    cp = ChannelProvider(
        id="cp-1", provider=ChannelProviderType.DISCORD,
        config=DiscordChannelProviderConfig(bot_token=SecretStr("x" * 40)))
    ch = Channel(id="ch-1", provider_id="cp-1", external_id="9001")
    await p.get_storage(ChannelProvider).create(cp)
    await p.get_storage(Channel).create(ch)
    adapter = DiscordChannelAdapter(
        provider=cp, channel=ch, inbox=None, storage_provider=p)
    return p, adapter, ch


@pytest.mark.asyncio
async def test_chat_thread_name_uses_agent_and_first_words(tmp_path: Path):
    p, adapter, ch = await _setup(tmp_path)
    chat = await p.get_storage(Chat).create(Chat(
        id="chat-1", agent_id="agent-x",
        created_at=datetime.now(timezone.utc),
        channel_binding=ChatChannelBinding(
            channel_id=ch.id, thread_external_id="1700")))
    await append_user_message(
        chat=chat,
        parts=[TextPart(text="[Alice] please deploy the staging service now please")],
        storage_provider=p)

    name = await adapter._chat_thread_name("1700")
    assert name == "Deploy Bot: please deploy the staging service now"


@pytest.mark.asyncio
async def test_chat_thread_name_fallback_when_no_chat(tmp_path: Path):
    _p, adapter, _ch = await _setup(tmp_path)
    assert await adapter._chat_thread_name("does-not-exist") == "chat"
