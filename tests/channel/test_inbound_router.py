"""ChannelInboundRouter: anchor resolution -> chat creation / gate / active chat.

Seeds real ``Channel`` rows (config.chats) + ``ChannelCorrelation`` rows via a
SqliteStorageProvider and drives :meth:`ChannelInboundRouter.route` for each
shape: thread-channel top-level (new thread), in-thread session gate, in-thread
chat delivery, and single-type unknown anchor (active chat).
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from primer.channel.correlation import CorrelationStore
from primer.channel.inbound_router import ChannelInboundRouter
from primer.model.agent import Agent
from primer.model.channel import (
    Channel, ChannelProviderType,
    SlackChannelConfig, TelegramChannelConfig,
)
from primer.model.chats import Chat, ChatChannelBinding, ChatMessage
from primer.model.provider import SqliteConfig
from primer.model.storage import OffsetPage
from primer.storage.q import Q
from primer.storage.sqlite import SqliteStorageProvider


class _RecordingBus:
    def __init__(self) -> None:
        self.published: list[tuple[str, dict]] = []

    async def publish(self, event_key, payload=None):
        self.published.append((event_key, payload or {}))


async def _provider(tmp_path):
    p = SqliteStorageProvider(SqliteConfig(path=tmp_path / "r.sqlite"))
    await p.initialize()
    await p.get_storage(Agent).create(Agent(
        id="agent-x", description="X",
        model={"provider_id": "lp", "model_name": "m"}))
    return p


async def _thread_channel(p, channel_id="ch-thr", enabled=True):
    ch = Channel(
        id=channel_id, provider_id="cp-1", provider=ChannelProviderType.SLACK,
        external_id="C100",
        config=SlackChannelConfig(chats={
            "enabled": enabled, "default_agent": "agent-x" if enabled else None}))
    await p.get_storage(Channel).create(ch)
    return ch


async def _single_channel(p, channel_id="ch-tg"):
    ch = Channel(
        id=channel_id, provider_id="cp-2", provider=ChannelProviderType.TELEGRAM,
        external_id="555",
        config=TelegramChannelConfig(chats={
            "enabled": True, "default_agent": "agent-x"}))
    await p.get_storage(Channel).create(ch)
    return ch


async def _user_rows(p, chat_id):
    rows = (await p.get_storage(ChatMessage).find(
        Q(ChatMessage).where("chat_id", chat_id).build(),
        OffsetPage(offset=0, length=50))).items
    return [r for r in rows if r.kind == "user_message"]


@pytest.mark.asyncio
async def test_thread_top_level_opens_new_thread_chat(tmp_path: Path):
    p = await _provider(tmp_path)
    ch = await _thread_channel(p)
    store = CorrelationStore(p)
    router = ChannelInboundRouter(p, store)

    await router.route(
        channel=ch, anchor=None, reply_to="m-1", is_thread_channel=True,
        sender="Cara", text="deploy")

    chats = (await p.get_storage(Chat).list(OffsetPage(offset=0, length=10))).items
    assert len(chats) == 1
    chat = chats[0]
    assert chat.channel_binding.thread_external_id == "m-1"
    rows = await _user_rows(p, chat.id)
    assert rows[-1].payload["content"] == "[Cara] deploy"
    # The thread->chat correlation is recorded for later in-thread messages.
    rec = await store.lookup(ch.id, "m-1")
    assert rec is not None and rec.kind == "chat" and rec.chat_id == chat.id


@pytest.mark.asyncio
async def test_thread_top_level_disabled_chats_ignored(tmp_path: Path):
    p = await _provider(tmp_path)
    ch = await _thread_channel(p, channel_id="ch-off", enabled=False)
    router = ChannelInboundRouter(p, CorrelationStore(p))

    await router.route(
        channel=ch, anchor=None, reply_to="m-1", is_thread_channel=True,
        sender="Cara", text="deploy")

    chats = (await p.get_storage(Chat).list(OffsetPage(offset=0, length=10))).items
    assert chats == []


@pytest.mark.asyncio
async def test_in_thread_session_correlation_publishes_gate_resume(tmp_path: Path):
    p = await _provider(tmp_path)
    ch = await _thread_channel(p)
    store = CorrelationStore(p)
    await store.upsert_session(
        channel_id=ch.id, anchor="thr-7", workspace_id="ws-1",
        session_id="sess-9", tool_call_id="tc-3")
    bus = _RecordingBus()
    router = ChannelInboundRouter(p, store, event_bus=bus)

    await router.route(
        channel=ch, anchor="thr-7", reply_to="m-2", is_thread_channel=True,
        sender="Cara", text="go ahead")

    assert bus.published == [
        ("ask_user:sess-9:tc-3", {"response": "go ahead"})]
    # No chat created for a session-gate reply.
    chats = (await p.get_storage(Chat).list(OffsetPage(offset=0, length=10))).items
    assert chats == []


@pytest.mark.asyncio
async def test_in_thread_chat_correlation_delivers_message(tmp_path: Path):
    p = await _provider(tmp_path)
    ch = await _thread_channel(p)
    store = CorrelationStore(p)
    # An existing thread-chat bound to anchor "thr-5".
    chat = await p.get_storage(Chat).create(Chat(
        id="chat-bound", agent_id="agent-x",
        created_at=datetime.now(timezone.utc),
        channel_binding=ChatChannelBinding(
            channel_id=ch.id, thread_external_id="thr-5")))
    await store.upsert_chat(channel_id=ch.id, anchor="thr-5", chat_id=chat.id)
    router = ChannelInboundRouter(p, store)

    await router.route(
        channel=ch, anchor="thr-5", reply_to="m-3", is_thread_channel=True,
        sender="Dan", text="next step?")

    rows = await _user_rows(p, chat.id)
    assert rows[-1].payload["content"] == "[Dan] next step?"
    # No new chat: the existing thread chat received it.
    chats = (await p.get_storage(Chat).list(OffsetPage(offset=0, length=10))).items
    assert len(chats) == 1


@pytest.mark.asyncio
async def test_single_type_unknown_anchor_routes_active_chat(tmp_path: Path):
    p = await _provider(tmp_path)
    ch = await _single_channel(p)
    store = CorrelationStore(p)
    router = ChannelInboundRouter(p, store)

    # First message: no active chat yet -> create one.
    await router.route(
        channel=ch, anchor=None, reply_to=None, is_thread_channel=False,
        sender="Eve", text="hello")
    chats = (await p.get_storage(Chat).list(OffsetPage(offset=0, length=10))).items
    assert len(chats) == 1
    first = chats[0]
    assert first.channel_binding.thread_external_id is None

    # Second message routes to the SAME active chat.
    await router.route(
        channel=ch, anchor=None, reply_to=None, is_thread_channel=False,
        sender="Eve", text="again")
    chats = (await p.get_storage(Chat).list(OffsetPage(offset=0, length=10))).items
    assert len(chats) == 1
    rows = await _user_rows(p, first.id)
    assert [r.payload["content"] for r in rows] == ["[Eve] hello", "[Eve] again"]
