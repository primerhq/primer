"""WS-disconnect-mid-stream resilience tests (Task 9).

These tests verify the new recv/send loop architecture:
  - Worker keeps running after the WS disconnects.
  - Reconnect replays via cursor.
  - interrupt stops the in-flight turn and produces a 'cancelled' row.
  - Multiple user_messages are processed in FIFO order.
"""

from __future__ import annotations

import asyncio
import time

import pytest
import pytest_asyncio
from fastapi import FastAPI
from pydantic import SecretStr

# asyncio is used in the test functions for async stream generators.

from primer.api.app import create_test_app
from primer.api.registries import ProviderRegistry
from primer.model.agent import Agent, AgentModel
from primer.model.provider import (
    AnthropicConfig,
    Limits,
    LLMModel,
    LLMProvider,
    LLMProviderType,
)

from tests.conftest import _FakeStorageProvider, _FakeLLM


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def app(
    fake_storage_provider: _FakeStorageProvider,
    fake_provider_registry: ProviderRegistry,
    fake_llm: _FakeLLM,
) -> FastAPI:
    """App fixture with a real WorkerPool + chat claim loop started.

    Uses start_chat_worker=True so the app lifespan starts the worker
    pool and tick forwarder in the same event loop as the ASGI app
    (inside SyncTestClient).
    """
    async def _get_llm(_pid: str) -> _FakeLLM:
        return fake_llm

    fake_provider_registry.get_llm = _get_llm  # type: ignore[assignment]

    return create_test_app(
        storage_provider=fake_storage_provider,  # type: ignore[arg-type]
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
    yield agent


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_disconnect_mid_stream_then_reconnect_replays(
    app: FastAPI, fake_llm: _FakeLLM, seeded_agent: Agent,
) -> None:
    """Worker turn keeps running after the WS disconnects; the next
    reconnect catches up via cursor replay."""
    from starlette.testclient import TestClient as SyncTestClient

    async def _slow_stream():
        from primer.model.chat import Done, TextDelta
        for t in ("first ", "second ", "third ", "fourth"):
            await asyncio.sleep(0.05)
            yield TextDelta(text=t, index=0)
        yield Done(stop_reason="stop", raw_reason="stop")

    fake_llm._stream_factory = _slow_stream

    with SyncTestClient(app) as sclient:

        sclient.post("/v1/auth/register", json={"username": "testuser", "password": "testpassword"})

        sclient.post("/v1/auth/login", json={"username": "testuser", "password": "testpassword"})
        r = sclient.post("/v1/chats", json={"agent_id": "ag-chat"})
        cid = r.json()["id"]
        # First WS: send + receive a couple frames, then close.
        with sclient.websocket_connect(f"/v1/chats/{cid}/ws") as ws:
            ws.send_json({"kind": "user_message", "content": "hi"})
            ws.receive_json()  # user_message echo
            ws.receive_json()  # first assistant_token
            # Drop the socket here.
        # Wait for the worker to finish on its own.
        time.sleep(0.6)
        # Reconnect with cursor=0; replay should include all four tokens + done.
        with sclient.websocket_connect(f"/v1/chats/{cid}/ws?cursor=0") as ws:
            kinds = []
            for _ in range(6):  # user + 4 tokens + done
                kinds.append(ws.receive_json()["kind"])
            assert kinds == [
                "user_message",
                "assistant_token", "assistant_token", "assistant_token", "assistant_token",
                "done",
            ]


@pytest.mark.asyncio
async def test_interrupt_cancels_in_flight_turn(
    app: FastAPI, fake_llm: _FakeLLM, seeded_agent: Agent,
) -> None:
    """`interrupt` WS frame stops the LLM stream and produces a
    'cancelled' terminal row."""
    from starlette.testclient import TestClient as SyncTestClient

    async def _slow_stream():
        from primer.model.chat import Done, TextDelta
        for t in ("token1 ", "token2 ", "token3 "):
            await asyncio.sleep(0.1)
            yield TextDelta(text=t, index=0)
        yield Done(stop_reason="stop", raw_reason="stop")

    fake_llm._stream_factory = _slow_stream

    with SyncTestClient(app) as sclient:

        sclient.post("/v1/auth/register", json={"username": "testuser", "password": "testpassword"})

        sclient.post("/v1/auth/login", json={"username": "testuser", "password": "testpassword"})
        r = sclient.post("/v1/chats", json={"agent_id": "ag-chat"})
        cid = r.json()["id"]
        with sclient.websocket_connect(f"/v1/chats/{cid}/ws") as ws:
            ws.send_json({"kind": "user_message", "content": "hi"})
            ws.receive_json()  # user_message
            ws.receive_json()  # one token
            ws.send_json({"kind": "interrupt"})
            # Drain until 'cancelled' arrives.
            kinds: list[str] = []
            for _ in range(6):
                try:
                    kinds.append(ws.receive_json()["kind"])
                except Exception:
                    break
                if kinds[-1] == "cancelled":
                    break
            assert "cancelled" in kinds


@pytest.mark.asyncio
async def test_queued_user_messages_processed_fifo(
    app: FastAPI, fake_llm: _FakeLLM, seeded_agent: Agent,
) -> None:
    """Send three user_messages back-to-back; all three are processed
    in seq order even if some arrive before the previous turn completes."""
    from starlette.testclient import TestClient as SyncTestClient

    fake_llm._reply_text = "ok"
    with SyncTestClient(app) as sclient:
        sclient.post("/v1/auth/register", json={"username": "testuser", "password": "testpassword"})
        sclient.post("/v1/auth/login", json={"username": "testuser", "password": "testpassword"})
        r = sclient.post("/v1/chats", json={"agent_id": "ag-chat"})
        cid = r.json()["id"]
        with sclient.websocket_connect(f"/v1/chats/{cid}/ws") as ws:
            for n in range(3):
                ws.send_json({"kind": "user_message", "content": f"q{n}"})
            # Drain — each turn = user + token + done = 3 frames.
            kinds = []
            while True:
                try:
                    msg = ws.receive_json()
                except Exception:
                    break
                kinds.append((msg["kind"], msg.get("content") or msg.get("delta")))
                if kinds.count(("done", None)) == 3:
                    break
        # Three user_messages with contents q0, q1, q2 in order.
        user_contents = [c for k, c in kinds if k == "user_message"]
        assert user_contents == ["q0", "q1", "q2"]
