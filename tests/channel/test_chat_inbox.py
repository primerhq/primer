"""ChatResponseInbox: channel gate reply -> chat resume input."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from primer.bus.in_memory import InMemoryEventBus
from primer.channel.chat_inbox import ChatResponseInbox
from primer.model.chats import Chat, ChatMessage
from primer.model.provider import SqliteConfig
from primer.model.storage import OffsetPage, OrderBy
from primer.storage.q import Q
from primer.storage.sqlite import SqliteStorageProvider


async def _provider_with_pending(tmp_path, *, mode):
    p = SqliteStorageProvider(SqliteConfig(path=tmp_path / "r.sqlite"))
    await p.initialize()
    chat = Chat(
        id="chat-1", agent_id="agent-x",
        created_at=datetime.now(timezone.utc), last_seq=1,
        pending_tool_call={"tool_call_id": "tc-1", "mode": mode})
    await p.get_storage(Chat).create(chat)
    await p.get_storage(ChatMessage).create(ChatMessage(
        id=ChatMessage.make_id("chat-1", 1), chat_id="chat-1", seq=1,
        kind="tool_call", payload={"id": "tc-1"},
        created_at=datetime.now(timezone.utc)))
    return p


async def _user_rows(p):
    rows = (await p.get_storage(ChatMessage).find(
        Q(ChatMessage).where("chat_id", "chat-1").build(),
        OffsetPage(offset=0, length=50),
        order_by=[OrderBy(field="seq", direction="asc")])).items
    return [r for r in rows if r.kind == "user_message"]


@pytest.mark.asyncio
async def test_ask_user_reply_appended_and_claimable(tmp_path: Path):
    p = await _provider_with_pending(tmp_path, mode="ask_user")
    bus = InMemoryEventBus()
    inbox = ChatResponseInbox(storage_provider=p, event_bus=bus)
    await inbox.handle_chat_response(
        chat_id="chat-1", pending={"tool_call_id": "tc-1", "mode": "ask_user"},
        text="ship it", sender="Alice")
    ums = await _user_rows(p)
    assert ums[-1].payload["content"] == "ship it"
    assert (await p.get_storage(Chat).get("chat-1")).turn_status == "claimable"


@pytest.mark.asyncio
async def test_approval_decision_maps_to_text(tmp_path: Path):
    p = await _provider_with_pending(tmp_path, mode="approval")
    inbox = ChatResponseInbox(storage_provider=p, event_bus=InMemoryEventBus())
    await inbox.handle_chat_decision(
        chat_id="chat-1", pending={"tool_call_id": "tc-1", "mode": "approval"},
        decision="approved", reason=None, sender="Bob")
    ums = await _user_rows(p)
    assert ums[-1].payload["content"] == "yes"

    await inbox.handle_chat_decision(
        chat_id="chat-1", pending={"tool_call_id": "tc-1", "mode": "approval"},
        decision="rejected", reason="nope", sender="Bob")
    ums2 = await _user_rows(p)
    assert ums2[-1].payload["content"] == "no"
