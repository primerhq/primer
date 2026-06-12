"""Mutating command handlers: /new, /switch, /agent <id>."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from primer.channel.commands import CommandExecutor
from primer.model.agent import Agent
from primer.model.channel import ChatChannelAssociation
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
    await p.get_storage(ChatChannelAssociation).create(ChatChannelAssociation(
        id="cca-1", channel_id="ch-1", default_agent_id="agent-x"))
    return p


@pytest.mark.asyncio
async def test_new_single_type_detaches_and_creates(tmp_path: Path):
    p = await _provider(tmp_path)
    now = datetime.now(timezone.utc)
    old = await p.get_storage(Chat).create(Chat(
        id="chat-old", agent_id="agent-x", created_at=now,
        channel_binding=ChatChannelBinding(channel_id="ch-1")))
    cca = await p.get_storage(ChatChannelAssociation).get("cca-1")
    cca.active_chat_id = old.id
    await p.get_storage(ChatChannelAssociation).update(cca)

    ex = CommandExecutor(storage_provider=p)
    res = await ex.new_single_chat(channel_id="ch-1")
    assert res.kind == "notice"
    cca2 = await p.get_storage(ChatChannelAssociation).get("cca-1")
    assert cca2.active_chat_id != old.id
    fresh = await p.get_storage(Chat).get(cca2.active_chat_id)
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
    cca = await p.get_storage(ChatChannelAssociation).get("cca-1")
    assert cca.active_chat_id == prior.id


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
