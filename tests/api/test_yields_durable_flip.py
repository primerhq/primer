"""Durable resume-flip in the reply handlers (arch review D-C2).

The ``ask_user`` and ``tool_approval`` respond endpoints used to be
bus-only: they published the reply on the event bus and returned 202 with
NO durable write. The durable ``parked -> resumable`` stamp happened only
in the bus listener, so a reply that landed while the listener was
down/reconnecting was permanently lost (LISTEN/NOTIFY is not durable) and
the park later timed out "unanswered".

These tests wire the app WITHOUT any :class:`YieldEventListener`, so the
bus publish is a genuine no-op (nothing consumes it). If the handler still
flips + stamps the row, the durability fix is doing its job. They also
prove the flip survives a publish that raises (best-effort wake) and is
idempotent under a second, listener-style flip.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from primer.model.workspace_session import (
    AgentSessionBinding,
    SessionStatus,
    WorkspaceSession,
)
from primer.session.yields import durably_mark_session_resumable


def _make_ask_user_parked_session(
    *, session_id: str, tool_call_id: str,
) -> WorkspaceSession:
    now = datetime.now(UTC)
    ek = f"ask_user:{session_id}:{tool_call_id}"
    sess = WorkspaceSession(
        id=session_id,
        workspace_id="ws-x",
        binding=AgentSessionBinding(kind="agent", agent_id="ag-x"),
        status=SessionStatus.RUNNING,
        created_at=now,
    )
    sess.parked_status = "parked"
    sess.parked_event_key = ek
    sess.parked_until = now + timedelta(seconds=600)
    sess.parked_at = now
    sess.parked_state = {
        "schema_version": 1,
        "tool_call_id": tool_call_id,
        "yielded": {
            "tool_name": "ask_user",
            "event_key": ek,
            "timeout": 600.0,
            "resume_metadata": {
                "prompt": "name?",
                "response_schema": None,
                "tool_call_id": tool_call_id,
                "parked_at_iso": now.isoformat(),
            },
        },
        "llm_messages": [],
        "turn_no": 1,
        "started_at": now.isoformat(),
        "resume_event_payload": None,
    }
    return sess


def _make_approval_parked_session(
    *, session_id: str, tool_call_id: str,
) -> WorkspaceSession:
    now = datetime.now(UTC)
    ek = f"tool_approval:{session_id}:{tool_call_id}"
    return WorkspaceSession(
        id=session_id,
        workspace_id="ws",
        binding=AgentSessionBinding(kind="agent", agent_id="agt"),
        status=SessionStatus.RUNNING,
        created_at=now,
        parked_status="parked",
        parked_at=now,
        parked_event_key=ek,
        parked_state={
            "tool_call_id": tool_call_id,
            "yielded": {
                "tool_name": "_approval",
                "event_key": ek,
                "resume_metadata": {
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
async def test_ask_user_respond_flips_row_without_listener(app, client):
    """No listener is wired, so the bus publish is a no-op; the row must
    still be flipped + stamped by the handler's own durable write."""
    sess = _make_ask_user_parked_session(session_id="d-a1", tool_call_id="tc1")
    storage = app.state.storage_provider.get_storage(WorkspaceSession)
    await storage.create(sess)

    resp = await client.post(
        "/v1/sessions/d-a1/ask_user/respond",
        json={"tool_call_id": "tc1", "response": "Alice"},
    )
    assert resp.status_code == 202

    row = await storage.get("d-a1")
    assert row is not None
    # Flipped durably by the handler, even though nothing consumed the bus.
    assert row.parked_status == "resumable"
    assert row.parked_state["resume_event_payload"] == {"response": "Alice"}
    assert row.parked_state["resume_event_key"] == "ask_user:d-a1:tc1"


@pytest.mark.asyncio
async def test_ask_user_respond_flips_even_when_publish_raises(app, client):
    """The bus publish is best-effort: a raising publish must not fail the
    reply nor lose the durable flip."""
    sess = _make_ask_user_parked_session(session_id="d-a2", tool_call_id="tc2")
    storage = app.state.storage_provider.get_storage(WorkspaceSession)
    await storage.create(sess)

    async def _boom(key, payload):  # noqa: ANN001
        raise RuntimeError("bus down")

    original = app.state.event_bus.publish
    app.state.event_bus.publish = _boom
    try:
        resp = await client.post(
            "/v1/sessions/d-a2/ask_user/respond",
            json={"tool_call_id": "tc2", "response": "Bob"},
        )
        assert resp.status_code == 202
    finally:
        app.state.event_bus.publish = original

    row = await storage.get("d-a2")
    assert row is not None
    assert row.parked_status == "resumable"
    assert row.parked_state["resume_event_payload"] == {"response": "Bob"}


@pytest.mark.asyncio
async def test_tool_approval_respond_flips_row_without_listener(app, client):
    """The tool_approval decision publisher must also stamp the row durably
    before the (unconsumed) bus publish."""
    sess = _make_approval_parked_session(
        session_id="d-ap1", tool_call_id="call-1",
    )
    storage = app.state.storage_provider.get_storage(WorkspaceSession)
    await storage.create(sess)

    resp = await client.post(
        "/v1/sessions/d-ap1/tool_approval/respond",
        json={"tool_call_id": "call-1", "decision": "approved"},
    )
    assert resp.status_code == 202

    row = await storage.get("d-ap1")
    assert row is not None
    assert row.parked_status == "resumable"
    assert row.parked_state["resume_event_payload"] == {
        "decision": "approved",
        "reason": None,
    }


@pytest.mark.asyncio
async def test_second_listener_style_flip_is_idempotent(app, client):
    """After the handler's durable flip, a second (listener-style) flip of
    the same single-event park is a guard-rejected no-op — it must not
    corrupt the already-stamped payload."""
    sess = _make_ask_user_parked_session(session_id="d-idem", tool_call_id="tcx")
    storage = app.state.storage_provider.get_storage(WorkspaceSession)
    await storage.create(sess)

    resp = await client.post(
        "/v1/sessions/d-idem/ask_user/respond",
        json={"tool_call_id": "tcx", "response": "first"},
    )
    assert resp.status_code == 202

    row = await storage.get("d-idem")
    assert row.parked_status == "resumable"
    assert row.parked_state["resume_event_payload"] == {"response": "first"}

    # Listener-style replay for the same key: the guard rejects a
    # single-event park that is already resumable, so nothing changes.
    did = await durably_mark_session_resumable(
        row,
        event_key="ask_user:d-idem:tcx",
        payload={"response": "second"},
        session_storage=storage,
        engine=None,
    )
    assert did is False
    after = await storage.get("d-idem")
    assert after.parked_status == "resumable"
    assert after.parked_state["resume_event_payload"] == {"response": "first"}
