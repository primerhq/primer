"""REST ``POST /v1/chats/{id}/messages`` contract.

The operator/CLI message-send path. A thin wrapper over the same
``append_user_message`` + wake tail the WebSocket ``_recv_loop`` runs, so
the assertions mirror the WS/append tests: a posted message is appended
as a ``user_message`` row (append-only, next seq), the chat is flipped to
``turn_status='claimable'``, and the ``chat-claimable`` bus event fires so
a worker picks the turn up.

Covers:

* 202 - happy path: row appended, last_seq bumped, turn_status flipped,
  ``chat-claimable`` published.
* 422 - empty frame (neither content nor parts).
* 404 - chat not found.
* 409 - chat ended / a turn already in flight (turn_status='running').
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import datetime, timezone

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from primer.api.app import create_test_app
from primer.model.chats import Chat, ChatMessage


@pytest.fixture
def app(fake_storage_provider, fake_provider_registry) -> FastAPI:
    # Don't start the chat worker - the tests pre-seed chat rows with the
    # exact turn_status they want to exercise (mirrors
    # tests/api/test_compact_endpoints.py).
    return create_test_app(
        storage_provider=fake_storage_provider,
        provider_registry=fake_provider_registry,
        start_chat_worker=False,
    )


@pytest_asyncio.fixture
async def client(app: FastAPI) -> AsyncIterator[AsyncClient]:
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://t",
    ) as c:
        try:
            await c.post(
                "/v1/auth/register",
                json={"username": "testuser", "password": "testpassword"},
            )
        except Exception:
            pass
        yield c


async def _seed_chat(
    app: FastAPI,
    *,
    chat_id: str = "c1",
    agent_id: str = "ag-chat",
    status: str = "active",
    turn_status: str = "idle",
    last_seq: int = 0,
) -> Chat:
    chat = Chat(
        id=chat_id,
        agent_id=agent_id,
        created_at=datetime.now(timezone.utc),
        status=status,  # type: ignore[arg-type]
        turn_status=turn_status,  # type: ignore[arg-type]
        last_seq=last_seq,
    )
    return await app.state.storage_provider.get_storage(Chat).create(chat)


async def _messages(app: FastAPI, chat_id: str) -> list[ChatMessage]:
    from primer.model.storage import OffsetPage, OrderBy
    from primer.storage.q import Q

    storage = app.state.storage_provider.get_storage(ChatMessage)
    page = await storage.find(
        Q(ChatMessage).where("chat_id", chat_id).build(),
        OffsetPage(offset=0, length=200),
        order_by=[OrderBy(field="seq", direction="asc")],
    )
    return list(page.items)


@pytest.mark.asyncio
class TestSendChatMessage:
    async def test_202_appends_row_and_wakes_worker(self, client, app) -> None:
        await _seed_chat(app, chat_id="c1", turn_status="idle", last_seq=0)

        # Subscribe to the bus so we can assert the wake event fires.
        sub = app.state.event_bus.subscribe()

        resp = await client.post(
            "/v1/chats/c1/messages", json={"content": "hello there"},
        )
        assert resp.status_code == 202, resp.text
        body = resp.json()
        # The returned row is the appended user_message at seq 1.
        assert body["kind"] == "user_message"
        assert body["seq"] == 1
        assert body["chat_id"] == "c1"
        assert body["payload"]["content"] == "hello there"

        # Append-only: exactly one new row, keyed by composite id.
        rows = await _messages(app, "c1")
        assert [r.seq for r in rows] == [1]
        assert rows[0].id == ChatMessage.make_id("c1", 1)

        # last_seq bumped + turn flipped to claimable (the wake tail).
        chat = await app.state.storage_provider.get_storage(Chat).get("c1")
        assert chat is not None
        assert chat.last_seq == 1
        assert chat.turn_status == "claimable"

        # The chat-claimable bus event fired for this chat.
        seen_claimable = False
        async for event in sub:
            if (
                event.event_key == "chat-claimable"
                and (event.payload or {}).get("chat_id") == "c1"
            ):
                seen_claimable = True
                break
        await sub.aclose()
        assert seen_claimable

    async def test_202_accepts_structured_parts(self, client, app) -> None:
        await _seed_chat(app, chat_id="c1", turn_status="idle", last_seq=0)
        resp = await client.post(
            "/v1/chats/c1/messages",
            json={"parts": [{"type": "text", "text": "structured"}]},
        )
        assert resp.status_code == 202, resp.text
        body = resp.json()
        assert body["payload"]["content"] == "structured"
        assert len(body["payload"]["parts"]) == 1

    async def test_422_when_frame_is_empty(self, client, app) -> None:
        await _seed_chat(app, chat_id="c1", turn_status="idle", last_seq=0)
        resp = await client.post("/v1/chats/c1/messages", json={})
        assert resp.status_code == 422, resp.text
        # No row appended.
        assert await _messages(app, "c1") == []

    async def test_404_when_chat_not_found(self, client) -> None:
        resp = await client.post(
            "/v1/chats/does-not-exist/messages", json={"content": "hi"},
        )
        assert resp.status_code == 404

    async def test_409_when_chat_ended(self, client, app) -> None:
        await _seed_chat(app, chat_id="c1", status="ended", last_seq=2)
        resp = await client.post(
            "/v1/chats/c1/messages", json={"content": "hi"},
        )
        assert resp.status_code == 409
        # No new row appended into the ended chat.
        assert await _messages(app, "c1") == []

    async def test_409_when_turn_in_flight(self, client, app) -> None:
        await _seed_chat(app, chat_id="c1", turn_status="running", last_seq=2)
        resp = await client.post(
            "/v1/chats/c1/messages", json={"content": "hi"},
        )
        assert resp.status_code == 409
        # The append-only history is untouched while a turn is running.
        assert await _messages(app, "c1") == []
