"""Task A7 (chat-refactor plan): rewind truncation (spec R4).

Covers both halves of the feature:

* :func:`primer.chat.rewind.truncate_chat_after` — the storage-mutation
  helper: deletes every row with ``seq > target_seq``, keeps the rest,
  resets ``last_seq``, clamps ``next_unprocessed_seq``, and clears any
  pending gate / cancel flag.
* ``POST /v1/chats/{id}/rewind`` (:func:`primer.api.routers.chats.rewind_chat`)
  — the REST guard surface: 404 unknown chat, 409 while running, 422 for
  a missing/non-user_message target, 422 for a target at or behind the
  latest ``compaction_marker``, and 422 when there's nothing to discard
  (``seq >= last_seq``). On success it keeps the selected message,
  discards everything after it, and publishes a ``chat:{id}:tick``.
"""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from primer.bus.in_memory import InMemoryEventBus
from primer.chat.rewind import truncate_chat_after
from primer.model.chats import Chat, ChatMessage
from primer.model.except_ import ConflictError, NotFoundError, ValidationError


def _now():
    return datetime.now(timezone.utc)


def _fake_request(event_bus=None) -> SimpleNamespace:
    return SimpleNamespace(
        app=SimpleNamespace(state=SimpleNamespace(event_bus=event_bus)),
    )


async def _seed_chat(
    fake_storage_provider,
    *,
    chat_id: str = "c1",
    agent_id: str = "ag-1",
    turn_status: str = "idle",
    last_seq: int = 4,
    next_unprocessed_seq: int = 5,
    pending_tool_call: dict | None = None,
    pending_handoff: str | None = None,
    cancel_requested_at=None,
) -> Chat:
    chat_store = fake_storage_provider.get_storage(Chat)
    chat = Chat(
        id=chat_id,
        agent_id=agent_id,
        created_at=_now(),
        turn_status=turn_status,  # type: ignore[arg-type]
        last_seq=last_seq,
        next_unprocessed_seq=next_unprocessed_seq,
        pending_tool_call=pending_tool_call,
        pending_handoff=pending_handoff,
        cancel_requested_at=cancel_requested_at,
    )
    await chat_store.create(chat)

    msgs = fake_storage_provider.get_storage(ChatMessage)
    kinds = {
        1: "user_message",
        2: "assistant_token",
        3: "user_message",
        4: "done",
    }
    for seq in range(1, last_seq + 1):
        await msgs.create(
            ChatMessage(
                id=ChatMessage.make_id(chat_id, seq),
                chat_id=chat_id,
                seq=seq,
                kind=kinds.get(seq, "assistant_token"),
                payload={},
                created_at=_now(),
            ),
        )
    return chat


# ===========================================================================
# truncate_chat_after — unit tests
# ===========================================================================


@pytest.mark.asyncio
async def test_truncate_deletes_after_target_and_keeps_rest(
    fake_storage_provider,
):
    chat = await _seed_chat(fake_storage_provider, last_seq=4, next_unprocessed_seq=5)

    deleted = await truncate_chat_after(
        chat, 2, storage_provider=fake_storage_provider,
    )
    assert deleted == 2

    msgs = fake_storage_provider.get_storage(ChatMessage)
    assert await msgs.get(ChatMessage.make_id("c1", 1)) is not None
    assert await msgs.get(ChatMessage.make_id("c1", 2)) is not None
    assert await msgs.get(ChatMessage.make_id("c1", 3)) is None
    assert await msgs.get(ChatMessage.make_id("c1", 4)) is None


@pytest.mark.asyncio
async def test_truncate_resets_last_seq_and_clamps_next_unprocessed_seq(
    fake_storage_provider,
):
    chat = await _seed_chat(fake_storage_provider, last_seq=4, next_unprocessed_seq=5)

    await truncate_chat_after(chat, 2, storage_provider=fake_storage_provider)

    chat_store = fake_storage_provider.get_storage(Chat)
    stored = await chat_store.get("c1")
    assert stored.last_seq == 2
    assert stored.next_unprocessed_seq == 2


@pytest.mark.asyncio
async def test_truncate_does_not_raise_next_unprocessed_seq(fake_storage_provider):
    """If ``next_unprocessed_seq`` was already behind ``target_seq``, it
    stays put — clamp is a min(), never a bump."""
    chat = await _seed_chat(
        fake_storage_provider, last_seq=4, next_unprocessed_seq=1,
    )

    await truncate_chat_after(chat, 3, storage_provider=fake_storage_provider)

    chat_store = fake_storage_provider.get_storage(Chat)
    stored = await chat_store.get("c1")
    assert stored.next_unprocessed_seq == 1


@pytest.mark.asyncio
async def test_truncate_clears_pending_gates_and_cancel_flag(fake_storage_provider):
    chat = await _seed_chat(
        fake_storage_provider,
        last_seq=4,
        next_unprocessed_seq=5,
        pending_tool_call={"tool_call_id": "tc1", "mode": "ask_user"},
        pending_handoff="some prompt",
        cancel_requested_at=_now(),
    )

    await truncate_chat_after(chat, 2, storage_provider=fake_storage_provider)

    chat_store = fake_storage_provider.get_storage(Chat)
    stored = await chat_store.get("c1")
    assert stored.pending_tool_call is None
    assert stored.pending_handoff is None
    assert stored.cancel_requested_at is None


@pytest.mark.asyncio
async def test_truncate_returns_zero_when_target_is_last_seq(fake_storage_provider):
    chat = await _seed_chat(fake_storage_provider, last_seq=4, next_unprocessed_seq=5)

    deleted = await truncate_chat_after(chat, 4, storage_provider=fake_storage_provider)
    assert deleted == 0

    msgs = fake_storage_provider.get_storage(ChatMessage)
    assert await msgs.get(ChatMessage.make_id("c1", 4)) is not None


# ===========================================================================
# POST /v1/chats/{id}/rewind — endpoint guard surface
# ===========================================================================


@pytest.mark.asyncio
async def test_rewind_valid_target_keeps_row_and_discards_tail(
    fake_storage_provider,
):
    from primer.api.routers.chats import ChatRewindBody, rewind_chat

    await _seed_chat(fake_storage_provider, last_seq=4, next_unprocessed_seq=5)

    bus = InMemoryEventBus()
    await bus.initialize()
    sub = bus.subscribe()

    result = await rewind_chat(
        ChatRewindBody(seq=1),
        _fake_request(bus),
        chat_id="c1",
        sp=fake_storage_provider,
    )

    assert result.chat_id == "c1"
    assert result.truncated_to_seq == 1
    assert result.deleted == 3

    msgs = fake_storage_provider.get_storage(ChatMessage)
    assert await msgs.get(ChatMessage.make_id("c1", 1)) is not None
    assert await msgs.get(ChatMessage.make_id("c1", 2)) is None
    assert await msgs.get(ChatMessage.make_id("c1", 3)) is None
    assert await msgs.get(ChatMessage.make_id("c1", 4)) is None

    chat_store = fake_storage_provider.get_storage(Chat)
    stored = await chat_store.get("c1")
    assert stored.last_seq == 1

    event = await sub.__anext__()
    assert event.event_key == "chat:c1:tick"
    await sub.aclose()
    await bus.aclose()


@pytest.mark.asyncio
async def test_rewind_unknown_chat_raises_not_found(fake_storage_provider):
    from primer.api.routers.chats import ChatRewindBody, rewind_chat

    with pytest.raises(NotFoundError):
        await rewind_chat(
            ChatRewindBody(seq=1),
            _fake_request(),
            chat_id="nope",
            sp=fake_storage_provider,
        )


@pytest.mark.asyncio
async def test_rewind_while_running_raises_conflict(fake_storage_provider):
    from primer.api.routers.chats import ChatRewindBody, rewind_chat

    await _seed_chat(
        fake_storage_provider, turn_status="running", last_seq=4,
        next_unprocessed_seq=5,
    )

    with pytest.raises(ConflictError):
        await rewind_chat(
            ChatRewindBody(seq=1),
            _fake_request(),
            chat_id="c1",
            sp=fake_storage_provider,
        )

    msgs = fake_storage_provider.get_storage(ChatMessage)
    assert await msgs.get(ChatMessage.make_id("c1", 4)) is not None


@pytest.mark.asyncio
async def test_rewind_missing_target_row_raises_validation_error(
    fake_storage_provider,
):
    from primer.api.routers.chats import ChatRewindBody, rewind_chat

    await _seed_chat(fake_storage_provider, last_seq=4, next_unprocessed_seq=5)

    with pytest.raises(ValidationError):
        await rewind_chat(
            ChatRewindBody(seq=99),
            _fake_request(),
            chat_id="c1",
            sp=fake_storage_provider,
        )


@pytest.mark.asyncio
async def test_rewind_non_user_message_target_raises_validation_error(
    fake_storage_provider,
):
    from primer.api.routers.chats import ChatRewindBody, rewind_chat

    await _seed_chat(fake_storage_provider, last_seq=4, next_unprocessed_seq=5)

    # seq=2 is an assistant_token row, not a user_message.
    with pytest.raises(ValidationError):
        await rewind_chat(
            ChatRewindBody(seq=2),
            _fake_request(),
            chat_id="c1",
            sp=fake_storage_provider,
        )


@pytest.mark.asyncio
async def test_rewind_at_or_behind_last_seq_raises_validation_error(
    fake_storage_provider,
):
    from primer.api.routers.chats import ChatRewindBody, rewind_chat

    await _seed_chat(fake_storage_provider, last_seq=4, next_unprocessed_seq=5)

    # seq == last_seq -> nothing to discard.
    with pytest.raises(ValidationError):
        await rewind_chat(
            ChatRewindBody(seq=4),
            _fake_request(),
            chat_id="c1",
            sp=fake_storage_provider,
        )


@pytest.mark.asyncio
async def test_rewind_at_or_behind_compaction_marker_raises_validation_error(
    fake_storage_provider,
):
    from primer.api.routers.chats import ChatRewindBody, rewind_chat

    await _seed_chat(fake_storage_provider, last_seq=4, next_unprocessed_seq=5)

    msgs = fake_storage_provider.get_storage(ChatMessage)
    # A compaction_marker lands at seq=5 (replacing seqs 1-4). Target
    # seq=1 is a legitimate user_message but sits behind the marker, so
    # it must be rejected purely on the compaction guard.
    await msgs.create(
        ChatMessage(
            id=ChatMessage.make_id("c1", 5),
            chat_id="c1",
            seq=5,
            kind="compaction_marker",
            payload={"summary": "..."},
            created_at=_now(),
        ),
    )
    chat_store = fake_storage_provider.get_storage(Chat)
    chat = await chat_store.get("c1")
    chat.last_seq = 5
    await chat_store.update(chat)

    with pytest.raises(ValidationError):
        await rewind_chat(
            ChatRewindBody(seq=1),
            _fake_request(),
            chat_id="c1",
            sp=fake_storage_provider,
        )
