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

from primer.api.app import create_test_app
from primer.model.agent import Agent, AgentModel
from primer.model.chat import Done, Message, StreamEvent, TextDelta
from primer.model.chats import Chat, ChatMessage
from primer.model.provider import (
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

    When ``raise_on_stream`` is set, the fake re-raises that exception
    instead of yielding events — used to exercise the chat runner's
    error-translation path for upstream provider failures.
    """

    def __init__(self, reply_text: str = "ok") -> None:
        self._reply_text = reply_text
        self.raise_on_stream: BaseException | None = None
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
        if self.raise_on_stream is not None:
            raise self.raise_on_stream
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
        start_chat_worker=True,
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
class TestChatTitle:
    """Chat.title is stamped from the first user_message so the chat
    list shows a human-readable label instead of the opaque chat id."""

    async def test_title_is_none_on_fresh_chat(self, client, seeded_agent):
        r = await client.post("/v1/chats", json={"agent_id": "ag-chat"})
        body = r.json()
        assert body["title"] is None

    async def test_first_user_message_stamps_title(
        self, app, fake_llm, seeded_agent,
    ):
        from starlette.testclient import TestClient as SyncTestClient

        with SyncTestClient(app) as sclient:
            r = sclient.post("/v1/chats", json={"agent_id": "ag-chat"})
            cid = r.json()["id"]
            with sclient.websocket_connect(f"/v1/chats/{cid}/ws") as ws:
                ws.send_json({"kind": "user_message", "content": "Hello agent, can you help me?"})
                for _ in range(3):  # user / assistant / done
                    ws.receive_json()
            # Fetch the chat row back through REST — title now stamped.
            r2 = sclient.get(f"/v1/chats/{cid}")
            assert r2.json()["title"] == "Hello agent, can you help me?"

    async def test_title_truncates_long_text(
        self, app, fake_llm, seeded_agent,
    ):
        from starlette.testclient import TestClient as SyncTestClient

        long_text = (
            "this is a really really long opening message that goes on "
            "and on and on and far exceeds the 80-character title cap "
            "matrix imposes on the chat list"
        )
        with SyncTestClient(app) as sclient:
            r = sclient.post("/v1/chats", json={"agent_id": "ag-chat"})
            cid = r.json()["id"]
            with sclient.websocket_connect(f"/v1/chats/{cid}/ws") as ws:
                ws.send_json({"kind": "user_message", "content": long_text})
                for _ in range(3):
                    ws.receive_json()
            title = sclient.get(f"/v1/chats/{cid}").json()["title"]
        assert title.endswith("…")
        assert len(title) <= 80
        # The truncation should land on a word boundary, not mid-word,
        # when one is available in the back third of the budget.
        assert " " in title  # multi-word survived
        assert not title.replace("…", "").endswith(" ")

    async def test_title_not_overwritten_on_subsequent_turns(
        self, app, fake_llm, seeded_agent,
    ):
        from starlette.testclient import TestClient as SyncTestClient

        with SyncTestClient(app) as sclient:
            r = sclient.post("/v1/chats", json={"agent_id": "ag-chat"})
            cid = r.json()["id"]
            with sclient.websocket_connect(f"/v1/chats/{cid}/ws") as ws:
                ws.send_json({"kind": "user_message", "content": "first message"})
                for _ in range(3):
                    ws.receive_json()
                ws.send_json({"kind": "user_message", "content": "different topic now"})
                for _ in range(3):
                    ws.receive_json()
            # Title still reflects the first message — operators don't
            # want the list label drifting as the conversation evolves.
            assert sclient.get(f"/v1/chats/{cid}").json()["title"] == "first message"

    async def test_title_falls_back_for_attachment_only_message(
        self, app, fake_llm, seeded_agent,
    ):
        """When the very first user_message carries only a file (no
        text), the title is a generic placeholder so the chat list
        still shows something readable."""
        import base64
        from starlette.testclient import TestClient as SyncTestClient

        png_b64 = base64.b64encode(b"\x89PNG\r\n\x1a\nfake").decode()

        with SyncTestClient(app) as sclient:
            r = sclient.post("/v1/chats", json={"agent_id": "ag-chat"})
            cid = r.json()["id"]
            with sclient.websocket_connect(f"/v1/chats/{cid}/ws") as ws:
                ws.send_json({
                    "kind": "user_message",
                    "parts": [{"type": "image", "data": png_b64, "mime_type": "image/png"}],
                })
                for _ in range(3):
                    ws.receive_json()
            assert sclient.get(f"/v1/chats/{cid}").json()["title"] == "[attachment]"


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

    async def test_ws_attachment_rejected_by_model_surfaces_friendly_error(
        self, app, fake_llm, seeded_agent,
    ):
        """When the upstream model rejects an attachment (LM Studio,
        text-only cloud model, etc.), the chat surface MUST translate
        the cryptic '400 invalid_union' into an operator-friendly
        message instead of leaking the raw provider error string.

        Reproduces the bug seen with Gemma 4 in LM Studio: the model
        returns ``Invalid type for 'input'`` because it can't accept
        ``input_file`` content."""
        import base64
        from starlette.testclient import TestClient as SyncTestClient

        from primer.model.except_ import BadRequestError

        # Make the fake LLM reject the call with the same shape
        # OpenAI / LM Studio return when content includes a file/image
        # part that the model can't handle.
        fake_llm.raise_on_stream = BadRequestError(
            "[400 invalid_union] Error code: 400 - "
            "{'error': {'message': \"Invalid type for 'input'.\", "
            "'type': 'invalid_request_error', 'param': 'input', "
            "'code': 'invalid_union'}}"
        )

        png_bytes = base64.b64decode(
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42m"
            "NkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg=="
        )
        png_b64 = base64.b64encode(png_bytes).decode("ascii")

        with SyncTestClient(app) as sclient:
            r = sclient.post("/v1/chats", json={"agent_id": "ag-chat"})
            cid = r.json()["id"]
            with sclient.websocket_connect(f"/v1/chats/{cid}/ws") as ws:
                ws.send_json({
                    "kind": "user_message",
                    "content": "what's in this?",
                    "parts": [
                        {"type": "image", "data": png_b64, "mime_type": "image/png"},
                    ],
                })
                # Expect: user_message echo, then an error row with the
                # friendly diagnosis.
                user_msg = ws.receive_json()
                err = ws.receive_json()

        assert user_msg["kind"] == "user_message"
        assert err["kind"] == "error"
        text = err["message"]
        # Friendly message must mention the rejected modality, the
        # model name, and at least one actionable hint. Crucially it
        # must NOT leak the raw 'invalid_union' string.
        assert "image" in text
        assert "'m'" in text  # model name (the seeded agent's model)
        assert (
            "vision" in text.lower()
            or "attachments" in text.lower()
            or "image" in text.lower()
        )
        assert "invalid_union" not in text

        # Sanitization: the persisted user_message MUST have its
        # ImagePart stripped so the next turn doesn't replay the same
        # rejection. Storage now holds a text-only version with a
        # marker explaining the removal.
        msgs_storage = app.state.storage_provider.get_storage(ChatMessage)
        user_row = await msgs_storage.get(ChatMessage.make_id(cid, 1))
        assert user_row.kind == "user_message"
        parts_after = user_row.payload.get("parts") or []
        assert all(p.get("type") == "text" for p in parts_after), (
            f"expected only text parts after sanitization, got: {parts_after!r}"
        )
        joined = " ".join(p.get("text", "") for p in parts_after)
        assert "attachment removed" in joined
        assert "what's in this?" in joined  # original text preserved

    async def test_ws_subsequent_turn_after_rejection_is_resumable(
        self, app, fake_llm, seeded_agent,
    ):
        """After the first attachment-rejection failure, a follow-up
        text-only message must succeed because history was sanitized.
        Reproduces the 'non-resumable chat' bug: before the fix, the
        loaded history still carried the rejected ImagePart and the
        second turn re-triggered ``invalid_union`` indefinitely."""
        import base64
        from starlette.testclient import TestClient as SyncTestClient

        from primer.model.except_ import BadRequestError

        png_bytes = base64.b64decode(
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42m"
            "NkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg=="
        )
        png_b64 = base64.b64encode(png_bytes).decode("ascii")

        with SyncTestClient(app) as sclient:
            r = sclient.post("/v1/chats", json={"agent_id": "ag-chat"})
            cid = r.json()["id"]

            # Turn 1: attachment + LLM rejects.
            fake_llm.raise_on_stream = BadRequestError(
                "[400 invalid_union] Invalid type for 'input'."
            )
            with sclient.websocket_connect(f"/v1/chats/{cid}/ws") as ws:
                ws.send_json({
                    "kind": "user_message",
                    "content": "describe this",
                    "parts": [{"type": "image", "data": png_b64, "mime_type": "image/png"}],
                })
                ws.receive_json()  # user_message echo
                err = ws.receive_json()  # friendly error
                assert err["kind"] == "error"

            # Turn 2: text-only follow-up. LLM is now NOT rejecting
            # — if sanitization worked, the prompt rebuilt from
            # storage has no image, and this turn produces tokens.
            fake_llm.raise_on_stream = None
            fake_llm._reply_text = "Sure, what would you like to know?"
            with sclient.websocket_connect(f"/v1/chats/{cid}/ws?cursor=2") as ws:
                ws.send_json({"kind": "user_message", "content": "still there?"})
                got = [ws.receive_json() for _ in range(3)]
            kinds = [g["kind"] for g in got]
            assert kinds == ["user_message", "assistant_token", "done"], (
                f"second turn should have completed cleanly; got {kinds!r}"
            )
            assert got[1]["delta"] == "Sure, what would you like to know?"

        # The LLM call on turn 2 received a clean prompt — no ImagePart
        # in any message. The most recent call's messages list reflects
        # the full prompt the LLM saw.
        last_call = fake_llm.calls[-1]
        for msg in last_call["messages"]:
            for p in msg.parts:
                assert p.type == "text", (
                    f"turn 2 prompt must be text-only after sanitization; "
                    f"found part of type {p.type!r}"
                )

    async def test_ws_text_only_failure_keeps_raw_error(
        self, app, fake_llm, seeded_agent,
    ):
        """A stream failure on a TEXT-only turn falls back to the raw
        exception message — the diagnosis only kicks in when there's
        actually an attachment that could explain the rejection."""
        from starlette.testclient import TestClient as SyncTestClient

        from primer.model.except_ import BadRequestError

        fake_llm.raise_on_stream = BadRequestError("upstream is down")

        with SyncTestClient(app) as sclient:
            r = sclient.post("/v1/chats", json={"agent_id": "ag-chat"})
            cid = r.json()["id"]
            with sclient.websocket_connect(f"/v1/chats/{cid}/ws") as ws:
                ws.send_json({"kind": "user_message", "content": "hello"})
                _user_msg = ws.receive_json()
                err = ws.receive_json()

        assert err["kind"] == "error"
        assert "upstream is down" in err["message"]

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


# ===========================================================================
# ClaimEngine integration — upsert on user_message, delete_lease on end/delete
# ===========================================================================


class _SpyClaimEngine:
    """Spy wrapper around a real ClaimEngine — records calls and delegates."""

    def __init__(self, real_engine) -> None:
        self._real = real_engine
        self.upserted: list[tuple] = []   # (kind, entity_id, kwargs)
        self.deleted: list[tuple] = []     # (kind, entity_id)

    async def upsert(self, kind, entity_id, *, priority=100, next_attempt_at=None):
        self.upserted.append((kind, entity_id, {"priority": priority}))
        await self._real.upsert(kind, entity_id, priority=priority, next_attempt_at=next_attempt_at)

    async def delete_lease(self, kind, entity_id):
        self.deleted.append((kind, entity_id))
        await self._real.delete_lease(kind, entity_id)


@pytest.fixture
def app_with_engine(
    fake_storage_provider,
    fake_provider_registry,
    fake_llm,
):
    """app fixture that also wires a spy ClaimEngine on app.state.

    The spy wraps the real engine so worker-pool processing still works,
    while call recording is available for assertions.
    """
    async def _get_llm(_pid: str) -> _ChatTestFakeLLM:
        return fake_llm

    fake_provider_registry.get_llm = _get_llm  # type: ignore[assignment]
    _app = create_test_app(
        storage_provider=fake_storage_provider,
        provider_registry=fake_provider_registry,
        start_chat_worker=True,
    )
    # Wrap the real engine so calls are recorded but also delegated.
    _spy = _SpyClaimEngine(_app.state.claim_engine)
    _app.state.claim_engine = _spy
    return _app, _spy


@pytest.mark.asyncio
async def test_claim_engine_upsert_called_on_user_message(
    app_with_engine, seeded_agent,
):
    """When a user_message is sent via WS, engine.upsert(CHAT, chat_id) fires.

    ``seeded_agent`` seeds the shared ``fake_storage_provider`` (which
    ``app_with_engine`` also uses) so both fixtures share the same LLMProvider
    and Agent rows.
    """
    from primer.int.claim import ClaimKind
    from starlette.testclient import TestClient as SyncTestClient

    _app, engine = app_with_engine

    with SyncTestClient(_app) as sclient:
        # Use the agent seeded by the seeded_agent fixture (id="ag-chat").
        r = sclient.post("/v1/chats", json={"agent_id": seeded_agent.id})
        assert r.status_code == 201, r.text
        cid = r.json()["id"]
        with sclient.websocket_connect(f"/v1/chats/{cid}/ws") as ws:
            ws.send_json({"kind": "user_message", "content": "hello engine"})
            for _ in range(3):
                ws.receive_json()

    # engine.upsert must have been called with kind=CHAT, chat_id, priority=10
    chat_upserts = [u for u in engine.upserted if u[0] == ClaimKind.CHAT and u[1] == cid]
    assert chat_upserts, f"Expected engine.upsert(CHAT, {cid!r}) but got: {engine.upserted!r}"
    assert chat_upserts[0][2]["priority"] == 10


@pytest.mark.asyncio
async def test_claim_engine_delete_lease_on_soft_end(
    app_with_engine, seeded_agent,
):
    """DELETE /chats/{id} (soft-end) calls engine.delete_lease(CHAT, chat_id)."""
    from primer.int.claim import ClaimKind

    _app, engine = app_with_engine
    async with AsyncClient(
        transport=ASGITransport(app=_app), base_url="http://t",
    ) as c:
        r = await c.post("/v1/chats", json={"agent_id": seeded_agent.id})
        assert r.status_code == 201
        cid = r.json()["id"]

        resp = await c.delete(f"/v1/chats/{cid}")
        assert resp.status_code == 200

    assert (ClaimKind.CHAT, cid) in engine.deleted, (
        f"Expected engine.delete_lease(CHAT, {cid!r}) but got: {engine.deleted!r}"
    )


@pytest.mark.asyncio
async def test_claim_engine_delete_lease_on_force_delete(
    app_with_engine, seeded_agent,
):
    """DELETE /chats/{id}?force=true calls engine.delete_lease(CHAT, chat_id)."""
    from primer.int.claim import ClaimKind

    _app, engine = app_with_engine
    async with AsyncClient(
        transport=ASGITransport(app=_app), base_url="http://t",
    ) as c:
        r = await c.post("/v1/chats", json={"agent_id": seeded_agent.id})
        assert r.status_code == 201
        cid = r.json()["id"]

        resp = await c.delete(f"/v1/chats/{cid}?force=true")
        assert resp.status_code == 200

    assert (ClaimKind.CHAT, cid) in engine.deleted, (
        f"Expected engine.delete_lease(CHAT, {cid!r}) after force-delete "
        f"but got: {engine.deleted!r}"
    )


@pytest.mark.asyncio
async def test_claim_engine_none_is_noop(
    fake_storage_provider,
    fake_provider_registry,
    seeded_agent,
):
    """When claim_engine is not on app.state, all router paths still work."""
    _app = create_test_app(
        storage_provider=fake_storage_provider,
        provider_registry=fake_provider_registry,
    )
    # Deliberately do NOT set _app.state.claim_engine

    async with AsyncClient(
        transport=ASGITransport(app=_app), base_url="http://t",
    ) as c:
        r = await c.post("/v1/chats", json={"agent_id": seeded_agent.id})
        assert r.status_code == 201
        cid = r.json()["id"]
        # soft-end — should not raise even without engine
        resp = await c.delete(f"/v1/chats/{cid}")
        assert resp.status_code == 200
