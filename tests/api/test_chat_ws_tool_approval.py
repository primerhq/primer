"""WS auto-reject for chat tool-approval.

Tests that the chat WebSocket handler auto-rejects a pending
tool_approval park when a new ``user_message`` or ``interrupt``
arrives, and that ``tool_approval_decide`` forwards the operator's
explicit decision to the event bus.

Uses the same ``starlette.testclient.TestClient.websocket_connect``
pattern as the existing ``test_chats.py`` WS suite.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any

import pytest
import pytest_asyncio
from fastapi import FastAPI
from pydantic import SecretStr

from matrix.api.app import create_test_app
from matrix.model.agent import Agent, AgentModel
from matrix.model.chat import Done, Message, StreamEvent, TextDelta
from matrix.model.chats import Chat
from matrix.model.provider import (
    AnthropicConfig,
    Limits,
    LLMModel,
    LLMProvider,
    LLMProviderType,
)


class _ApprovalFakeLLM:
    """Deterministic fake LLM matching the test_chats.py pattern.

    The tool-approval tests never actually drive a full LLM turn —
    they exercise the WS frame routing (user_message → auto-reject,
    interrupt → auto-reject, tool_approval_decide → publish). The
    fake just has to be a usable :class:`LLM`-shaped object so the
    chat runner builds without raising.
    """

    def __init__(self) -> None:
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
        yield TextDelta(text="ok", index=0)
        yield Done(stop_reason="stop", raw_reason="stop")

    async def aclose(self) -> None:
        return None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _approval_blob(*, tool_call_id: str, chat_id: str) -> dict[str, Any]:
    """Build a parked_state blob that looks like a tool_approval park."""
    return {
        "tool_call_id": tool_call_id,
        "yielded": {
            "tool_name": "_approval",
            "event_key": f"tool_approval:{chat_id}:{tool_call_id}",
            "resume_metadata": {
                "policy_id": "p1",
                "approval_type": "required",
                "gate_reason": None,
                "original_call": {
                    "id": tool_call_id,
                    "name": "delete_workspace",
                    "arguments": {"id": "ws"},
                },
            },
        },
        "parked_at_iso": datetime.now(UTC).isoformat(),
    }


# ---------------------------------------------------------------------------
# Fixtures  (mirror test_chats.py exactly)
# ---------------------------------------------------------------------------


@pytest.fixture
def fake_llm() -> _ApprovalFakeLLM:
    return _ApprovalFakeLLM()


@pytest.fixture
def app(fake_storage_provider, fake_provider_registry, fake_llm) -> FastAPI:
    async def _get_llm(_pid: str) -> _ApprovalFakeLLM:
        return fake_llm

    fake_provider_registry.get_llm = _get_llm  # type: ignore[assignment]
    return create_test_app(
        storage_provider=fake_storage_provider,
        provider_registry=fake_provider_registry,
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
        id="ag-approval",
        description="approval test agent",
        model=AgentModel(provider_id="llm-p", model_name="m"),
        tools=[],
        system_prompt=[],
    )
    await storage.create(agent)
    return agent


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestAutoRejectOnUserMessage:
    """user_message while parked on _approval must auto-reject."""

    async def test_user_message_while_approval_pending_auto_rejects(
        self, app: FastAPI, seeded_agent: Agent,
    ) -> None:
        from starlette.testclient import TestClient as SyncTestClient

        chat_id = "chat-auto-reject-um"
        # Seed a chat that is already parked on a tool_approval.
        chat_storage = app.state.storage_provider.get_storage(Chat)
        chat = Chat(
            id=chat_id,
            agent_id="ag-approval",
            created_at=datetime.now(UTC),
            parked_status="parked",
            parked_at=datetime.now(UTC),
            parked_event_key=f"tool_approval:{chat_id}:c1",
            parked_state=_approval_blob(tool_call_id="c1", chat_id=chat_id),
            last_seq=1,
        )
        await chat_storage.create(chat)

        # Capture every call to event_bus.publish.
        seen_publishes: list[tuple[str, Any]] = []
        original_publish = app.state.event_bus.publish

        async def _capture(key: str, payload: Any) -> None:
            seen_publishes.append((key, payload))
            await original_publish(key, payload)

        app.state.event_bus.publish = _capture  # type: ignore[method-assign]
        try:
            with SyncTestClient(app) as sclient:
                with sclient.websocket_connect(f"/v1/chats/{chat_id}/ws") as ws:
                    ws.send_json({"kind": "user_message", "content": "hello"})
                    # Consume: user_message echo + assistant_token + done
                    for _ in range(3):
                        ws.receive_json()
        finally:
            app.state.event_bus.publish = original_publish  # type: ignore[method-assign]

        assert any(
            key == f"tool_approval:{chat_id}:c1"
            and isinstance(payload, dict)
            and payload.get("decision") == "rejected"
            and "superseded" in (payload.get("reason") or "")
            for key, payload in seen_publishes
        ), f"expected auto-reject publish not found in: {seen_publishes!r}"

    async def test_interrupt_while_approval_pending_auto_rejects(
        self, app: FastAPI, seeded_agent: Agent,
    ) -> None:
        from starlette.testclient import TestClient as SyncTestClient

        chat_id = "chat-auto-reject-int"
        chat_storage = app.state.storage_provider.get_storage(Chat)
        chat = Chat(
            id=chat_id,
            agent_id="ag-approval",
            created_at=datetime.now(UTC),
            parked_status="resumable",
            parked_at=datetime.now(UTC),
            parked_event_key=f"tool_approval:{chat_id}:c2",
            parked_state=_approval_blob(tool_call_id="c2", chat_id=chat_id),
            last_seq=2,
        )
        await chat_storage.create(chat)

        seen_publishes: list[tuple[str, Any]] = []
        original_publish = app.state.event_bus.publish

        async def _capture(key: str, payload: Any) -> None:
            seen_publishes.append((key, payload))
            await original_publish(key, payload)

        app.state.event_bus.publish = _capture  # type: ignore[method-assign]
        try:
            with SyncTestClient(app) as sclient:
                with sclient.websocket_connect(f"/v1/chats/{chat_id}/ws") as ws:
                    ws.send_json({"kind": "interrupt"})
                    # interrupt path emits one error row
                    ws.receive_json()
        finally:
            app.state.event_bus.publish = original_publish  # type: ignore[method-assign]

        assert any(
            key == f"tool_approval:{chat_id}:c2"
            and isinstance(payload, dict)
            and payload.get("decision") == "rejected"
            and "interrupt" in (payload.get("reason") or "")
            for key, payload in seen_publishes
        ), f"expected auto-reject on interrupt not found: {seen_publishes!r}"


@pytest.mark.asyncio
class TestToolApprovalDecide:
    """tool_approval_decide must forward the operator decision to the bus."""

    async def test_approved_decision_published(
        self, app: FastAPI, seeded_agent: Agent,
    ) -> None:
        from starlette.testclient import TestClient as SyncTestClient

        chat_id = "chat-decide-approve"
        chat_storage = app.state.storage_provider.get_storage(Chat)
        chat = Chat(
            id=chat_id,
            agent_id="ag-approval",
            created_at=datetime.now(UTC),
            parked_status="parked",
            parked_at=datetime.now(UTC),
            parked_event_key=f"tool_approval:{chat_id}:c3",
            parked_state=_approval_blob(tool_call_id="c3", chat_id=chat_id),
            last_seq=1,
        )
        await chat_storage.create(chat)

        seen_publishes: list[tuple[str, Any]] = []
        original_publish = app.state.event_bus.publish

        async def _capture(key: str, payload: Any) -> None:
            seen_publishes.append((key, payload))
            await original_publish(key, payload)

        app.state.event_bus.publish = _capture  # type: ignore[method-assign]
        try:
            with SyncTestClient(app) as sclient:
                with sclient.websocket_connect(f"/v1/chats/{chat_id}/ws") as ws:
                    ws.send_json({
                        "kind": "tool_approval_decide",
                        "tool_call_id": "c3",
                        "decision": "approved",
                        "reason": None,
                    })
                    # The handler continues the loop after publishing;
                    # send a ping to get a reply so we can close cleanly.
                    ws.send_json({"kind": "ping"})
                    ws.receive_json()  # pong
        finally:
            app.state.event_bus.publish = original_publish  # type: ignore[method-assign]

        assert any(
            key == f"tool_approval:{chat_id}:c3"
            and isinstance(payload, dict)
            and payload.get("decision") == "approved"
            for key, payload in seen_publishes
        ), f"expected approved publish not found: {seen_publishes!r}"

    async def test_rejected_decision_published(
        self, app: FastAPI, seeded_agent: Agent,
    ) -> None:
        from starlette.testclient import TestClient as SyncTestClient

        chat_id = "chat-decide-reject"
        chat_storage = app.state.storage_provider.get_storage(Chat)
        chat = Chat(
            id=chat_id,
            agent_id="ag-approval",
            created_at=datetime.now(UTC),
            parked_status="parked",
            parked_at=datetime.now(UTC),
            parked_event_key=f"tool_approval:{chat_id}:c4",
            parked_state=_approval_blob(tool_call_id="c4", chat_id=chat_id),
            last_seq=1,
        )
        await chat_storage.create(chat)

        seen_publishes: list[tuple[str, Any]] = []
        original_publish = app.state.event_bus.publish

        async def _capture(key: str, payload: Any) -> None:
            seen_publishes.append((key, payload))
            await original_publish(key, payload)

        app.state.event_bus.publish = _capture  # type: ignore[method-assign]
        try:
            with SyncTestClient(app) as sclient:
                with sclient.websocket_connect(f"/v1/chats/{chat_id}/ws") as ws:
                    ws.send_json({
                        "kind": "tool_approval_decide",
                        "tool_call_id": "c4",
                        "decision": "rejected",
                        "reason": "operator veto",
                    })
                    ws.send_json({"kind": "ping"})
                    ws.receive_json()  # pong
        finally:
            app.state.event_bus.publish = original_publish  # type: ignore[method-assign]

        assert any(
            key == f"tool_approval:{chat_id}:c4"
            and isinstance(payload, dict)
            and payload.get("decision") == "rejected"
            and payload.get("reason") == "operator veto"
            for key, payload in seen_publishes
        ), f"expected rejected publish not found: {seen_publishes!r}"

    async def test_bad_decision_value_returns_error(
        self, app: FastAPI, seeded_agent: Agent,
    ) -> None:
        from starlette.testclient import TestClient as SyncTestClient

        chat_id = "chat-decide-bad"
        chat_storage = app.state.storage_provider.get_storage(Chat)
        chat = Chat(
            id=chat_id,
            agent_id="ag-approval",
            created_at=datetime.now(UTC),
            last_seq=0,
        )
        await chat_storage.create(chat)

        with SyncTestClient(app) as sclient:
            with sclient.websocket_connect(f"/v1/chats/{chat_id}/ws") as ws:
                ws.send_json({
                    "kind": "tool_approval_decide",
                    "tool_call_id": "c9",
                    "decision": "maybe",
                })
                msg = ws.receive_json()
                assert msg["kind"] == "error"
                assert msg.get("code") == "tool_approval_bad_decision"


# ---------------------------------------------------------------------------
# Unit tests: _maybe_auto_reject_pending_approval helper
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_maybe_auto_reject_publishes_when_parked_on_approval() -> None:
    """Helper publishes rejection when chat is parked on _approval."""
    from matrix.api.routers.chats import _maybe_auto_reject_pending_approval

    class _StubBus:
        def __init__(self) -> None:
            self.published: list[tuple[str, Any]] = []

        async def publish(self, key: str, payload: Any) -> None:
            self.published.append((key, payload))

    class _StubChat:
        parked_status = "parked"
        parked_state = _approval_blob(tool_call_id="c1", chat_id="chat-x")

    bus = _StubBus()
    await _maybe_auto_reject_pending_approval(
        chat=_StubChat(),
        event_bus=bus,
        note="superseded by new user input",
    )
    assert len(bus.published) == 1
    key, payload = bus.published[0]
    assert key == "tool_approval:chat-x:c1"
    assert payload == {
        "decision": "rejected",
        "reason": "superseded by new user input",
    }


@pytest.mark.asyncio
async def test_maybe_auto_reject_noop_when_not_parked() -> None:
    """Helper is a no-op when chat is not parked."""
    from matrix.api.routers.chats import _maybe_auto_reject_pending_approval

    class _StubBus:
        def __init__(self) -> None:
            self.published: list[tuple[str, Any]] = []

        async def publish(self, key: str, payload: Any) -> None:
            self.published.append((key, payload))

    class _StubChat:
        parked_status = None
        parked_state = None

    bus = _StubBus()
    await _maybe_auto_reject_pending_approval(
        chat=_StubChat(),
        event_bus=bus,
        note="irrelevant",
    )
    assert bus.published == []


@pytest.mark.asyncio
async def test_maybe_auto_reject_noop_when_parked_on_non_approval_tool() -> None:
    """Helper is a no-op when chat is parked on a tool that is NOT _approval."""
    from matrix.api.routers.chats import _maybe_auto_reject_pending_approval

    class _StubBus:
        def __init__(self) -> None:
            self.published: list[tuple[str, Any]] = []

        async def publish(self, key: str, payload: Any) -> None:
            self.published.append((key, payload))

    class _StubChat:
        parked_status = "parked"
        parked_state = {
            "yielded": {
                "tool_name": "ask_user",  # not _approval
                "event_key": "ask_user:chat-y:x1",
            }
        }

    bus = _StubBus()
    await _maybe_auto_reject_pending_approval(
        chat=_StubChat(),
        event_bus=bus,
        note="whatever",
    )
    assert bus.published == []
