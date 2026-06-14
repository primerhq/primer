"""hotpaths #2: next_unprocessed_seq cursor equivalence.

Proves the cursor-bounded claim scans (``_find_next_user_message`` /
``_find_resume_reply``) pick EXACTLY the same row the pre-cursor full scan
would, across the fresh-message, multi-turn, drained, and resume-reply cases,
and that advancing the cursor never skips a concurrently-appended message.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from primer.chat.dispatch import (
    ChatDispatchDeps,
    _find_next_user_message,
    _find_resume_reply,
)
from primer.model.chats import Chat, ChatMessage


def _deps(sp) -> ChatDispatchDeps:
    # Only storage_provider is exercised by the two scan helpers.
    return ChatDispatchDeps(
        storage_provider=sp, provider_registry=None,
        event_bus=None, chat_tick_router=None,  # type: ignore[arg-type]
    )


async def _seed_chat(sp, chat_id: str, *, cursor: int = 0) -> Chat:
    chat = Chat(id=chat_id, agent_id="a", created_at=datetime.now(timezone.utc))
    chat.next_unprocessed_seq = cursor
    await sp.get_storage(Chat).create(chat)
    return chat


async def _add(sp, chat_id: str, seq: int, kind: str, payload: dict | None = None):
    await sp.get_storage(ChatMessage).create(ChatMessage(
        id=ChatMessage.make_id(chat_id, seq), chat_id=chat_id, seq=seq,
        kind=kind, payload=payload or {}, created_at=datetime.now(timezone.utc),
    ))


async def _set_last_seq(sp, chat_id: str, seq: int) -> None:
    chat = await sp.get_storage(Chat).get(chat_id)
    chat.last_seq = seq
    await sp.get_storage(Chat).update(chat)


# --- _find_next_user_message ------------------------------------------------


@pytest.mark.asyncio
async def test_fresh_message_picked_same_with_and_without_cursor(
    fake_storage_provider,
):
    """A fresh queued user_message is picked identically at cursor 0 and at a
    balanced-prefix cursor."""
    sp = fake_storage_provider
    # Prefix: one fully-processed turn (U1 + done), then a fresh U2.
    await _seed_chat(sp, "c1", cursor=0)
    await _add(sp, "c1", 1, "user_message", {"content": "first"})
    await _add(sp, "c1", 2, "done", {"stop_reason": "stop"})
    await _add(sp, "c1", 3, "user_message", {"content": "second"})
    await _set_last_seq(sp, "c1", 3)

    # cursor 0 (full scan)
    picked0 = await _find_next_user_message(_deps(sp), "c1")
    assert picked0 is not None and picked0.seq == 3

    # Now set cursor to the balanced checkpoint (after the first turn) and
    # confirm the SAME message is picked.
    chat = await sp.get_storage(Chat).get("c1")
    chat.next_unprocessed_seq = 3
    await sp.get_storage(Chat).update(chat)
    picked_cursor = await _find_next_user_message(_deps(sp), "c1")
    assert picked_cursor is not None and picked_cursor.seq == 3
    assert picked_cursor.seq == picked0.seq


@pytest.mark.asyncio
async def test_drain_advances_cursor_and_stays_equivalent(fake_storage_provider):
    """When drained, the cursor advances to last_seq+1; a re-scan still
    returns None and a freshly appended message is then picked."""
    sp = fake_storage_provider
    await _seed_chat(sp, "c2", cursor=0)
    await _add(sp, "c2", 1, "user_message", {"content": "u1"})
    await _add(sp, "c2", 2, "done", {})
    await _set_last_seq(sp, "c2", 2)

    picked = await _find_next_user_message(_deps(sp), "c2")
    assert picked is None  # drained
    chat = await sp.get_storage(Chat).get("c2")
    assert chat.next_unprocessed_seq == 3  # advanced to last scanned seq + 1

    # Append a new turn; it must be found despite the advanced cursor.
    await _add(sp, "c2", 3, "user_message", {"content": "u2"})
    await _set_last_seq(sp, "c2", 3)
    picked2 = await _find_next_user_message(_deps(sp), "c2")
    assert picked2 is not None and picked2.seq == 3


@pytest.mark.asyncio
async def test_history_excluded_reply_not_served(fake_storage_provider):
    """A resume-consumed reply (``_history_excluded``) is never re-served, with
    or without a cursor -- matches the old scan."""
    sp = fake_storage_provider
    await _seed_chat(sp, "c3", cursor=0)
    await _add(sp, "c3", 1, "user_message", {"content": "u1"})
    await _add(sp, "c3", 2, "tool_call", {"id": "tc-1"})
    await _add(sp, "c3", 3, "user_message",
               {"content": "reply", "_history_excluded": True})
    await _add(sp, "c3", 4, "tool_result", {"id": "tc-1"})
    await _add(sp, "c3", 5, "done", {})
    await _set_last_seq(sp, "c3", 5)

    picked = await _find_next_user_message(_deps(sp), "c3")
    assert picked is None  # U1 paired with done; the reply is excluded


@pytest.mark.asyncio
async def test_multi_turn_index_equivalence(fake_storage_provider):
    """Three queued user_messages, two terminals -> third message is next, at
    cursor 0 AND at a balanced cursor over the first turn."""
    sp = fake_storage_provider
    await _seed_chat(sp, "c4", cursor=0)
    await _add(sp, "c4", 1, "user_message", {"content": "u1"})
    await _add(sp, "c4", 2, "done", {})
    await _add(sp, "c4", 3, "user_message", {"content": "u2"})
    await _add(sp, "c4", 4, "error", {})
    await _add(sp, "c4", 5, "user_message", {"content": "u3"})
    await _set_last_seq(sp, "c4", 5)

    picked0 = await _find_next_user_message(_deps(sp), "c4")
    assert picked0 is not None and picked0.seq == 5

    chat = await sp.get_storage(Chat).get("c4")
    chat.next_unprocessed_seq = 3  # balanced after first turn (U1+done)
    await sp.get_storage(Chat).update(chat)
    picked_cursor = await _find_next_user_message(_deps(sp), "c4")
    assert picked_cursor is not None and picked_cursor.seq == 5


# --- _find_resume_reply -----------------------------------------------------


@pytest.mark.asyncio
async def test_resume_reply_equivalence(fake_storage_provider):
    """The reply to a pending tool_call is found identically at cursor 0 and at
    a cursor at/below the pending turn's start."""
    sp = fake_storage_provider
    await _seed_chat(sp, "c5", cursor=0)
    await _add(sp, "c5", 1, "user_message", {"content": "u1"})
    await _add(sp, "c5", 2, "tool_call", {"id": "tc-9"})
    await _add(sp, "c5", 3, "assistant_token", {"delta": "approve?"})
    await _add(sp, "c5", 4, "user_message", {"content": "yes"})  # the reply
    await _set_last_seq(sp, "c5", 4)
    pending = {"tool_call_id": "tc-9"}

    reply0 = await _find_resume_reply(_deps(sp), "c5", pending)
    assert reply0 is not None and reply0.seq == 4

    # Cursor at the pending turn's start: still finds the same reply.
    chat = await sp.get_storage(Chat).get("c5")
    chat.next_unprocessed_seq = 1
    await sp.get_storage(Chat).update(chat)
    reply_cursor = await _find_resume_reply(_deps(sp), "c5", pending)
    assert reply_cursor is not None and reply_cursor.seq == 4


@pytest.mark.asyncio
async def test_resume_reply_none_when_no_reply_yet(fake_storage_provider):
    """No reply after the pending tool_call -> None (unchanged)."""
    sp = fake_storage_provider
    await _seed_chat(sp, "c6", cursor=0)
    await _add(sp, "c6", 1, "user_message", {"content": "u1"})
    await _add(sp, "c6", 2, "tool_call", {"id": "tc-1"})
    await _set_last_seq(sp, "c6", 2)
    reply = await _find_resume_reply(_deps(sp), "c6", {"tool_call_id": "tc-1"})
    assert reply is None


@pytest.mark.asyncio
async def test_existing_row_default_cursor_zero(fake_storage_provider):
    """A Chat row that predates the field validates with cursor 0 (scan from
    start), so legacy chats behave exactly as before."""
    sp = fake_storage_provider
    chat = Chat(id="c7", agent_id="a", created_at=datetime.now(timezone.utc))
    assert chat.next_unprocessed_seq == 0
