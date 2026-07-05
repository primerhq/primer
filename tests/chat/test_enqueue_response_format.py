"""Task A3 (chat-refactor plan): ephemeral ``response_format`` entry point.

Pins :func:`primer.chat.enqueue.append_user_message`'s ``response_format``
kwarg — the EPHEMERAL (this-send-only) structured-output schema stamped
onto a single user_message row's ``payload["response_format"]``, which
A2's dispatch resolution (``tests/chat/test_response_format_resolution.py``)
reads back for that one turn. Distinct from the PERSISTENT
``Chat.response_format`` (A1), which is set via
``PUT /v1/chats/{id}/response_format`` (covered by the e2e journey test).
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from primer.chat.enqueue import append_user_message
from primer.model.chats import Chat, ChatMessage


VALID_SCHEMA = {"type": "object", "properties": {"a": {"type": "string"}}}
INVALID_SCHEMA = {"type": "nonsense-☠"}


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
async def test_append_user_message_stamps_valid_response_format(
    fake_storage_provider,
):
    chats_storage = fake_storage_provider.get_storage(Chat)
    chat = _make_chat()
    await chats_storage.create(chat)

    msg = await append_user_message(
        chat=chat,
        parts=[{"type": "text", "text": "hello"}],
        storage_provider=fake_storage_provider,
        response_format=VALID_SCHEMA,
    )

    assert msg.payload.get("response_format") == VALID_SCHEMA


@pytest.mark.asyncio
async def test_append_user_message_rejects_invalid_response_format(
    fake_storage_provider,
):
    chats_storage = fake_storage_provider.get_storage(Chat)
    chat = _make_chat()
    await chats_storage.create(chat)

    with pytest.raises(ValueError):
        await append_user_message(
            chat=chat,
            parts=[{"type": "text", "text": "hello"}],
            storage_provider=fake_storage_provider,
            response_format=INVALID_SCHEMA,
        )

    # Nothing persisted: seq not bumped, no row written.
    rehydrated = await chats_storage.get("cn-1")
    assert rehydrated.last_seq == 0
    messages_storage = fake_storage_provider.get_storage(ChatMessage)
    row = await messages_storage.get(ChatMessage.make_id("cn-1", 1))
    assert row is None


@pytest.mark.asyncio
async def test_append_user_message_omits_key_when_none(fake_storage_provider):
    chats_storage = fake_storage_provider.get_storage(Chat)
    chat = _make_chat()
    await chats_storage.create(chat)

    msg = await append_user_message(
        chat=chat,
        parts=[{"type": "text", "text": "hello"}],
        storage_provider=fake_storage_provider,
    )

    assert "response_format" not in msg.payload
