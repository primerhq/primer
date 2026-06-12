"""ChatChannelRouter resolve-or-create + single/multi binding."""

from __future__ import annotations

from pathlib import Path

import pytest

from primer.channel.chat_router import ChatChannelRouter
from primer.model.agent import Agent
from primer.model.channel import ChatChannelAssociation
from primer.model.chats import Chat
from primer.model.provider import SqliteConfig
from primer.storage.sqlite import SqliteStorageProvider


async def _provider(tmp_path):
    p = SqliteStorageProvider(SqliteConfig(path=tmp_path / "r.sqlite"))
    await p.initialize()
    await p.get_storage(Agent).create(
        Agent(id="agent-x", description="X",
              model={"provider_id": "lp", "model_name": "m"}))
    await p.get_storage(ChatChannelAssociation).create(
        ChatChannelAssociation(id="cca-1", channel_id="ch-1", default_agent_id="agent-x"))
    return p


@pytest.mark.asyncio
async def test_multi_type_creates_thread_chat_then_resolves_it(tmp_path: Path):
    p = await _provider(tmp_path)
    r = ChatChannelRouter(storage_provider=p)

    chat, created = await r.resolve_or_create(
        channel_id="ch-1", thread_external_id="t-9", supports_threads=True)
    assert created is True
    assert chat.agent_id == "agent-x"
    assert chat.channel_binding.channel_id == "ch-1"
    assert chat.channel_binding.thread_external_id == "t-9"

    again, created2 = await r.resolve_or_create(
        channel_id="ch-1", thread_external_id="t-9", supports_threads=True)
    assert created2 is False
    assert again.id == chat.id


@pytest.mark.asyncio
async def test_single_type_uses_active_chat_id(tmp_path: Path):
    p = await _provider(tmp_path)
    r = ChatChannelRouter(storage_provider=p)

    chat, created = await r.resolve_or_create(
        channel_id="ch-1", thread_external_id=None, supports_threads=False)
    assert created is True
    assert chat.channel_binding.thread_external_id is None

    cca = await p.get_storage(ChatChannelAssociation).get("cca-1")
    assert cca.active_chat_id == chat.id

    again, created2 = await r.resolve_or_create(
        channel_id="ch-1", thread_external_id=None, supports_threads=False)
    assert created2 is False
    assert again.id == chat.id


@pytest.mark.asyncio
async def test_ended_single_type_chat_creates_fresh(tmp_path: Path):
    p = await _provider(tmp_path)
    r = ChatChannelRouter(storage_provider=p)
    chat, _ = await r.resolve_or_create(
        channel_id="ch-1", thread_external_id=None, supports_threads=False)
    ended = await p.get_storage(Chat).get(chat.id)
    ended.status = "ended"
    await p.get_storage(Chat).update(ended)

    fresh, created = await r.resolve_or_create(
        channel_id="ch-1", thread_external_id=None, supports_threads=False)
    assert created is True
    assert fresh.id != chat.id


@pytest.mark.asyncio
async def test_missing_agent_raises(tmp_path: Path):
    from primer.model.except_ import NotFoundError
    p = await _provider(tmp_path)
    await p.get_storage(Agent).delete("agent-x")
    r = ChatChannelRouter(storage_provider=p)
    with pytest.raises(NotFoundError):
        await r.resolve_or_create(
            channel_id="ch-1", thread_external_id=None, supports_threads=False)
