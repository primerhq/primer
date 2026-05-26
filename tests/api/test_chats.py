"""Tests for the /v1/chats REST + WebSocket surface (M6)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import datetime, timezone
from typing import Any

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from pydantic import SecretStr

from matrix.api.app import create_test_app
from matrix.model.agent import Agent, AgentModel
from matrix.model.chat import Done, Message, StreamEvent, TextDelta
from matrix.model.chats import Chat, ChatMessage
from matrix.model.provider import (
    AnthropicConfig,
    Limits,
    LLMModel,
    LLMProvider,
    LLMProviderType,
)


class _ChatTestFakeLLM:
    """Deterministic fake LLM for the chat tests.

    Replaces the M6 stub's hard-coded ``(stub) heard: <input>`` string
    with a single TextDelta + Done — the same three-frame shape the
    cursor-replay tests assume.
    """

    def __init__(self, reply_text: str = "ok") -> None:
        self._reply_text = reply_text
        self.calls: list[dict[str, Any]] = []

    async def list_models(self):
        return ["m"]

    def stream(
        self,
        *,
        model: str,
        messages: list[Message],
        **kwargs: Any,
    ) -> AsyncIterator[StreamEvent]:
        self.calls.append({"model": model, "messages": list(messages), **kwargs})
        return self._stream_impl()

    async def _stream_impl(self) -> AsyncIterator[StreamEvent]:
        yield TextDelta(text=self._reply_text, index=0)
        yield Done(stop_reason="stop", raw_reason="stop")

    async def aclose(self) -> None:
        return None


@pytest.fixture
def fake_llm() -> _ChatTestFakeLLM:
    return _ChatTestFakeLLM()


@pytest.fixture
def app(
    fake_storage_provider,
    fake_provider_registry,
    fake_llm,
) -> FastAPI:
    # Wire the fake LLM through the provider registry so the chat
    # runner's `provider_registry.get_llm(...)` lookup resolves
    # without spinning up a real provider adapter.
    async def _get_llm(_pid: str) -> _ChatTestFakeLLM:
        return fake_llm

    fake_provider_registry.get_llm = _get_llm  # type: ignore[assignment]
    return create_test_app(
        storage_provider=fake_storage_provider,
        provider_registry=fake_provider_registry,
    )


@pytest_asyncio.fixture
async def client(app):
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://t",
    ) as c:
        yield c


@pytest_asyncio.fixture
async def seeded_agent(app):
    # Chat resolves the agent's pinned LLMProvider + model row at WS
    # connect time, so both the Agent and an LLMProvider row carrying
    # the model name must exist before any user_message frame.
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

    async def test_delete_force_hard_deletes_chat_and_messages(
        self, client, app, seeded_agent,
    ):
        # Seed a chat row + a few message rows under it.
        create = await client.post("/v1/chats", json={"agent_id": "ag-chat"})
        cid = create.json()["id"]
        msgs = app.state.storage_provider.get_storage(ChatMessage)
        for seq, kind in enumerate(["user_message", "assistant_token", "done"], start=1):
            await msgs.create(
                ChatMessage(
                    id=ChatMessage.make_id(cid, seq),
                    chat_id=cid,
                    seq=seq,
                    kind=kind,  # type: ignore[arg-type]
                    payload={},
                    created_at=datetime.now(timezone.utc),
                ),
            )

        # force=true → 200 with delete payload + chat + messages gone.
        resp = await client.delete(f"/v1/chats/{cid}?force=true")
        assert resp.status_code == 200, resp.text
        assert resp.json() == {"id": cid, "deleted": True}

        # Verify storage is empty.
        chats = app.state.storage_provider.get_storage(Chat)
        assert await chats.get(cid) is None
        for seq in (1, 2, 3):
            assert await msgs.get(ChatMessage.make_id(cid, seq)) is None

        # And the resource has actually gone — subsequent GET 404s.
        resp = await client.get(f"/v1/chats/{cid}")
        assert resp.status_code == 404

    async def test_delete_force_works_on_already_ended_chat(
        self, client, app, seeded_agent,
    ):
        """T0765 still pins 409 on a plain DELETE; force=true overrides
        that and removes the chat even when status='ended'."""
        create = await client.post("/v1/chats", json={"agent_id": "ag-chat"})
        cid = create.json()["id"]
        # Soft-end first.
        await client.delete(f"/v1/chats/{cid}")
        # Plain DELETE again → 409 conflict (existing semantic).
        again = await client.delete(f"/v1/chats/{cid}")
        assert again.status_code == 409
        # force=true → succeeds, removes the row.
        forced = await client.delete(f"/v1/chats/{cid}?force=true")
        assert forced.status_code == 200, forced.text
        chats = app.state.storage_provider.get_storage(Chat)
        assert await chats.get(cid) is None

    async def test_delete_force_404s_for_unknown_chat(
        self, client, seeded_agent,
    ):
        resp = await client.delete("/v1/chats/no-such-chat?force=true")
        assert resp.status_code == 404


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
    async def test_ws_send_user_message_streams_back_llm_reply(
        self, app, fake_llm, seeded_agent,
    ):
        from starlette.testclient import TestClient as SyncTestClient

        # Create chat via the REST endpoint using the same app.
        # FastAPI's WS testing uses the sync TestClient because the
        # ASGI WebSocket protocol is easier to drive synchronously.
        fake_llm._reply_text = "hello back"
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
                assert got[1]["delta"] == "hello back"
                assert got[2]["kind"] == "done"
                assert got[2]["stop_reason"] == "stop"
        # The fake LLM should have been invoked exactly once for this
        # one-turn chat, with the user message as the prompt's tail.
        assert len(fake_llm.calls) == 1
        prompt = fake_llm.calls[0]["messages"]
        assert prompt[-1].role == "user"

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

    async def test_ws_accepts_structured_parts_with_image_attachment(
        self, app, fake_llm, seeded_agent,
    ):
        """user_message frames may carry a `parts` list with image /
        document Parts (multimodal). The runner persists the parts
        in the ChatMessage payload and threads the structured Message
        into the LLM prompt — verify both halves end to end."""
        import base64
        from starlette.testclient import TestClient as SyncTestClient

        # 1x1 transparent PNG. The payload doesn't matter for the test;
        # we only check it round-trips through the WS / storage layer.
        png_bytes = base64.b64decode(
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42m"
            "NkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg=="
        )
        png_b64 = base64.b64encode(png_bytes).decode("ascii")

        fake_llm._reply_text = "saw the image"
        with SyncTestClient(app) as sclient:
            r = sclient.post("/v1/chats", json={"agent_id": "ag-chat"})
            cid = r.json()["id"]
            with sclient.websocket_connect(f"/v1/chats/{cid}/ws") as ws:
                ws.send_json({
                    "kind": "user_message",
                    "content": "what's in this?",
                    "parts": [
                        {
                            "type": "image",
                            "data": png_b64,
                            "mime_type": "image/png",
                        },
                    ],
                })
                got = [ws.receive_json() for _ in range(3)]

        # Frame 0: user_message echo carries both flattened content
        # and the structured parts list.
        assert got[0]["kind"] == "user_message"
        assert got[0]["content"] == "what's in this?"
        assert isinstance(got[0]["parts"], list)
        assert len(got[0]["parts"]) == 2  # leading TextPart + ImagePart
        assert got[0]["parts"][0]["type"] == "text"
        assert got[0]["parts"][1]["type"] == "image"
        assert got[0]["parts"][1]["mime_type"] == "image/png"

        # The runner forwarded the structured Message to the LLM.
        prompt = fake_llm.calls[0]["messages"]
        last = prompt[-1]
        assert last.role == "user"
        part_types = [p.type for p in last.parts]
        assert "text" in part_types
        assert "image" in part_types
        # Regression: the binary part's ``data`` must round-trip back
        # to the raw bytes, NOT the base64 string of the bytes. Pydantic
        # doesn't auto-decode base64 strings into ``bytes`` fields by
        # default — the chat Part models add a BeforeValidator for it.
        # Without it the LLM adapter base64-encodes the already-encoded
        # string and OpenAI 400s on ``invalid_union``.
        image_part = next(p for p in last.parts if p.type == "image")
        assert image_part.data == png_bytes, (
            "ImagePart.data must hold the decoded image bytes, not the "
            "base64 string of those bytes"
        )

    async def test_ws_rejects_user_message_with_bad_part(
        self, app, seeded_agent,
    ):
        """A part missing required fields (e.g. an image with no
        data/url/file_id) is rejected with a typed error frame, NOT
        a 500. Protocol surface stays robust under malformed input."""
        from starlette.testclient import TestClient as SyncTestClient

        with SyncTestClient(app) as sclient:
            r = sclient.post("/v1/chats", json={"agent_id": "ag-chat"})
            cid = r.json()["id"]
            with sclient.websocket_connect(f"/v1/chats/{cid}/ws") as ws:
                ws.send_json({
                    "kind": "user_message",
                    "parts": [{"type": "image", "mime_type": "image/png"}],
                })
                msg = ws.receive_json()
                assert msg["kind"] == "error"
                assert "image" in msg["message"].lower() or "data" in msg["message"].lower()

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
