"""Journey: Task A3 (chat-refactor plan) persistent response_format entry point.

Pins the ``PUT /v1/chats/{id}/response_format`` REST surface end-to-end:
setting a schema actually constrains the NEXT turn (the fake LLM captures
the ``response_format`` kwarg the executor streamed with — proof A1
(storage) + A2 (dispatch resolution) + A3 (this endpoint) are wired
together correctly), clearing it removes the constraint, a turn in
flight rejects the mutation with 409, and a malformed schema 422s.

Mirrors ``tests/e2e/test_chat_compact_journey.py``'s in-process pattern
(``create_test_app`` + fake storage + a fake LLM) rather than the live
``primer api`` server the rest of ``tests/e2e/`` requires — no
docker/postgres bring-up needed, just ``PRIMER_RUN_E2E=1`` to lift the
default-skip in ``tests/e2e/conftest.py``.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from pydantic import SecretStr
from starlette.testclient import TestClient as SyncTestClient

from primer.api.app import create_test_app
from primer.model.agent import Agent, AgentModel
from primer.model.chat import Done, StreamEvent, TextDelta
from primer.model.chats import Chat
from primer.model.provider import (
    AnthropicConfig,
    Limits,
    LLMModel,
    LLMProvider,
    LLMProviderType,
)
from tests.conftest import _FakeStorageProvider  # noqa: F401
from tests.api.conftest import fake_provider_registry  # noqa: F401


CHAT_SCHEMA = {"type": "object", "properties": {"x": {"type": "string"}}}
INVALID_SCHEMA = {"type": "nonsense-☠"}


class _RFCapturingLLM:
    """Fake LLM recording the ``response_format`` kwarg each stream call
    is made with — the same shape as ``tests/chat/test_response_format_
    resolution.py``'s fake, reused here to prove the REST->storage->
    dispatch->executor chain threads the schema through for real."""

    def __init__(self, reply_text: str = "ok") -> None:
        self._reply_text = reply_text
        self.calls: list[dict[str, Any]] = []

    async def list_models(self) -> list[str]:
        return ["m"]

    def stream(self, *, model, messages, response_format=None, **kwargs):
        self.calls.append({"model": model, "response_format": response_format})
        return self._stream_impl()

    async def _stream_impl(self):
        yield TextDelta(text=self._reply_text, index=0)
        yield Done(stop_reason="stop", raw_reason="stop")

    async def aclose(self) -> None:
        return None


@pytest.fixture
def fake_llm() -> _RFCapturingLLM:
    return _RFCapturingLLM()


@pytest.fixture
def app(fake_storage_provider, fake_provider_registry, fake_llm) -> FastAPI:
    async def _get_llm(_pid: str) -> _RFCapturingLLM:
        return fake_llm

    fake_provider_registry.get_llm = _get_llm  # type: ignore[assignment]
    return create_test_app(
        storage_provider=fake_storage_provider,
        provider_registry=fake_provider_registry,
        start_chat_worker=True,
    )


@pytest_asyncio.fixture
async def seeded_agent(app: FastAPI) -> Agent:
    llm_storage = app.state.storage_provider.get_storage(LLMProvider)
    await llm_storage.create(
        LLMProvider(
            id="llm-p",
            provider=LLMProviderType.ANTHROPIC,
            models=[LLMModel(name="m", context_length=8000)],
            config=AnthropicConfig(api_key=SecretStr("test-only")),
            limits=Limits(max_concurrency=1),
        ),
    )
    storage = app.state.storage_provider.get_storage(Agent)
    agent = Agent(
        id="ag-chat",
        description="chat agent",
        model=AgentModel(provider_id="llm-p", model_name="m"),
        tools=[],
        system_prompt=[],
    )
    await storage.create(agent)
    return agent


def _login(sclient: SyncTestClient) -> None:
    sclient.post(
        "/v1/auth/register",
        json={"username": "testuser", "password": "testpassword"},
    )
    sclient.post(
        "/v1/auth/login",
        json={"username": "testuser", "password": "testpassword"},
    )


@pytest.mark.asyncio
class TestPersistentResponseFormatJourney:
    async def test_put_sets_schema_and_constrains_next_turn(
        self, app, fake_llm, seeded_agent,
    ) -> None:
        with SyncTestClient(app) as sclient:
            _login(sclient)
            r = sclient.post("/v1/chats", json={"agent_id": "ag-chat"})
            assert r.status_code == 201, r.text
            cid = r.json()["id"]

            put = sclient.put(
                f"/v1/chats/{cid}/response_format", json={"schema": CHAT_SCHEMA},
            )
            assert put.status_code == 200, put.text
            assert put.json()["response_format"] == CHAT_SCHEMA

            with sclient.websocket_connect(f"/v1/chats/{cid}/ws") as ws:
                assert ws.receive_json()["kind"] == "usage"
                ws.send_json({"kind": "user_message", "content": "hi"})
                for _ in range(4):  # user / assistant / done / usage
                    ws.receive_json()

        assert len(fake_llm.calls) == 1
        assert fake_llm.calls[0]["response_format"] == CHAT_SCHEMA

    async def test_put_null_clears_schema(
        self, app, fake_llm, seeded_agent,
    ) -> None:
        with SyncTestClient(app) as sclient:
            _login(sclient)
            r = sclient.post("/v1/chats", json={"agent_id": "ag-chat"})
            cid = r.json()["id"]

            first = sclient.put(
                f"/v1/chats/{cid}/response_format", json={"schema": CHAT_SCHEMA},
            )
            assert first.status_code == 200, first.text

            cleared = sclient.put(
                f"/v1/chats/{cid}/response_format", json={"schema": None},
            )
            assert cleared.status_code == 200, cleared.text
            assert cleared.json()["response_format"] is None

            with sclient.websocket_connect(f"/v1/chats/{cid}/ws") as ws:
                assert ws.receive_json()["kind"] == "usage"
                ws.send_json({"kind": "user_message", "content": "hi again"})
                for _ in range(4):
                    ws.receive_json()

        assert len(fake_llm.calls) == 1
        assert fake_llm.calls[0]["response_format"] is None

    async def test_put_409_when_turn_in_flight(
        self, app, seeded_agent,
    ) -> None:
        chat = Chat(
            id="c-running",
            agent_id="ag-chat",
            created_at=datetime.now(timezone.utc),
            status="active",
            turn_status="running",
        )
        await app.state.storage_provider.get_storage(Chat).create(chat)

        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://t",
        ) as client:
            try:
                await client.post(
                    "/v1/auth/register",
                    json={"username": "testuser", "password": "testpassword"},
                )
            except Exception:  # noqa: BLE001
                pass
            resp = await client.put(
                "/v1/chats/c-running/response_format",
                json={"schema": CHAT_SCHEMA},
            )
        assert resp.status_code == 409, resp.text
        # The chat's response_format was NOT mutated.
        fresh = await app.state.storage_provider.get_storage(Chat).get(
            "c-running",
        )
        assert fresh.response_format is None

    async def test_put_422_for_invalid_schema(
        self, app, seeded_agent,
    ) -> None:
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://t",
        ) as client:
            try:
                await client.post(
                    "/v1/auth/register",
                    json={"username": "testuser", "password": "testpassword"},
                )
            except Exception:  # noqa: BLE001
                pass
            create = await client.post(
                "/v1/chats", json={"agent_id": "ag-chat"},
            )
            cid = create.json()["id"]
            resp = await client.put(
                f"/v1/chats/{cid}/response_format",
                json={"schema": INVALID_SCHEMA},
            )
        assert resp.status_code == 422, resp.text
        fresh = await app.state.storage_provider.get_storage(Chat).get(cid)
        assert fresh.response_format is None

    async def test_put_404_for_unknown_chat(self, app, seeded_agent) -> None:
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://t",
        ) as client:
            try:
                await client.post(
                    "/v1/auth/register",
                    json={"username": "testuser", "password": "testpassword"},
                )
            except Exception:  # noqa: BLE001
                pass
            resp = await client.put(
                "/v1/chats/does-not-exist/response_format",
                json={"schema": CHAT_SCHEMA},
            )
        assert resp.status_code == 404
