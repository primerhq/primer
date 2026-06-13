"""Slack in-thread /command handling: /agent renders an interactive select
seeded with THIS thread's chat; /agent <id> switches it; /help posts help."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import SecretStr

import primer.channel.slack.adapter as adapter_mod
from primer.bus.in_memory import InMemoryEventBus
from primer.channel.chat_router import ChatChannelRouter
from primer.channel.commands import ParsedCommand
from primer.channel.slack.adapter import SlackChannelAdapter
from primer.model.agent import Agent
from primer.model.channel import (
    Channel, ChannelProvider, ChannelProviderType,
    SlackChannelConfig, SlackChannelProviderConfig,
)
from primer.model.chats import Chat
from primer.model.provider import SqliteConfig
from primer.storage.sqlite import SqliteStorageProvider


class _FakeClient:
    def __init__(self):
        self.posts = []

    async def chat_postMessage(self, **kw):
        self.posts.append(kw)
        return {"ts": "1"}


async def _setup(tmp_path, monkeypatch):
    p = SqliteStorageProvider(SqliteConfig(path=tmp_path / "r.sqlite"))
    await p.initialize()
    await p.get_storage(Agent).create(Agent(
        id="agent-x", description="X",
        model={"provider_id": "lp", "model_name": "m"}))
    await p.get_storage(Agent).create(Agent(
        id="agent-y", description="Y",
        model={"provider_id": "lp", "model_name": "m"}))
    cp = ChannelProvider(
        id="cp-1", provider=ChannelProviderType.SLACK,
        config=SlackChannelProviderConfig(
            app_token=SecretStr("xapp-t"), bot_token=SecretStr("xoxb-t")))
    ch = Channel(
        id="ch-1", provider_id="cp-1", provider=ChannelProviderType.SLACK,
        external_id="C123",
        config=SlackChannelConfig(chats={
            "enabled": True, "default_agent": "agent-x",
            "allow_agent_switch": True}))
    await p.get_storage(ChannelProvider).create(cp)
    await p.get_storage(Channel).create(ch)
    adapter = SlackChannelAdapter(
        provider=cp, channel=ch, inbox=None,
        storage_provider=p, event_bus=InMemoryEventBus())
    # Make _get_web_client return the fake; _conn just needs to be non-None.
    fake = _FakeClient()
    adapter._conn = object()
    monkeypatch.setattr(adapter_mod, "_get_web_client", lambda conn: fake)
    return p, adapter, fake


async def _thread_chat(p, thread_ts):
    """Resolve the (already-created) chat bound to a thread on channel ch-1."""
    chat, _ = await ChatChannelRouter(storage_provider=p).resolve_or_create(
        channel_id="ch-1", thread_external_id=thread_ts, supports_threads=True)
    return chat


@pytest.mark.asyncio
async def test_agent_no_arg_renders_select_seeded_with_thread_chat(
    tmp_path: Path, monkeypatch,
):
    p, adapter, fake = await _setup(tmp_path, monkeypatch)
    await adapter._handle_thread_command(
        parsed=ParsedCommand("agent", None), thread_ts="t-1")
    # The thread's chat was resolved/created.
    chat = await _thread_chat(p, "t-1")
    assert chat is not None
    assert len(fake.posts) == 1
    blocks = fake.posts[0]["blocks"]
    accessory = blocks[0]["accessory"]
    assert accessory["action_id"] == "pick_agent"
    values = [o["value"] for o in accessory["options"]]
    assert values, "expected agent options"
    assert all(v.startswith(f"{chat.id}:") for v in values)


@pytest.mark.asyncio
async def test_agent_with_arg_switches_thread_chat(
    tmp_path: Path, monkeypatch,
):
    p, adapter, fake = await _setup(tmp_path, monkeypatch)
    # Seed the thread chat (defaults to agent-x).
    await adapter._handle_thread_command(
        parsed=ParsedCommand("agent", None), thread_ts="t-2")
    chat = await _thread_chat(p, "t-2")
    assert chat.agent_id == "agent-x"
    fake.posts.clear()
    await adapter._handle_thread_command(
        parsed=ParsedCommand("agent", "agent-y"), thread_ts="t-2")
    switched = await p.get_storage(Chat).get(chat.id)
    assert switched.agent_id == "agent-y"
    assert len(fake.posts) == 1
    assert "blocks" not in fake.posts[0]
    assert "agent-y" in fake.posts[0]["text"] or "Y" in fake.posts[0]["text"]


@pytest.mark.asyncio
async def test_help_posts_help_text(tmp_path: Path, monkeypatch):
    p, adapter, fake = await _setup(tmp_path, monkeypatch)
    await adapter._handle_thread_command(
        parsed=ParsedCommand("help", None), thread_ts="t-3")
    assert len(fake.posts) == 1
    text = fake.posts[0]["text"]
    assert "/agent" in text
    assert "/help" in text
