"""GET pending + POST respond for tool_approval (sessions + chats)."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from primer.model.chats import Chat
from primer.model.workspace_session import WorkspaceSession


def _make_approval_parked_session(*, session_id: str, tool_call_id: str) -> WorkspaceSession:
    """Helper: a session row parked on _approval."""
    now = datetime.now(UTC)
    return WorkspaceSession(
        id=session_id,
        workspace_id="ws",
        binding={"kind": "agent", "agent_id": "agt"},
        status="running",
        created_at=now,
        parked_status="parked",
        parked_at=now,
        parked_event_key=f"tool_approval:{session_id}:{tool_call_id}",
        parked_state={
            "tool_call_id": tool_call_id,
            "yielded": {
                "tool_name": "_approval",
                "event_key": f"tool_approval:{session_id}:{tool_call_id}",
                "resume_metadata": {
                    "policy_id": "p1",
                    "approval_type": "required",
                    "gate_reason": "always",
                    "original_call": {
                        "id": tool_call_id,
                        "name": "delete_workspace",
                        "arguments": {"id": "ws-x"},
                    },
                },
            },
            "parked_at_iso": now.isoformat(),
        },
    )


@pytest.mark.asyncio
async def test_get_session_pending_returns_payload(client, app):
    sess = _make_approval_parked_session(
        session_id="sess-pending", tool_call_id="call-1",
    )
    storage = app.state.storage_provider.get_storage(WorkspaceSession)
    await storage.create(sess)
    r = await client.get("/v1/sessions/sess-pending/tool_approval/pending")
    assert r.status_code == 200, r.text
    j = r.json()
    assert j["tool_call_id"] == "call-1"
    assert j["tool_name"] == "delete_workspace"
    assert j["arguments"] == {"id": "ws-x"}
    assert j["policy_id"] == "p1"
    assert j["approval_type"] == "required"


@pytest.mark.asyncio
async def test_get_session_pending_404_when_not_approval_parked(client):
    r = await client.get("/v1/sessions/missing/tool_approval/pending")
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_post_session_respond_publishes_approval(client, app):
    sess = _make_approval_parked_session(
        session_id="sess-resp", tool_call_id="call-2",
    )
    storage = app.state.storage_provider.get_storage(WorkspaceSession)
    await storage.create(sess)
    seen: list = []
    original_publish = app.state.event_bus.publish

    async def _capture(key, payload):
        seen.append((key, payload))
        await original_publish(key, payload)

    app.state.event_bus.publish = _capture
    try:
        r = await client.post(
            "/v1/sessions/sess-resp/tool_approval/respond",
            json={
                "tool_call_id": "call-2",
                "decision": "approved",
            },
        )
        assert r.status_code == 202, r.text
    finally:
        app.state.event_bus.publish = original_publish
    assert seen == [
        (
            "tool_approval:sess-resp:call-2",
            {"decision": "approved", "reason": None},
        ),
    ]


@pytest.mark.asyncio
async def test_post_session_respond_rejected_with_reason(client, app):
    sess = _make_approval_parked_session(
        session_id="sess-reject", tool_call_id="call-3",
    )
    storage = app.state.storage_provider.get_storage(WorkspaceSession)
    await storage.create(sess)
    seen: list = []
    original_publish = app.state.event_bus.publish

    async def _capture(key, payload):
        seen.append(payload)
        await original_publish(key, payload)

    app.state.event_bus.publish = _capture
    try:
        r = await client.post(
            "/v1/sessions/sess-reject/tool_approval/respond",
            json={
                "tool_call_id": "call-3",
                "decision": "rejected",
                "reason": "looks risky",
            },
        )
        assert r.status_code == 202
    finally:
        app.state.event_bus.publish = original_publish
    assert seen == [{"decision": "rejected", "reason": "looks risky"}]


@pytest.mark.asyncio
async def test_post_session_respond_mismatched_tool_call_id_404(client, app):
    sess = _make_approval_parked_session(
        session_id="sess-mm", tool_call_id="call-4",
    )
    storage = app.state.storage_provider.get_storage(WorkspaceSession)
    await storage.create(sess)
    r = await client.post(
        "/v1/sessions/sess-mm/tool_approval/respond",
        json={"tool_call_id": "different", "decision": "approved"},
    )
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_post_session_respond_bad_decision_422(client, app):
    sess = _make_approval_parked_session(
        session_id="sess-bad", tool_call_id="call-5",
    )
    storage = app.state.storage_provider.get_storage(WorkspaceSession)
    await storage.create(sess)
    r = await client.post(
        "/v1/sessions/sess-bad/tool_approval/respond",
        json={"tool_call_id": "call-5", "decision": "maybe"},
    )
    assert r.status_code == 422


# ===========================================================================
# Chat surface: GET /v1/chats/{id}/tool_approval/pending
# ===========================================================================


def _make_approval_pending_chat(*, chat_id: str, tool_call_id: str) -> Chat:
    """Helper: a chat row with a pending approval tool call."""
    now = datetime.now(UTC)
    return Chat(
        id=chat_id,
        agent_id="agt",
        created_at=now,
        status="active",
        pending_tool_call={
            "tool_call_id": tool_call_id,
            "mode": "approval",
            "original_call": {
                "id": tool_call_id,
                "name": "delete_workspace",
                "arguments": {"id": "ws-x"},
            },
        },
    )


@pytest.mark.asyncio
async def test_get_chat_pending_returns_payload(client, app):
    """A chat with pending_tool_call mode=approval returns 200 with correct fields."""
    chat = _make_approval_pending_chat(
        chat_id="chat-pending-1", tool_call_id="ctcall-1",
    )
    storage = app.state.storage_provider.get_storage(Chat)
    await storage.create(chat)
    r = await client.get("/v1/chats/chat-pending-1/tool_approval/pending")
    assert r.status_code == 200, r.text
    j = r.json()
    assert j["tool_call_id"] == "ctcall-1"
    assert j["tool_name"] == "delete_workspace"
    assert j["arguments"] == {"id": "ws-x"}
    assert isinstance(j["parked_at"], str) and j["parked_at"]


@pytest.mark.asyncio
async def test_get_chat_pending_404_when_no_pending_tool_call(client):
    """A chat that does not exist (or has no pending approval) returns 404."""
    r = await client.get("/v1/chats/missing-chat/tool_approval/pending")
    assert r.status_code == 404
    body = r.json()
    assert body["status"] == 404
    assert body["type"].endswith("/not-found")


@pytest.mark.asyncio
async def test_get_chat_pending_404_when_pending_is_ask_user(client, app):
    """A chat with pending_tool_call mode=ask_user returns 404 (not approval)."""
    now = datetime.now(UTC)
    chat = Chat(
        id="chat-ask-user-1",
        agent_id="agt",
        created_at=now,
        status="active",
        pending_tool_call={
            "tool_call_id": "ask-tc-1",
            "mode": "ask_user",
            "response_schema": None,
        },
    )
    storage = app.state.storage_provider.get_storage(Chat)
    await storage.create(chat)
    r = await client.get("/v1/chats/chat-ask-user-1/tool_approval/pending")
    assert r.status_code == 404
    body = r.json()
    assert body["status"] == 404
    assert body["type"].endswith("/not-found")


@pytest.mark.asyncio
async def test_get_chat_pending_404_rfc7807_envelope(client):
    """The 404 for a missing chat carries a proper RFC 7807 problem-details body."""
    r = await client.get("/v1/chats/no-such-chat/tool_approval/pending")
    assert r.status_code == 404
    body = r.json()
    # RFC 7807 required fields.
    assert "status" in body and body["status"] == 404
    assert "type" in body and body["type"].endswith("/not-found")
    # Must not expose an internal-error envelope.
    import json as _json
    assert "/errors/internal" not in _json.dumps(body)
