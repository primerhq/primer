"""Telegram adapter chat-inbound dispatch (command vs message)."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import SecretStr

from primer.bus.in_memory import InMemoryEventBus
from primer.channel.correlation import ACTIVE_CHAT_ANCHOR, CorrelationStore
from primer.channel.telegram.adapter import TelegramChannelAdapter
from primer.model.agent import Agent
from primer.model.channel import (
    Channel, ChannelProvider, ChannelProviderType,
    TelegramChannelConfig, TelegramChannelProviderConfig,
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
    ch = Channel(
        id="ch-1", provider_id="cp-1", provider=ChannelProviderType.TELEGRAM,
        external_id="555",
        config=TelegramChannelConfig(chats={
            "enabled": True, "default_agent": "agent-x"}))
    await p.get_storage(ChannelProvider).create(cp)
    await p.get_storage(Channel).create(ch)
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
async def test_help_command_returns_help_text(tmp_path: Path):
    from primer.channel.commands import help_text
    p, adapter = await _setup(tmp_path)
    notice = await adapter.handle_inbound_chat_text(
        sender_name="Alice", text="/help")
    assert notice == help_text(supports_threads=False)
    assert "/switch" in notice


@pytest.mark.asyncio
async def test_agent_command_blocked_when_flag_off(tmp_path: Path):
    """allow_agent_switch defaults off, so /agent (picker and <id>) is refused."""
    p, adapter = await _setup(tmp_path)
    notice = await adapter.handle_inbound_chat_text(
        sender_name="Alice", text="/agent")
    assert notice == "Agent switching is disabled on this channel."
    notice = await adapter.handle_inbound_chat_text(
        sender_name="Alice", text="/agent agent-x")
    assert notice == "Agent switching is disabled on this channel."


@pytest.mark.asyncio
async def test_new_command_creates_fresh_chat(tmp_path: Path):
    p, adapter = await _setup(tmp_path)
    await adapter.handle_inbound_chat_text(sender_name="Alice", text="hi")
    store = CorrelationStore(p)
    first = (await store.lookup("ch-1", ACTIVE_CHAT_ANCHOR)).chat_id
    notice = await adapter.handle_inbound_chat_text(sender_name="Alice", text="/new")
    assert notice is not None and "fresh" in notice.lower()
    assert (await store.lookup("ch-1", ACTIVE_CHAT_ANCHOR)).chat_id != first
