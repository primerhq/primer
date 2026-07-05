"""Journey: Task A6 (chat-refactor plan) — ``POST /v1/chats/{id}/cancel``
Stop-button endpoint.

Pins the REST surface end-to-end against the in-process app (mirrors
``tests/e2e/test_chat_compact_journey.py`` / ``test_chat_response_format_
journey.py``'s pattern — ``create_test_app`` + fake storage, no live
``primer api`` server / docker / postgres needed): a chat with a turn
in flight (``turn_status="running"``) accepts the cancel request (202,
``cancel_requested_at`` stamped, a ``chat:{id}:cancel`` bus event
observed), an idle chat rejects it (409, nothing running to cancel),
and an unknown chat 404s.

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
from primer.model.chats import Chat
from tests.conftest import _FakeStorageProvider  # noqa: F401
from tests.api.conftest import fake_provider_registry  # noqa: F401


AGENT_ID = "ag-cancel-journey"


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
            description="cancel journey",
            model=AgentModel(provider_id="p", model_name="m"),
            tools=[],
            system_prompt=[],
        ),
    )


async def _seed_chat(
    app: FastAPI, *, chat_id: str, turn_status: str,
) -> Chat:
    chat = Chat(
        id=chat_id,
        agent_id=AGENT_ID,
        created_at=datetime.now(timezone.utc),
        status="active",
        turn_status=turn_status,  # type: ignore[arg-type]
    )
    await app.state.storage_provider.get_storage(Chat).create(chat)
    return chat


@pytest.mark.asyncio
class TestChatCancelJourney:
    async def test_cancel_running_chat_returns_202_and_publishes(
        self, client: AsyncClient, app: FastAPI,
    ) -> None:
        await _seed_agent(app)
        await _seed_chat(app, chat_id="c-running", turn_status="running")

        bus: InMemoryEventBus = app.state.event_bus
        sub = bus.subscribe()

        resp = await client.post("/v1/chats/c-running/cancel")
        assert resp.status_code == 202, resp.text
        assert resp.json() == {"cancel_requested": True}

        fresh = await app.state.storage_provider.get_storage(Chat).get(
            "c-running",
        )
        assert fresh.cancel_requested_at is not None

        event = await sub.__anext__()
        assert event.event_key == "chat:c-running:cancel"
        await sub.aclose()

    async def test_cancel_idle_chat_returns_409(
        self, client: AsyncClient, app: FastAPI,
    ) -> None:
        await _seed_agent(app)
        await _seed_chat(app, chat_id="c-idle", turn_status="idle")

        resp = await client.post("/v1/chats/c-idle/cancel")
        assert resp.status_code == 409, resp.text

        fresh = await app.state.storage_provider.get_storage(Chat).get(
            "c-idle",
        )
        assert fresh.cancel_requested_at is None

    async def test_cancel_unknown_chat_returns_404(
        self, client: AsyncClient, app: FastAPI,
    ) -> None:
        resp = await client.post("/v1/chats/does-not-exist/cancel")
        assert resp.status_code == 404, resp.text
