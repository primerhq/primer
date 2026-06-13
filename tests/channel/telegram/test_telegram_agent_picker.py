"""Telegram agent-picker keyboard + approval-button gate bridge."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest
from pydantic import SecretStr

from primer.bus.in_memory import InMemoryEventBus
from primer.channel.telegram.adapter import TelegramChannelAdapter
from primer.model.agent import Agent
from primer.model.channel import (
    Channel, ChannelProvider, ChannelProviderType,
    TelegramChannelConfig, TelegramChannelProviderConfig,
)
from primer.model.chats import Chat, ChatMessage
from primer.model.provider import SqliteConfig
from primer.model.storage import OffsetPage, OrderBy
from primer.storage.q import Q
from primer.storage.sqlite import SqliteStorageProvider


async def _setup(tmp_path):
    p = SqliteStorageProvider(SqliteConfig(path=tmp_path / "r.sqlite"))
    await p.initialize()
    for aid, nm in [("agent-x", "X"), ("agent-y", "Y")]:
        await p.get_storage(Agent).create(Agent(
            id=aid, description=nm, model={"provider_id": "lp", "model_name": "m"}))
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
async def test_agent_picker_keyboard_options(tmp_path: Path):
    p, adapter = await _setup(tmp_path)
    await p.get_storage(Chat).create(Chat(
        id="chat-1", agent_id="agent-x", created_at=datetime.now(timezone.utc)))
    kb = await adapter.build_agent_picker_keyboard(chat_id="chat-1")
    flat = [btn for row in kb for btn in row]
    datas = {b["callback_data"] for b in flat}
    assert "pick_agent:chat-1:agent-x" in datas
    assert "pick_agent:chat-1:agent-y" in datas


async def _setup_many(tmp_path, count: int):
    p = SqliteStorageProvider(SqliteConfig(path=tmp_path / "many.sqlite"))
    await p.initialize()
    for i in range(count):
        await p.get_storage(Agent).create(Agent(
            id=f"agent-{i:02d}", description=f"A{i:02d}",
            model={"provider_id": "lp", "model_name": "m"}))
    cp = ChannelProvider(
        id="cp-1", provider=ChannelProviderType.TELEGRAM,
        config=TelegramChannelProviderConfig(bot_token=SecretStr("123456:ABCDEFGHIJKLMNOP")))
    ch = Channel(
        id="ch-1", provider_id="cp-1", provider=ChannelProviderType.TELEGRAM,
        external_id="555")
    await p.get_storage(ChannelProvider).create(cp)
    await p.get_storage(Channel).create(ch)
    adapter = TelegramChannelAdapter(
        provider=cp, channel=ch, inbox=None,
        storage_provider=p, event_bus=InMemoryEventBus())
    return p, adapter


@pytest.mark.asyncio
async def test_agent_picker_pagination(tmp_path: Path):
    p, adapter = await _setup_many(tmp_path, 20)

    # Page 0: 8 agent buttons + a nav row with only "Next >".
    kb0 = await adapter.build_agent_picker_keyboard(chat_id="c", page=0)
    agent_rows0 = [r for r in kb0 if r[0]["callback_data"].startswith("pick_agent:")]
    nav0 = [r for r in kb0 if r[0]["callback_data"].startswith("agentpage:")]
    assert len(agent_rows0) == 8
    assert len(nav0) == 1
    assert [b["text"] for b in nav0[0]] == ["Next >"]
    assert nav0[0][0]["callback_data"] == "agentpage:c:1"
    assert all(r[0]["callback_data"].startswith("pick_agent:c:") for r in agent_rows0)

    # Page 1: 8 agents + nav row with both "< Prev" and "Next >".
    kb1 = await adapter.build_agent_picker_keyboard(chat_id="c", page=1)
    agent_rows1 = [r for r in kb1 if r[0]["callback_data"].startswith("pick_agent:")]
    nav1 = [r for r in kb1 if r[0]["callback_data"].startswith("agentpage:")]
    assert len(agent_rows1) == 8
    assert len(nav1) == 1
    assert [b["text"] for b in nav1[0]] == ["< Prev", "Next >"]
    assert nav1[0][0]["callback_data"] == "agentpage:c:0"
    assert nav1[0][1]["callback_data"] == "agentpage:c:2"

    # Page 2 (last): remainder (20 - 16 = 4) + nav row with only "< Prev".
    kb2 = await adapter.build_agent_picker_keyboard(chat_id="c", page=2)
    agent_rows2 = [r for r in kb2 if r[0]["callback_data"].startswith("pick_agent:")]
    nav2 = [r for r in kb2 if r[0]["callback_data"].startswith("agentpage:")]
    assert len(agent_rows2) == 4
    assert len(nav2) == 1
    assert [b["text"] for b in nav2[0]] == ["< Prev"]
    assert nav2[0][0]["callback_data"] == "agentpage:c:1"


@pytest.mark.asyncio
async def test_apply_agent_pick(tmp_path: Path):
    p, adapter = await _setup(tmp_path)
    await p.get_storage(Chat).create(Chat(
        id="chat-1", agent_id="agent-x", created_at=datetime.now(timezone.utc)))
    notice = await adapter.apply_agent_pick(
        callback_data="pick_agent:chat-1:agent-y")
    assert "Y" in notice
    assert (await p.get_storage(Chat).get("chat-1")).agent_id == "agent-y"


@pytest.mark.asyncio
async def test_approval_button_appends_yes(tmp_path: Path):
    p, adapter = await _setup(tmp_path)
    await p.get_storage(Chat).create(Chat(
        id="chat-1", agent_id="agent-x", created_at=datetime.now(timezone.utc),
        last_seq=1, pending_tool_call={"tool_call_id": "tc-1", "mode": "approval"}))
    await p.get_storage(ChatMessage).create(ChatMessage(
        id=ChatMessage.make_id("chat-1", 1), chat_id="chat-1", seq=1,
        kind="tool_call", payload={"id": "tc-1"},
        created_at=datetime.now(timezone.utc)))
    await adapter.apply_chat_decision_button(callback_data="chat_ok:chat-1")
    rows = (await p.get_storage(ChatMessage).find(
        Q(ChatMessage).where("chat_id", "chat-1").build(),
        OffsetPage(offset=0, length=20),
        order_by=[OrderBy(field="seq", direction="asc")])).items
    ums = [r for r in rows if r.kind == "user_message"]
    assert ums[-1].payload["content"] == "yes"
