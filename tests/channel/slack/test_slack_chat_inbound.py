"""Slack multi-type chat inbound: top-level -> new thread-chat; in-thread -> route."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import SecretStr

from primer.bus.in_memory import InMemoryEventBus
from primer.channel.slack.adapter import SlackChannelAdapter
from primer.model.agent import Agent
from primer.model.channel import (
    Channel, ChannelProvider, ChannelProviderType,
    SlackChannelConfig, SlackChannelProviderConfig,
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
        id="cp-1", provider=ChannelProviderType.SLACK,
        config=SlackChannelProviderConfig(
            app_token=SecretStr("xapp-t"), bot_token=SecretStr("xoxb-t")))
    ch = Channel(
        id="ch-1", provider_id="cp-1", provider=ChannelProviderType.SLACK,
        external_id="C123",
        config=SlackChannelConfig(chats={
            "enabled": True, "default_agent": "agent-x"}))
    await p.get_storage(ChannelProvider).create(cp)
    await p.get_storage(Channel).create(ch)
    adapter = SlackChannelAdapter(
        provider=cp, channel=ch, inbox=None,
        storage_provider=p, event_bus=InMemoryEventBus())
    return p, adapter


@pytest.mark.asyncio
async def test_top_level_message_opens_thread_chat(tmp_path: Path):
    p, adapter = await _setup(tmp_path)
    chat = await adapter.handle_inbound_chat_message(
        thread_ts=None, message_ts="1700.0001",
        sender_name="Alice", text="deploy")
    assert chat.channel_binding.thread_external_id == "1700.0001"
    rows = (await p.get_storage(ChatMessage).find(
        Q(ChatMessage).where("chat_id", chat.id).build(),
        OffsetPage(offset=0, length=10))).items
    um = [r for r in rows if r.kind == "user_message"][0]
    assert um.payload["content"] == "[Alice] deploy"


@pytest.mark.asyncio
async def test_in_thread_message_routes_to_same_chat(tmp_path: Path):
    p, adapter = await _setup(tmp_path)
    first = await adapter.handle_inbound_chat_message(
        thread_ts=None, message_ts="1700.0001",
        sender_name="Alice", text="deploy")
    again = await adapter.handle_inbound_chat_message(
        thread_ts="1700.0001", message_ts="1700.0002",
        sender_name="Alice", text="status?")
    assert again.id == first.id
