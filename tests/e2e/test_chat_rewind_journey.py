"""Journey: Task A7 (chat-refactor plan) — ``POST /v1/chats/{id}/rewind``
truncation endpoint (spec R4).

Pins the REST surface end-to-end against the in-process app (mirrors
``tests/e2e/test_chat_cancel_journey.py`` / ``test_chat_compact_journey.
py``'s pattern — ``create_test_app`` + fake storage, no live
``primer api`` server / docker / postgres needed): rewinding to a kept
``user_message`` discards everything after it (200, ``deleted`` count,
``last_seq`` reset, a ``chat:{id}:tick`` bus event observed); a running
turn rejects the rewind (409); an unknown chat 404s; a missing / non-
user_message / at-or-behind-``last_seq`` / at-or-behind-the-latest-
``compaction_marker`` target all 422.

Only ``PRIMER_RUN_E2E=1`` lifts the default-skip in
``tests/e2e/conftest.py``; no live-server bring-up is required for
this file.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from primer.api.app import create_test_app
from primer.bus.in_memory import InMemoryEventBus
from primer.model.agent import Agent, AgentModel
from primer.model.chats import Chat, ChatMessage
from tests.conftest import _FakeStorageProvider  # noqa: F401
from tests.api.conftest import fake_provider_registry  # noqa: F401


AGENT_ID = "ag-rewind-journey"


@pytest.fixture
def app(fake_storage_provider, fake_provider_registry) -> FastAPI:
    return create_test_app(
        storage_provider=fake_storage_provider,
        provider_registry=fake_provider_registry,
        start_chat_worker=False,
    )


@pytest_asyncio.fixture
async def client(app: FastAPI):
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://t",
    ) as c:
        try:
            await c.post(
                "/v1/auth/register",
                json={"username": "testuser", "password": "testpassword"},
            )
        except Exception:  # noqa: BLE001
            pass
        yield c


async def _seed_agent(app: FastAPI) -> None:
    await app.state.storage_provider.get_storage(Agent).create(
        Agent(
            id=AGENT_ID,
            description="rewind journey",
            model=AgentModel(provider_id="p", model_name="m"),
            tools=[],
            system_prompt=[],
        ),
    )


async def _seed_chat(
    app: FastAPI,
    *,
    chat_id: str,
    turn_status: str = "idle",
    last_seq: int = 4,
) -> Chat:
    chat = Chat(
        id=chat_id,
        agent_id=AGENT_ID,
        created_at=datetime.now(timezone.utc),
        status="active",
        turn_status=turn_status,  # type: ignore[arg-type]
        last_seq=last_seq,
        next_unprocessed_seq=last_seq + 1,
    )
    await app.state.storage_provider.get_storage(Chat).create(chat)

    msgs = app.state.storage_provider.get_storage(ChatMessage)
    now = datetime.now(timezone.utc)
    kinds = {1: "user_message", 2: "assistant_token", 3: "user_message", 4: "done"}
    for seq in range(1, last_seq + 1):
        await msgs.create(
            ChatMessage(
                id=ChatMessage.make_id(chat_id, seq),
                chat_id=chat_id,
                seq=seq,
                kind=kinds.get(seq, "assistant_token"),
                payload={},
                created_at=now,
            ),
        )
    return chat


@pytest.mark.asyncio
class TestChatRewindJourney:
    async def test_rewind_valid_target_returns_200_and_discards_tail(
        self, client: AsyncClient, app: FastAPI,
    ) -> None:
        await _seed_agent(app)
        await _seed_chat(app, chat_id="c-rewind", last_seq=4)

        bus: InMemoryEventBus = app.state.event_bus
        sub = bus.subscribe()

        resp = await client.post("/v1/chats/c-rewind/rewind", json={"seq": 1})
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body == {
            "chat_id": "c-rewind", "truncated_to_seq": 1, "deleted": 3,
        }

        msgs = app.state.storage_provider.get_storage(ChatMessage)
        assert await msgs.get(ChatMessage.make_id("c-rewind", 1)) is not None
        assert await msgs.get(ChatMessage.make_id("c-rewind", 2)) is None
        assert await msgs.get(ChatMessage.make_id("c-rewind", 3)) is None
        assert await msgs.get(ChatMessage.make_id("c-rewind", 4)) is None

        fresh = await app.state.storage_provider.get_storage(Chat).get(
            "c-rewind",
        )
        assert fresh.last_seq == 1

        event = await sub.__anext__()
        assert event.event_key == "chat:c-rewind:tick"
        await sub.aclose()

    async def test_rewind_unknown_chat_returns_404(
        self, client: AsyncClient,
    ) -> None:
        resp = await client.post(
            "/v1/chats/does-not-exist/rewind", json={"seq": 1},
        )
        assert resp.status_code == 404, resp.text

    async def test_rewind_running_chat_returns_409(
        self, client: AsyncClient, app: FastAPI,
    ) -> None:
        await _seed_agent(app)
        await _seed_chat(app, chat_id="c-running", turn_status="running")

        resp = await client.post("/v1/chats/c-running/rewind", json={"seq": 1})
        assert resp.status_code == 409, resp.text

        msgs = app.state.storage_provider.get_storage(ChatMessage)
        assert await msgs.get(ChatMessage.make_id("c-running", 4)) is not None

    async def test_rewind_non_user_message_target_returns_422(
        self, client: AsyncClient, app: FastAPI,
    ) -> None:
        await _seed_agent(app)
        await _seed_chat(app, chat_id="c-badkind", last_seq=4)

        # seq=2 is an assistant_token row, not a user_message.
        resp = await client.post(
            "/v1/chats/c-badkind/rewind", json={"seq": 2},
        )
        assert resp.status_code == 422, resp.text

    async def test_rewind_at_or_beyond_last_seq_returns_422(
        self, client: AsyncClient, app: FastAPI,
    ) -> None:
        await _seed_agent(app)
        await _seed_chat(app, chat_id="c-nothing", last_seq=4)

        resp = await client.post(
            "/v1/chats/c-nothing/rewind", json={"seq": 4},
        )
        assert resp.status_code == 422, resp.text

    async def test_rewind_behind_compaction_marker_returns_422(
        self, client: AsyncClient, app: FastAPI,
    ) -> None:
        await _seed_agent(app)
        await _seed_chat(app, chat_id="c-compacted", last_seq=4)

        msgs = app.state.storage_provider.get_storage(ChatMessage)
        await msgs.create(
            ChatMessage(
                id=ChatMessage.make_id("c-compacted", 5),
                chat_id="c-compacted", seq=5, kind="compaction_marker",
                payload={"summary": "rolled up"},
                created_at=datetime.now(timezone.utc),
            ),
        )
        chat_store = app.state.storage_provider.get_storage(Chat)
        chat = await chat_store.get("c-compacted")
        chat.last_seq = 5
        await chat_store.update(chat)

        # seq=1 is a legitimate user_message but sits behind the marker.
        resp = await client.post(
            "/v1/chats/c-compacted/rewind", json={"seq": 1},
        )
        assert resp.status_code == 422, resp.text
