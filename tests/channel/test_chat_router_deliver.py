"""ChatChannelRouter.deliver_message: attribution, claimable flip, gate route."""

from __future__ import annotations

from pathlib import Path

import pytest

from primer.bus.in_memory import InMemoryEventBus
from primer.channel.chat_router import ChatChannelRouter
from primer.model.agent import Agent
from primer.model.channel import ChatChannelAssociation
from primer.model.chats import Chat, ChatMessage
from primer.model.provider import SqliteConfig
from primer.model.storage import OffsetPage
from primer.storage.q import Q
from primer.storage.sqlite import SqliteStorageProvider


class _RecordingGateInbox:
    def __init__(self):
        self.calls = []

    async def handle_chat_response(self, *, chat_id, pending, text, sender):
        self.calls.append((chat_id, pending, text, sender))


class _RecClaim:
    def __init__(self):
        self.calls = []

    async def upsert(self, kind, entity_id, *, priority=0):
        self.calls.append((kind, entity_id, priority))


async def _provider(tmp_path):
    p = SqliteStorageProvider(SqliteConfig(path=tmp_path / "r.sqlite"))
    await p.initialize()
    await p.get_storage(Agent).create(
        Agent(id="agent-x", description="X", model={"provider_id": "lp", "model_name": "m"}))
    await p.get_storage(ChatChannelAssociation).create(
        ChatChannelAssociation(id="cca-1", channel_id="ch-1", default_agent_id="agent-x"))
    return p


async def _user_rows(p, chat_id):
    rows = (await p.get_storage(ChatMessage).find(
        Q(ChatMessage).where("chat_id", chat_id).build(),
        OffsetPage(offset=0, length=50))).items
    return [r for r in rows if r.kind == "user_message"]


@pytest.mark.asyncio
async def test_deliver_appends_attributed_and_flips_claimable(tmp_path: Path):
    p = await _provider(tmp_path)
    bus = InMemoryEventBus()
    r = ChatChannelRouter(storage_provider=p, event_bus=bus,
                          gate_inbox=_RecordingGateInbox())
    chat, _ = await r.deliver_message(
        channel_id="ch-1", thread_external_id=None, supports_threads=False,
        sender_name="Alice", text="deploy staging")

    ums = await _user_rows(p, chat.id)
    assert ums[-1].payload["content"] == "[Alice] deploy staging"
    refreshed = await p.get_storage(Chat).get(chat.id)
    assert refreshed.turn_status == "claimable"


@pytest.mark.asyncio
async def test_deliver_wakes_worker_via_claim_engine(tmp_path: Path):
    from primer.int.claim import ClaimKind

    p = await _provider(tmp_path)
    claim = _RecClaim()
    r = ChatChannelRouter(storage_provider=p, event_bus=InMemoryEventBus(),
                          gate_inbox=_RecordingGateInbox(), claim_engine=claim)
    chat, _ = await r.deliver_message(
        channel_id="ch-1", thread_external_id=None, supports_threads=False,
        sender_name="Alice", text="deploy staging")

    assert claim.calls == [(ClaimKind.CHAT, chat.id, 10)]


@pytest.mark.asyncio
async def test_deliver_routes_to_gate_when_pending(tmp_path: Path):
    p = await _provider(tmp_path)
    gate = _RecordingGateInbox()
    r = ChatChannelRouter(storage_provider=p, event_bus=InMemoryEventBus(),
                          gate_inbox=gate)
    chat, _ = await r.resolve_or_create(
        channel_id="ch-1", thread_external_id=None, supports_threads=False)
    chat.pending_tool_call = {"tool_call_id": "tc-1", "mode": "ask_user"}
    await p.get_storage(Chat).update(chat)

    await r.deliver_message(
        channel_id="ch-1", thread_external_id=None, supports_threads=False,
        sender_name="Bob", text="yes")
    assert gate.calls == [(chat.id, {"tool_call_id": "tc-1", "mode": "ask_user"},
                           "yes", "Bob")]


@pytest.mark.asyncio
async def test_deliver_with_media_parts(tmp_path: Path):
    from primer.model.chat import ImagePart
    p = await _provider(tmp_path)
    bus = InMemoryEventBus()
    r = ChatChannelRouter(storage_provider=p, event_bus=bus,
                          gate_inbox=_RecordingGateInbox())
    chat, _ = await r.deliver_message(
        channel_id="ch-1", thread_external_id=None, supports_threads=False,
        sender_name="ana", text="look at this",
        media_parts=[ImagePart(artifact_id="artifact-1", mime_type="image/png")])
    rows = await _user_rows(p, chat.id)
    assert len(rows) == 1
    parts = rows[0].payload["parts"]
    assert parts[0]["type"] == "text"
    assert "[ana] look at this" in parts[0]["text"]
    assert parts[1]["type"] == "image"
    assert parts[1]["artifact_id"] == "artifact-1"


@pytest.mark.asyncio
async def test_deliver_media_only_no_caption(tmp_path: Path):
    from primer.model.chat import ImagePart
    p = await _provider(tmp_path)
    bus = InMemoryEventBus()
    r = ChatChannelRouter(storage_provider=p, event_bus=bus,
                          gate_inbox=_RecordingGateInbox())
    chat, _ = await r.deliver_message(
        channel_id="ch-1", thread_external_id=None, supports_threads=False,
        sender_name="ana", text="",
        media_parts=[ImagePart(artifact_id="artifact-2", mime_type="image/png")])
    rows = await _user_rows(p, chat.id)
    parts = rows[0].payload["parts"]
    # No caption -> a single image part (no empty text part).
    assert [pp["type"] for pp in parts] == ["image"]
