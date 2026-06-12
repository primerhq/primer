"""Telegram adapter chat-inbound dispatch (command vs message)."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import SecretStr

from primer.bus.in_memory import InMemoryEventBus
from primer.channel.telegram.adapter import TelegramChannelAdapter
from primer.model.agent import Agent
from primer.model.channel import (
    Channel, ChannelProvider, ChannelProviderType,
    ChatChannelAssociation, TelegramChannelProviderConfig,
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
        id="cp-1", provider=ChannelProviderType.TELEGRAM,
        config=TelegramChannelProviderConfig(bot_token=SecretStr("123456:ABCDEFGHIJKLMNOP")))
    ch = Channel(id="ch-1", provider_id="cp-1", external_id="555")
    await p.get_storage(ChannelProvider).create(cp)
    await p.get_storage(Channel).create(ch)
    await p.get_storage(ChatChannelAssociation).create(ChatChannelAssociation(
        id="cca-1", channel_id="ch-1", default_agent_id="agent-x"))
    adapter = TelegramChannelAdapter(
        provider=cp, channel=ch, inbox=None,
        storage_provider=p, event_bus=InMemoryEventBus())
    return p, adapter


@pytest.mark.asyncio
async def test_plain_message_routes_to_chat(tmp_path: Path):
    p, adapter = await _setup(tmp_path)
    await adapter.handle_inbound_chat_text(sender_name="Alice", text="deploy")
    chats = (await p.get_storage(Chat).list(OffsetPage(offset=0, length=10))).items
    assert len(chats) == 1
    rows = (await p.get_storage(ChatMessage).find(
        Q(ChatMessage).where("chat_id", chats[0].id).build(),
        OffsetPage(offset=0, length=10))).items
    um = [r for r in rows if r.kind == "user_message"][0]
    assert um.payload["content"] == "[Alice] deploy"


@pytest.mark.asyncio
async def test_new_command_creates_fresh_chat(tmp_path: Path):
    p, adapter = await _setup(tmp_path)
    await adapter.handle_inbound_chat_text(sender_name="Alice", text="hi")
    cca = await p.get_storage(ChatChannelAssociation).get("cca-1")
    first = cca.active_chat_id
    notice = await adapter.handle_inbound_chat_text(sender_name="Alice", text="/new")
    assert notice is not None and "fresh" in notice.lower()
    cca2 = await p.get_storage(ChatChannelAssociation).get("cca-1")
    assert cca2.active_chat_id != first
