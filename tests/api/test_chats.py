"""Tests for the /v1/chats REST + WebSocket surface (M6)."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from matrix.api.app import create_test_app
from matrix.model.agent import Agent, AgentModel
from matrix.model.chats import Chat, ChatMessage


@pytest.fixture
def app(
    fake_storage_provider,
    fake_provider_registry,
    fake_vector_store_registry,
) -> FastAPI:
    return create_test_app(
        storage_provider=fake_storage_provider,
        provider_registry=fake_provider_registry,
        vector_store_registry=fake_vector_store_registry,
    )


@pytest_asyncio.fixture
async def client(app):
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://t",
    ) as c:
        yield c


@pytest_asyncio.fixture
async def seeded_agent(app):
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


# ===========================================================================
# REST: create / get / list / delete
# ===========================================================================


@pytest.mark.asyncio
class TestCreateChat:
    async def test_create_chat_succeeds(self, client, seeded_agent):
        resp = await client.post("/v1/chats", json={"agent_id": "ag-chat"})
        assert resp.status_code == 201
        body = resp.json()
        assert body["agent_id"] == "ag-chat"
        assert body["status"] == "active"
        assert body["last_seq"] == 0
        assert body["id"].startswith("chat-")

    async def test_create_chat_404_when_agent_missing(self, client):
        resp = await client.post("/v1/chats", json={"agent_id": "no-such"})
        assert resp.status_code == 404


@pytest.mark.asyncio
class TestGetChat:
    async def test_get_returns_existing(self, app, client, seeded_agent):
        create = await client.post("/v1/chats", json={"agent_id": "ag-chat"})
        cid = create.json()["id"]
        resp = await client.get(f"/v1/chats/{cid}")
        assert resp.status_code == 200
        assert resp.json()["id"] == cid

    async def test_get_404_for_unknown(self, client):
        resp = await client.get("/v1/chats/does-not-exist")
        assert resp.status_code == 404


@pytest.mark.asyncio
class TestListChats:
    async def test_list_with_agent_filter(self, app, client, seeded_agent):
        # Create one chat against the seeded agent + a second agent.
        ag2 = Agent(
            id="ag-2",
            description="b",
            model=AgentModel(provider_id="llm-p", model_name="m"),
            tools=[],
            system_prompt=[],
        )
        await app.state.storage_provider.get_storage(Agent).create(ag2)
        await client.post("/v1/chats", json={"agent_id": "ag-chat"})
        await client.post("/v1/chats", json={"agent_id": "ag-2"})

        resp = await client.get("/v1/chats?agent_id=ag-chat")
        assert resp.status_code == 200
        body = resp.json()
        assert all(c["agent_id"] == "ag-chat" for c in body["items"])
        assert len(body["items"]) == 1


@pytest.mark.asyncio
class TestEndChat:
    async def test_delete_marks_chat_ended(self, client, seeded_agent):
        create = await client.post("/v1/chats", json={"agent_id": "ag-chat"})
        cid = create.json()["id"]
        resp = await client.delete(f"/v1/chats/{cid}")
        assert resp.status_code == 200
        assert resp.json()["status"] == "ended"

    async def test_delete_twice_409s(self, client, seeded_agent):
        create = await client.post("/v1/chats", json={"agent_id": "ag-chat"})
        cid = create.json()["id"]
        await client.delete(f"/v1/chats/{cid}")
        resp = await client.delete(f"/v1/chats/{cid}")
        assert resp.status_code == 409


# ===========================================================================
# GET /v1/chats/{id}/messages
# ===========================================================================


@pytest.mark.asyncio
class TestListMessages:
    async def test_returns_empty_for_new_chat(self, client, seeded_agent):
        create = await client.post("/v1/chats", json={"agent_id": "ag-chat"})
        cid = create.json()["id"]
        resp = await client.get(f"/v1/chats/{cid}/messages")
        assert resp.status_code == 200
        assert resp.json()["items"] == []

    async def test_after_seq_filters(self, app, client, seeded_agent):
        # Seed messages directly.
        create = await client.post("/v1/chats", json={"agent_id": "ag-chat"})
        cid = create.json()["id"]
        msgs = app.state.storage_provider.get_storage(ChatMessage)
        for i in range(1, 6):
            await msgs.create(
                ChatMessage(
                    id=ChatMessage.make_id(cid, i),
                    chat_id=cid,
                    seq=i,
                    kind="assistant_token",
                    payload={"delta": f"t{i}"},
                    created_at=datetime.now(timezone.utc),
                )
            )
        resp = await client.get(f"/v1/chats/{cid}/messages?after_seq=2")
        assert resp.status_code == 200
        items = resp.json()["items"]
        seqs = [it["seq"] for it in items]
        assert seqs == [3, 4, 5]

    async def test_404_for_unknown_chat(self, client):
        resp = await client.get("/v1/chats/nope/messages")
        assert resp.status_code == 404


# ===========================================================================
# WebSocket: connect + cursor replay + send + receive
# ===========================================================================


@pytest.mark.asyncio
class TestChatWebSocket:
    async def test_ws_send_user_message_streams_back_stub_reply(
        self, app, seeded_agent,
    ):
        from starlette.testclient import TestClient as SyncTestClient

        # Create chat via the REST endpoint using the same app.
        # FastAPI's WS testing uses the sync TestClient because the
        # ASGI WebSocket protocol is easier to drive synchronously.
        with SyncTestClient(app) as sclient:
            r = sclient.post("/v1/chats", json={"agent_id": "ag-chat"})
            assert r.status_code == 201
            cid = r.json()["id"]
            with sclient.websocket_connect(f"/v1/chats/{cid}/ws") as ws:
                ws.send_json({"kind": "user_message", "content": "hi"})
                got: list[dict] = []
                for _ in range(3):  # user_message + assistant_token + done
                    got.append(ws.receive_json())
                assert got[0]["kind"] == "user_message"
                assert got[0]["content"] == "hi"
                assert got[1]["kind"] == "assistant_token"
                assert "(stub) heard: hi" in got[1]["delta"]
                assert got[2]["kind"] == "done"

    async def test_ws_replays_history_when_cursor_below_last_seq(
        self, app, seeded_agent,
    ):
        from starlette.testclient import TestClient as SyncTestClient

        with SyncTestClient(app) as sclient:
            r = sclient.post("/v1/chats", json={"agent_id": "ag-chat"})
            cid = r.json()["id"]
            # Run one turn so the chat has some history.
            with sclient.websocket_connect(f"/v1/chats/{cid}/ws") as ws:
                ws.send_json({"kind": "user_message", "content": "first"})
                for _ in range(3):
                    ws.receive_json()
            # Reconnect with cursor=0 — must replay all 3 messages.
            with sclient.websocket_connect(f"/v1/chats/{cid}/ws?cursor=0") as ws:
                replayed: list[dict] = []
                for _ in range(3):
                    replayed.append(ws.receive_json())
                assert [m["kind"] for m in replayed] == [
                    "user_message", "assistant_token", "done",
                ]
                assert [m["seq"] for m in replayed] == [1, 2, 3]

    async def test_ws_skips_replay_when_cursor_at_last_seq(
        self, app, seeded_agent,
    ):
        from starlette.testclient import TestClient as SyncTestClient

        with SyncTestClient(app) as sclient:
            r = sclient.post("/v1/chats", json={"agent_id": "ag-chat"})
            cid = r.json()["id"]
            with sclient.websocket_connect(f"/v1/chats/{cid}/ws") as ws:
                ws.send_json({"kind": "user_message", "content": "first"})
                for _ in range(3):
                    ws.receive_json()
            # Reconnect with cursor=3 — no replay; the client supplies
            # the next user_message and gets a fresh stream.
            with sclient.websocket_connect(f"/v1/chats/{cid}/ws?cursor=3") as ws:
                ws.send_json({"kind": "user_message", "content": "second"})
                got = [ws.receive_json() for _ in range(3)]
                # seqs continue from 4: user_message=4, token=5, done=6
                assert [m["seq"] for m in got] == [4, 5, 6]

    async def test_ws_ping_returns_pong(self, app, seeded_agent):
        from starlette.testclient import TestClient as SyncTestClient

        with SyncTestClient(app) as sclient:
            r = sclient.post("/v1/chats", json={"agent_id": "ag-chat"})
            cid = r.json()["id"]
            with sclient.websocket_connect(f"/v1/chats/{cid}/ws") as ws:
                ws.send_json({"kind": "ping"})
                msg = ws.receive_json()
                assert msg == {"kind": "pong"}

    async def test_ws_rejects_empty_user_message(self, app, seeded_agent):
        from starlette.testclient import TestClient as SyncTestClient

        with SyncTestClient(app) as sclient:
            r = sclient.post("/v1/chats", json={"agent_id": "ag-chat"})
            cid = r.json()["id"]
            with sclient.websocket_connect(f"/v1/chats/{cid}/ws") as ws:
                ws.send_json({"kind": "user_message", "content": "   "})
                msg = ws.receive_json()
                assert msg["kind"] == "error"

    async def test_ws_rejects_unknown_kind(self, app, seeded_agent):
        from starlette.testclient import TestClient as SyncTestClient

        with SyncTestClient(app) as sclient:
            r = sclient.post("/v1/chats", json={"agent_id": "ag-chat"})
            cid = r.json()["id"]
            with sclient.websocket_connect(f"/v1/chats/{cid}/ws") as ws:
                ws.send_json({"kind": "totally_bogus"})
                msg = ws.receive_json()
                assert msg["kind"] == "error"

    async def test_ws_closes_for_unknown_chat(self, app, seeded_agent):
        from starlette.testclient import TestClient as SyncTestClient
        from starlette.websockets import WebSocketDisconnect

        with SyncTestClient(app) as sclient:
            with pytest.raises(WebSocketDisconnect) as excinfo:
                with sclient.websocket_connect("/v1/chats/no-such/ws") as ws:
                    ws.receive_json()
            # 4404 is the application-defined "not found" close code
            assert excinfo.value.code == 4404

    async def test_ws_closes_for_ended_chat(self, app, client, seeded_agent):
        from starlette.testclient import TestClient as SyncTestClient
        from starlette.websockets import WebSocketDisconnect

        with SyncTestClient(app) as sclient:
            r = sclient.post("/v1/chats", json={"agent_id": "ag-chat"})
            cid = r.json()["id"]
            sclient.delete(f"/v1/chats/{cid}")
            with pytest.raises(WebSocketDisconnect) as excinfo:
                with sclient.websocket_connect(f"/v1/chats/{cid}/ws") as ws:
                    ws.receive_json()
            assert excinfo.value.code == 4410


# ===========================================================================
# ChatMessage.make_id sanity
# ===========================================================================


def test_chat_message_id_format():
    assert ChatMessage.make_id("chat-x", 1).startswith("chat-x:0000")
    a = ChatMessage.make_id("c", 2)
    b = ChatMessage.make_id("c", 10)
    # Zero-pad keeps lexicographic order in sync with numeric order
    # — important for cursor pagination by id.
    assert a < b
