"""Journey: Task A8 (chat-refactor plan) — ``GET
/v1/chats/{id}/artifacts/{artifact_id}`` inline artifact fetch (spec §4.4).

Pins the REST surface end-to-end against the in-process app (mirrors
``tests/e2e/test_chat_rewind_journey.py``'s pattern — ``create_test_app`` +
fake storage, no live ``primer api`` server / docker / postgres needed):
fetching an artifact referenced by a chat's own ``tool_result`` row returns
200 with the raw bytes + the stored ``Content-Type``; a foreign artifact id
(one that exists in the store but is never referenced by THIS chat's rows)
404s (chat-local authz); an unknown chat 404s.

Only ``PRIMER_RUN_E2E=1`` lifts the default-skip in ``tests/e2e/conftest.py``;
no live-server bring-up is required for this file.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from primer.api.app import create_test_app
from primer.model.agent import Agent, AgentModel
from primer.model.chats import Chat, ChatMessage
from tests.conftest import _FakeStorageProvider  # noqa: F401
from tests.api.conftest import fake_provider_registry  # noqa: F401


AGENT_ID = "ag-artifact-fetch-journey"


@pytest_asyncio.fixture
async def app(fake_storage_provider, fake_provider_registry) -> FastAPI:
    _app = create_test_app(
        storage_provider=fake_storage_provider,
        provider_registry=fake_provider_registry,
        start_chat_worker=False,
    )
    # Seed the reserved default artifact provider (parity with the real
    # lifespan) so ``artifact_storage_registry.get_default()`` resolves.
    if getattr(_app.state, "seed_artifact_default", None) is not None:
        await _app.state.seed_artifact_default()
    return _app


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
            description="artifact fetch journey",
            model=AgentModel(provider_id="p", model_name="m"),
            tools=[],
            system_prompt=[],
        ),
    )


async def _seed_chat(app: FastAPI, *, chat_id: str) -> Chat:
    chat = Chat(
        id=chat_id,
        agent_id=AGENT_ID,
        created_at=datetime.now(timezone.utc),
        status="active",
        turn_status="idle",  # type: ignore[arg-type]
        last_seq=1,
        next_unprocessed_seq=2,
    )
    await app.state.storage_provider.get_storage(Chat).create(chat)
    return chat


async def _seed_tool_result_row(
    app: FastAPI, *, chat_id: str, seq: int, artifact_id: str,
) -> None:
    msgs = app.state.storage_provider.get_storage(ChatMessage)
    await msgs.create(
        ChatMessage(
            id=ChatMessage.make_id(chat_id, seq),
            chat_id=chat_id,
            seq=seq,
            kind="tool_result",
            payload={
                "id": "tc-1",
                "name": "some_tool",
                "result": "ok",
                "error": False,
                "media": [
                    {
                        "type": "image",
                        "artifact_id": artifact_id,
                        "mime_type": "image/png",
                    },
                ],
            },
            created_at=datetime.now(timezone.utc),
        ),
    )


@pytest.mark.asyncio
class TestChatArtifactFetchJourney:
    async def test_fetch_referenced_artifact_returns_200_with_bytes(
        self, client: AsyncClient, app: FastAPI,
    ) -> None:
        await _seed_agent(app)
        await _seed_chat(app, chat_id="c-artifact")

        store = await app.state.artifact_storage_registry.get_default()
        artifact_id = await store.put(
            data=b"\x89PNG-fake-bytes", mime_type="image/png",
            filename="shot.png",
        )
        await _seed_tool_result_row(
            app, chat_id="c-artifact", seq=2, artifact_id=artifact_id,
        )

        resp = await client.get(f"/v1/chats/c-artifact/artifacts/{artifact_id}")
        assert resp.status_code == 200, resp.text
        assert resp.content == b"\x89PNG-fake-bytes"
        assert resp.headers["content-type"] == "image/png"
        assert resp.headers["content-disposition"] == "inline"

    async def test_fetch_foreign_artifact_id_returns_404(
        self, client: AsyncClient, app: FastAPI,
    ) -> None:
        await _seed_agent(app)
        await _seed_chat(app, chat_id="c-foreign")

        store = await app.state.artifact_storage_registry.get_default()
        # Exists in the store, but never referenced by c-foreign's rows.
        artifact_id = await store.put(
            data=b"belongs-to-another-chat", mime_type="image/png",
        )

        resp = await client.get(f"/v1/chats/c-foreign/artifacts/{artifact_id}")
        assert resp.status_code == 404, resp.text

    async def test_fetch_on_unknown_chat_returns_404(
        self, client: AsyncClient,
    ) -> None:
        resp = await client.get(
            "/v1/chats/does-not-exist/artifacts/some-artifact-id",
        )
        assert resp.status_code == 404, resp.text
