"""Mutating command handlers: /new, /switch, /agent <id>."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from primer.channel.commands import CommandExecutor
from primer.channel.correlation import ACTIVE_CHAT_ANCHOR, CorrelationStore
from primer.model.agent import Agent
from primer.model.channel import (
    Channel, ChannelProviderType, TelegramChannelConfig,
)
from primer.model.chats import Chat, ChatChannelBinding
from primer.model.except_ import NotFoundError
from primer.model.provider import SqliteConfig
from primer.storage.sqlite import SqliteStorageProvider


async def _provider(tmp_path):
    p = SqliteStorageProvider(SqliteConfig(path=tmp_path / "r.sqlite"))
    await p.initialize()
    for aid, nm in [("agent-x", "X"), ("agent-y", "Y")]:
        await p.get_storage(Agent).create(
            Agent(id=aid, description=nm, model={"provider_id": "lp", "model_name": "m"}))
    await p.get_storage(Channel).create(Channel(
        id="ch-1", provider_id="cp-1", provider=ChannelProviderType.TELEGRAM,
        external_id="555",
        config=TelegramChannelConfig(chats={
            "enabled": True, "default_agent": "agent-x",
            "allow_agent_switch": True})))
    return p


async def _active_chat_id(p, channel_id="ch-1"):
    rec = await CorrelationStore(p).lookup(channel_id, ACTIVE_CHAT_ANCHOR)
    return rec.chat_id if rec is not None else None


@pytest.mark.asyncio
async def test_new_single_type_detaches_and_creates(tmp_path: Path):
    p = await _provider(tmp_path)
    now = datetime.now(timezone.utc)
    old = await p.get_storage(Chat).create(Chat(
        id="chat-old", agent_id="agent-x", created_at=now,
        channel_binding=ChatChannelBinding(channel_id="ch-1")))
    await CorrelationStore(p).set_active_chat("ch-1", old.id)

    ex = CommandExecutor(storage_provider=p)
    res = await ex.new_single_chat(channel_id="ch-1")
    assert res.kind == "notice"
    new_active = await _active_chat_id(p)
    assert new_active != old.id
    fresh = await p.get_storage(Chat).get(new_active)
    assert fresh.agent_id == "agent-x"
    assert fresh.channel_binding.channel_id == "ch-1"


@pytest.mark.asyncio
async def test_switch_reattaches_active_chat(tmp_path: Path):
    p = await _provider(tmp_path)
    now = datetime.now(timezone.utc)
    prior = await p.get_storage(Chat).create(Chat(
        id="chat-prior", agent_id="agent-y", created_at=now,
        channel_binding=ChatChannelBinding(channel_id="ch-1")))
    ex = CommandExecutor(storage_provider=p)
    res = await ex.switch_active_chat(channel_id="ch-1", chat_id=prior.id)
    assert res.kind == "notice"
    assert await _active_chat_id(p) == prior.id


@pytest.mark.asyncio
async def test_switch_unknown_chat_raises(tmp_path: Path):
    p = await _provider(tmp_path)
    ex = CommandExecutor(storage_provider=p)
    with pytest.raises(NotFoundError):
        await ex.switch_active_chat(channel_id="ch-1", chat_id="chat-nope")


@pytest.mark.asyncio
async def test_agent_switch_repoints_chat(tmp_path: Path):
    p = await _provider(tmp_path)
    now = datetime.now(timezone.utc)
    chat = await p.get_storage(Chat).create(Chat(
        id="chat-1", agent_id="agent-x", created_at=now,
        channel_binding=ChatChannelBinding(channel_id="ch-1")))
    ex = CommandExecutor(storage_provider=p)
    res = await ex.set_agent(chat_id=chat.id, agent_id="agent-y")
    assert res.kind == "notice"
    refreshed = await p.get_storage(Chat).get(chat.id)
    assert refreshed.agent_id == "agent-y"


@pytest.mark.asyncio
async def test_agent_switch_blocked_when_flag_off(tmp_path: Path):
    """allow_agent_switch defaults off: set_agent refuses and leaves the chat."""
    p = await _provider(tmp_path)
    # Flip the channel's flag back off.
    channel = await p.get_storage(Channel).get("ch-1")
    channel.config.chats.allow_agent_switch = False
    await p.get_storage(Channel).update(channel)
    now = datetime.now(timezone.utc)
    chat = await p.get_storage(Chat).create(Chat(
        id="chat-1", agent_id="agent-x", created_at=now,
        channel_binding=ChatChannelBinding(channel_id="ch-1")))
    ex = CommandExecutor(storage_provider=p)
    res = await ex.set_agent(chat_id=chat.id, agent_id="agent-y")
    assert res.kind == "notice"
    assert "disabled" in res.text.lower()
    refreshed = await p.get_storage(Chat).get(chat.id)
    assert refreshed.agent_id == "agent-x"


@pytest.mark.asyncio
async def test_agent_switch_allowed_reads_flag(tmp_path: Path):
    p = await _provider(tmp_path)
    ex = CommandExecutor(storage_provider=p)
    assert await ex.agent_switch_allowed("ch-1") is True
    # Unknown channel is treated as not-allowed.
    assert await ex.agent_switch_allowed("ch-nope") is False
    channel = await p.get_storage(Channel).get("ch-1")
    channel.config.chats.allow_agent_switch = False
    await p.get_storage(Channel).update(channel)
    assert await ex.agent_switch_allowed("ch-1") is False
