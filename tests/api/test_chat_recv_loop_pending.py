"""The WS recv loop must DEFER a user_message that arrives while a turn
is active (turn_status in {claimable, running}) onto
``Chat.pending_user_messages`` — NOT allocate it a seq mid-turn (which
is what collided with the executor's assistant_token seq). When the
chat is idle it keeps the current behaviour: append a real seq'd row
and flip turn_status to claimable.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from fastapi import WebSocketDisconnect

from primer.api.routers.chats import _recv_loop
from primer.model.chats import Chat, ChatMessage
from primer.model.storage import OffsetPage, OrderBy, Predicate, FieldRef, Op, Value


class _FakeWS:
    """Yields queued frames then signals a normal disconnect."""

    def __init__(self, frames):
        self._frames = list(frames)
        self.sent: list[dict] = []

    async def receive_json(self):
        if self._frames:
            return self._frames.pop(0)
        raise WebSocketDisconnect()

    async def send_json(self, obj):
        self.sent.append(obj)


class _RecordingBus:
    def __init__(self):
        self.published: list[tuple[str, dict]] = []

    async def publish(self, key, payload):
        self.published.append((key, payload))


async def _all_messages(msgs, chat_id):
    page = await msgs.find(
        Predicate(left=FieldRef(name="chat_id"), op=Op.EQ,
                  right=Value(value=chat_id)),
        OffsetPage(offset=0, length=200),
        order_by=[OrderBy(field="seq", direction="asc")],
    )
    return list(page.items)


@pytest.mark.asyncio
async def test_recv_loop_appends_real_row_when_idle(fake_storage_provider):
    chats = fake_storage_provider.get_storage(Chat)
    msgs = fake_storage_provider.get_storage(ChatMessage)
    await chats.create(Chat(
        id="c1", agent_id="ag", created_at=datetime.now(timezone.utc),
        turn_status="idle", last_seq=0,
    ))
    bus = _RecordingBus()
    ws = _FakeWS([{"kind": "user_message", "content": "hello",
                   "client_msg_id": "cid-1"}])

    await _recv_loop(
        ws, "c1", chats, msgs, bus,
        claim_engine=None, storage_provider=fake_storage_provider,
    )

    rows = await _all_messages(msgs, "c1")
    assert len(rows) == 1
    assert rows[0].kind == "user_message"
    assert rows[0].seq == 1

    chat = await chats.get("c1")
    assert chat.last_seq == 1
    assert chat.turn_status == "claimable"
    assert chat.pending_user_messages == []
    assert any(k == "chat-claimable" for k, _ in bus.published)


@pytest.mark.asyncio
async def test_recv_loop_defers_when_running(fake_storage_provider):
    chats = fake_storage_provider.get_storage(Chat)
    msgs = fake_storage_provider.get_storage(ChatMessage)
    await chats.create(Chat(
        id="c1", agent_id="ag", created_at=datetime.now(timezone.utc),
        turn_status="running", last_seq=3,
    ))
    bus = _RecordingBus()
    ws = _FakeWS([{"kind": "user_message", "content": "queued while busy",
                   "client_msg_id": "cid-2"}])

    await _recv_loop(
        ws, "c1", chats, msgs, bus,
        claim_engine=None, storage_provider=fake_storage_provider,
    )

    # No mid-turn seq allocated: no new ChatMessage row, last_seq unchanged.
    rows = await _all_messages(msgs, "c1")
    assert rows == []

    chat = await chats.get("c1")
    assert chat.last_seq == 3
    assert chat.turn_status == "running"  # not flipped
    # Held on the deferred queue with its client_msg_id for reconcile.
    assert len(chat.pending_user_messages) == 1
    entry = chat.pending_user_messages[0]
    assert entry["client_msg_id"] == "cid-2"
    assert entry["parts"] == [{"type": "text", "text": "queued while busy"}]
    # A deferred message does NOT wake a worker.
    assert all(k != "chat-claimable" for k, _ in bus.published)


@pytest.mark.asyncio
async def test_recv_loop_defers_when_claimable(fake_storage_provider):
    chats = fake_storage_provider.get_storage(Chat)
    msgs = fake_storage_provider.get_storage(ChatMessage)
    await chats.create(Chat(
        id="c1", agent_id="ag", created_at=datetime.now(timezone.utc),
        turn_status="claimable", last_seq=2,
    ))
    bus = _RecordingBus()
    ws = _FakeWS([{"kind": "user_message", "content": "also queued"}])

    await _recv_loop(
        ws, "c1", chats, msgs, bus,
        claim_engine=None, storage_provider=fake_storage_provider,
    )

    rows = await _all_messages(msgs, "c1")
    assert rows == []
    chat = await chats.get("c1")
    assert len(chat.pending_user_messages) == 1
    # client_msg_id is optional on the frame; stored as None when absent.
    assert chat.pending_user_messages[0]["client_msg_id"] is None
