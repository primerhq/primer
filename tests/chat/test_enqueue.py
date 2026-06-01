"""Extracted ``append_user_message`` helper — Plan §3.1.

These tests pin the behaviour of the canonical user_message persist path
that both the WS recv loop (primer.api.routers.chats) and the trigger
dispatcher (Phase 4+) call into. Spec §12.4.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from primer.chat.enqueue import append_user_message
from primer.model.chats import Chat, ChatMessage


def _make_chat(agent_id: str = "ag-1") -> Chat:
    return Chat(
        id="cn-1",
        agent_id=agent_id,
        last_seq=0,
        status="active",
        turn_status="idle",
        created_at=datetime.now(timezone.utc),
    )


@pytest.mark.asyncio
async def test_append_user_message_writes_row(fake_storage_provider):
    chats_storage = fake_storage_provider.get_storage(Chat)
    chat = _make_chat()
    await chats_storage.create(chat)

    msg = await append_user_message(
        chat=chat,
        parts=[{"type": "text", "text": "hello"}],
        storage_provider=fake_storage_provider,
    )

    assert msg.seq == 1
    assert msg.kind == "user_message"
    assert msg.chat_id == "cn-1"
    # Row is keyed by composite id.
    assert msg.id == ChatMessage.make_id("cn-1", 1)
    rehydrated = await chats_storage.get("cn-1")
    assert rehydrated is not None
    assert rehydrated.last_seq == 1
    # Title derived from the flat text on the first turn.
    assert rehydrated.title == "hello"


@pytest.mark.asyncio
async def test_append_user_message_bumps_existing_seq(fake_storage_provider):
    chats_storage = fake_storage_provider.get_storage(Chat)
    chat = _make_chat()
    chat.last_seq = 4
    chat.title = "pre-existing"
    await chats_storage.create(chat)

    msg = await append_user_message(
        chat=chat,
        parts=[{"type": "text", "text": "another"}],
        storage_provider=fake_storage_provider,
    )

    assert msg.seq == 5
    rehydrated = await chats_storage.get("cn-1")
    assert rehydrated.last_seq == 5
    # Title is stamped only on the first turn — never overwritten.
    assert rehydrated.title == "pre-existing"


@pytest.mark.asyncio
async def test_append_user_message_payload_has_parts_and_content(
    fake_storage_provider,
):
    chats_storage = fake_storage_provider.get_storage(Chat)
    chat = _make_chat()
    await chats_storage.create(chat)

    msg = await append_user_message(
        chat=chat,
        parts=[
            {"type": "text", "text": "hi"},
            {"type": "text", "text": "there"},
        ],
        storage_provider=fake_storage_provider,
    )

    assert isinstance(msg.payload, dict)
    assert "parts" in msg.payload
    # Existing router contract: text parts joined by newline.
    assert msg.payload.get("content") == "hi\nthere"
    assert len(msg.payload["parts"]) == 2


@pytest.mark.asyncio
async def test_append_user_message_stamps_attribution(fake_storage_provider):
    chats_storage = fake_storage_provider.get_storage(Chat)
    chat = _make_chat()
    await chats_storage.create(chat)

    msg = await append_user_message(
        chat=chat,
        parts=[{"type": "text", "text": "hello"}],
        storage_provider=fake_storage_provider,
        attribution={
            "trigger_id": "tr-1",
            "subscription_id": "sb-1",
            "fire_id": "fire-tr-1-100",
        },
    )

    assert msg.payload.get("trigger", {}).get("trigger_id") == "tr-1"
    assert msg.payload.get("trigger", {}).get("subscription_id") == "sb-1"
    assert msg.payload.get("trigger", {}).get("fire_id") == "fire-tr-1-100"


@pytest.mark.asyncio
async def test_append_user_message_accepts_pydantic_parts(fake_storage_provider):
    """Router still passes Pydantic TextPart objects; helper must accept those."""
    from primer.model.chat import TextPart

    chats_storage = fake_storage_provider.get_storage(Chat)
    chat = _make_chat()
    await chats_storage.create(chat)

    msg = await append_user_message(
        chat=chat,
        parts=[TextPart(text="hello")],
        storage_provider=fake_storage_provider,
    )

    assert msg.seq == 1
    assert msg.payload.get("content") == "hello"
    rehydrated = await chats_storage.get("cn-1")
    assert rehydrated.title == "hello"
