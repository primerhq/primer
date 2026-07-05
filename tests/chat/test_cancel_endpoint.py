"""Task A6 (chat-refactor plan): ``POST /v1/chats/{id}/cancel`` — the REST
Stop-button entry point.

Mirrors the WebSocket ``interrupt`` frame's tail in
:func:`primer.api.routers.chats._recv_loop`: sets
``chat.cancel_requested_at`` and publishes ``chat:{id}:cancel`` on the
event bus, but reachable over REST so the composer's Stop button works
without a live WS connection.

Covers:
* a running chat → 202, ``cancel_requested_at`` stamped, bus event
  observed.
* an idle chat → 409 (nothing to cancel).
* an unknown chat → 404.
* a missing event bus never breaks the REST response (wrapped
  try/except, mirrors ``switch_chat_agent`` / ``compact_chat``).
"""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from primer.bus.in_memory import InMemoryEventBus
from primer.model.chats import Chat
from primer.model.except_ import ConflictError, NotFoundError


def _now():
    return datetime.now(timezone.utc)


def _fake_request(event_bus=None) -> SimpleNamespace:
    return SimpleNamespace(
        app=SimpleNamespace(state=SimpleNamespace(event_bus=event_bus)),
    )


@pytest.mark.asyncio
async def test_cancel_running_chat_returns_202_and_stamps_and_publishes(
    fake_storage_provider,
):
    from primer.api.routers.chats import cancel_chat_turn

    chat_store = fake_storage_provider.get_storage(Chat)
    chat = Chat(
        id="c1", agent_id="ag-1", created_at=_now(), turn_status="running",
    )
    await chat_store.create(chat)

    bus = InMemoryEventBus()
    await bus.initialize()
    sub = bus.subscribe()

    result = await cancel_chat_turn(
        _fake_request(bus), chat_id="c1", sp=fake_storage_provider,
    )

    assert result.cancel_requested is True

    stored = await chat_store.get("c1")
    assert stored.cancel_requested_at is not None

    event = await sub.__anext__()
    assert event.event_key == "chat:c1:cancel"
    await sub.aclose()
    await bus.aclose()


@pytest.mark.asyncio
async def test_cancel_idle_chat_raises_conflict(fake_storage_provider):
    from primer.api.routers.chats import cancel_chat_turn

    chat_store = fake_storage_provider.get_storage(Chat)
    chat = Chat(
        id="c2", agent_id="ag-1", created_at=_now(), turn_status="idle",
    )
    await chat_store.create(chat)

    with pytest.raises(ConflictError):
        await cancel_chat_turn(
            _fake_request(), chat_id="c2", sp=fake_storage_provider,
        )

    stored = await chat_store.get("c2")
    assert stored.cancel_requested_at is None


@pytest.mark.asyncio
async def test_cancel_claimable_chat_raises_conflict(fake_storage_provider):
    """``claimable`` (queued but not yet claimed by a worker) is also
    "nothing running to cancel" — only ``running`` is cancellable."""
    from primer.api.routers.chats import cancel_chat_turn

    chat_store = fake_storage_provider.get_storage(Chat)
    chat = Chat(
        id="c3", agent_id="ag-1", created_at=_now(), turn_status="claimable",
    )
    await chat_store.create(chat)

    with pytest.raises(ConflictError):
        await cancel_chat_turn(
            _fake_request(), chat_id="c3", sp=fake_storage_provider,
        )


@pytest.mark.asyncio
async def test_cancel_unknown_chat_raises_not_found(fake_storage_provider):
    from primer.api.routers.chats import cancel_chat_turn

    with pytest.raises(NotFoundError):
        await cancel_chat_turn(
            _fake_request(), chat_id="nope", sp=fake_storage_provider,
        )


@pytest.mark.asyncio
async def test_cancel_tolerates_missing_event_bus(fake_storage_provider):
    """A ``None`` event_bus must not crash the REST response — mirrors
    the optional-bus guard used elsewhere (``compact_chat`` /
    ``switch_chat_agent``)."""
    from primer.api.routers.chats import cancel_chat_turn

    chat_store = fake_storage_provider.get_storage(Chat)
    chat = Chat(
        id="c4", agent_id="ag-1", created_at=_now(), turn_status="running",
    )
    await chat_store.create(chat)

    result = await cancel_chat_turn(
        _fake_request(None), chat_id="c4", sp=fake_storage_provider,
    )
    assert result.cancel_requested is True
    stored = await chat_store.get("c4")
    assert stored.cancel_requested_at is not None
