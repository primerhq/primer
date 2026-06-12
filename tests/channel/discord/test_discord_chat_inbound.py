"""Discord multi-type chat inbound routing."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import SecretStr

from primer.bus.in_memory import InMemoryEventBus
from primer.channel.discord.adapter import DiscordChannelAdapter
from primer.model.agent import Agent
from primer.model.channel import (
    Channel, ChannelProvider, ChannelProviderType,
    ChatChannelAssociation, DiscordChannelProviderConfig,
)
from primer.model.chats import Chat, ChatMessage
from primer.model.provider import SqliteConfig
from primer.model.storage import OffsetPage
from primer.storage.q import Q
from primer.storage.sqlite import SqliteStorageProvider


async def _setup(tmp_path):
    p = SqliteStorageProvider(SqliteConfig(path=tmp_path / "r.sqlite"))
    await p.initialize()
    await p.get_storage(Agent).create(Agent(
        id="agent-x", description="X", model={"provider_id": "lp", "model_name": "m"}))
    cp = ChannelProvider(
        id="cp-1", provider=ChannelProviderType.DISCORD,
        config=DiscordChannelProviderConfig(bot_token=SecretStr("x" * 40)))
    ch = Channel(id="ch-1", provider_id="cp-1", external_id="9001")
    await p.get_storage(ChannelProvider).create(cp)
    await p.get_storage(Channel).create(ch)
    await p.get_storage(ChatChannelAssociation).create(ChatChannelAssociation(
        id="cca-1", channel_id="ch-1", default_agent_id="agent-x"))
    adapter = DiscordChannelAdapter(
        provider=cp, channel=ch, inbox=None,
        storage_provider=p, event_bus=InMemoryEventBus())
    return p, adapter


@pytest.mark.asyncio
async def test_top_level_opens_thread_chat(tmp_path: Path):
    p, adapter = await _setup(tmp_path)
    chat = await adapter.handle_inbound_chat_message(
        thread_id=None, message_id="m-1", sender_name="Cara", text="deploy")
    assert chat.channel_binding.thread_external_id == "m-1"
    rows = (await p.get_storage(ChatMessage).find(
        Q(ChatMessage).where("chat_id", chat.id).build(),
        OffsetPage(offset=0, length=10))).items
    um = [r for r in rows if r.kind == "user_message"][0]
    assert um.payload["content"] == "[Cara] deploy"


@pytest.mark.asyncio
async def test_in_thread_routes_same_chat(tmp_path: Path):
    p, adapter = await _setup(tmp_path)
    first = await adapter.handle_inbound_chat_message(
        thread_id=None, message_id="m-1", sender_name="Cara", text="deploy")
    again = await adapter.handle_inbound_chat_message(
        thread_id="m-1", message_id="m-2", sender_name="Cara", text="status?")
    assert again.id == first.id
