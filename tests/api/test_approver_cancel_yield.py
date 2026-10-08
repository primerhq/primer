"""Cancelling a yield cannot reject a gate the caller may not decide (follow-up of #536).

``POST /v1/sessions/{id}/yields/{tcid}/cancel`` publishes a cancel payload to ANY park's event key, including an ``_approval`` gate, where
``classify_approval_payload`` turns it into a REJECTION. So any user could reject a gate routed to alice, whatever the policy said. The route
now judges an approval gate by the same ``may_decide`` as the respond route (403 ``approver_mismatch``); every other kind of yield is unchanged.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from primer.model.workspace_session import AgentSessionBinding, SessionStatus, WorkspaceSession
from tests.api.conftest import raw_client as client, app, fake_provider_registry  # noqa: F401
from tests.api.test_approver_routing import _login_user, _parked_session, _register_admin


class _Published:
    """Records what the cancel route puts on the event bus."""

    def __init__(self, bus) -> None:
        self.events: list[tuple[str, dict]] = []
        real = bus.publish

        async def spy(event_key, payload):
            self.events.append((event_key, payload))
            return await real(event_key, payload)

        bus.publish = spy


def _cancel_url(session_id: str, tool_call_id: str) -> str:
    return f"/v1/sessions/{session_id}/yields/{tool_call_id}/cancel"


@pytest.mark.asyncio
async def test_a_user_who_may_not_decide_an_approval_gate_cannot_reject_it_by_cancelling_its_yield(client, app) -> None:
    await _register_admin(client)
    storage = app.state.storage_provider.get_storage(WorkspaceSession)
    await storage.create(_parked_session(session_id="cy-1", tool_call_id="tc-1", approvers={"kind": "users", "users": ["alice"]}))
    published = _Published(app.state.event_bus)

    await _login_user(client, app, "bob")
    refused = await client.post(_cancel_url("cy-1", "tc-1"), json={"reason": "no"})

    assert refused.status_code == 403, f"bob rejected a gate routed to alice by cancelling it: {refused.text}"
    assert refused.json()["extensions"]["error"] == "approver_mismatch"
    assert published.events == [], "a refused cancel reached the event bus"

    await _login_user(client, app, "alice")
    accepted = await client.post(_cancel_url("cy-1", "tc-1"), json={"reason": "no"})
    assert accepted.status_code == 202, accepted.text
    assert [key for key, _ in published.events] == ["tool_approval:cy-1:tc-1"]


@pytest.mark.asyncio
async def test_an_unrestricted_approval_gate_can_still_be_cancelled_by_any_user(client, app) -> None:
    await _register_admin(client)
    await app.state.storage_provider.get_storage(WorkspaceSession).create(
        _parked_session(session_id="cy-2", tool_call_id="tc-2", approvers=None),
    )

    await _login_user(client, app, "carol")
    accepted = await client.post(_cancel_url("cy-2", "tc-2"), json={})

    assert accepted.status_code == 202, accepted.text


@pytest.mark.asyncio
async def test_cancelling_a_yield_that_is_not_an_approval_is_unchanged(client, app) -> None:
    """An ask_user park has no approver routing: any user can cancel it, as before."""
    await _register_admin(client)
    now = datetime.now(UTC)
    event_key = "ask_user:cy-3:tc-3"
    await app.state.storage_provider.get_storage(WorkspaceSession).create(WorkspaceSession(
        id="cy-3", workspace_id="ws", binding=AgentSessionBinding(kind="agent", agent_id="agt"), status=SessionStatus.RUNNING,
        created_at=now, parked_status="parked", parked_at=now, parked_event_key=event_key,
        parked_state={
            "tool_call_id": "tc-3",
            "yielded": {"tool_name": "ask_user", "event_key": event_key, "resume_metadata": {"prompt": "?"}},
        },
    ))

    await _login_user(client, app, "bob")
    accepted = await client.post(_cancel_url("cy-3", "tc-3"), json={})

    assert accepted.status_code == 202, accepted.text


# ---- a graph park is judged by the gate that was parked, not by its top-level projection (follow-up of #540 review) -------------------


def _graph_park_with_a_keyless_projection(session_id: str, *, tool_call_id: str, approvers: dict | None, in_checkpoint: bool = True) -> WorkspaceSession:
    """A graph park whose PRIMARY gate is a ToolCall node's approval, shaped as production writes it.

    The gate itself (with its stamped ``approvers``) lives only in ``graph_checkpoint.pending_toolcalls``. The top-level ``yielded`` is a
    projection of it (``_CheckpointMixin._build_pending_park_yield``) that carries ``original_call`` and nothing else, and
    ``pending_dispatch`` is the channel-prompt view of the same gate (also ``original_call`` only). So the top-level
    ``resume_metadata`` has no ``approvers`` key whatever the gate says.
    """
    now = datetime.now(UTC)
    event_key = f"tool_approval:{session_id}:{tool_call_id}"
    original_call = {"id": tool_call_id, "name": "delete_workspace", "arguments": {"id": "ws-x"}}
    gate = {
        "node_id": "worker", "tool_call_id": tool_call_id, "parked_event_key": event_key, "arguments": original_call["arguments"],
        "tool_name": "_approval", "scoped_tool_call_id": None,
        "resume_metadata": {
            "policy_id": "pol", "approval_type": "required", "gate_reason": None, "approvers": approvers, "original_call": original_call,
        },
    }
    return WorkspaceSession(
        id=session_id, workspace_id="ws", binding=AgentSessionBinding(kind="agent", agent_id="agt"), status=SessionStatus.RUNNING,
        created_at=now, parked_status="parked", parked_at=now, parked_event_key=event_key, parked_event_keys=[event_key],
        parked_state={
            "tool_call_id": tool_call_id,
            "yielded": {"tool_name": "_approval", "event_key": event_key, "resume_metadata": {"original_call": original_call}, "event_keys": [event_key]},
            "graph_checkpoint": {
                "pending_toolcalls": [gate] if in_checkpoint else [],
                "pending_agent_yields": [],
                "pending_dispatch": [
                    {"kind": "_approval", "node_id": "worker", "tool_call_id": tool_call_id, "resume_metadata": {"original_call": original_call}},
                ],
            },
        },
    )


@pytest.mark.asyncio
async def test_a_graph_parks_primary_toolcall_gate_restricted_to_alice_cannot_be_rejected_by_bob_cancelling_it(client, app) -> None:
    """The route used to judge the caller by the TOP-LEVEL ``yielded.resume_metadata``, which for this gate is ``{"original_call": ...}``
    with no ``approvers`` key: that reads as "anyone", so bob rejected a gate routed to alice. The gate's own stamp is judged now."""
    await _register_admin(client)
    storage = app.state.storage_provider.get_storage(WorkspaceSession)
    await storage.create(_graph_park_with_a_keyless_projection("cy-4", tool_call_id="tc-4", approvers={"kind": "users", "users": ["alice"]}))
    published = _Published(app.state.event_bus)

    await _login_user(client, app, "bob")
    refused = await client.post(_cancel_url("cy-4", "tc-4"), json={"reason": "no"})

    assert refused.status_code == 403, f"bob rejected a graph gate routed to alice by cancelling it: {refused.text}"
    assert refused.json()["extensions"]["error"] == "approver_mismatch"
    assert published.events == [], "a refused cancel reached the event bus"

    await _login_user(client, app, "alice")
    accepted = await client.post(_cancel_url("cy-4", "tc-4"), json={"reason": "no"})
    assert accepted.status_code == 202, accepted.text
    assert [key for key, _ in published.events] == ["tool_approval:cy-4:tc-4"]


@pytest.mark.asyncio
async def test_a_graph_parks_unrestricted_toolcall_gate_can_still_be_cancelled_by_any_user(client, app) -> None:
    await _register_admin(client)
    await app.state.storage_provider.get_storage(WorkspaceSession).create(
        _graph_park_with_a_keyless_projection("cy-5", tool_call_id="tc-5", approvers={"kind": "anyone"}),
    )

    await _login_user(client, app, "carol")
    accepted = await client.post(_cancel_url("cy-5", "tc-5"), json={})

    assert accepted.status_code == 202, accepted.text


@pytest.mark.asyncio
async def test_a_cancel_whose_gate_cannot_be_resolved_is_admin_only(client, app) -> None:
    """The park's primary id matches but no pending entry carries it (a checkpoint that lost its entry): the gate's spec cannot be read,
    so the route fails CLOSED, as an unreadable stamp does. An admin can still cancel it, so the park cannot wedge."""
    await _register_admin(client)
    await app.state.storage_provider.get_storage(WorkspaceSession).create(
        _graph_park_with_a_keyless_projection("cy-6", tool_call_id="tc-6", approvers={"kind": "users", "users": ["alice"]}, in_checkpoint=False),
    )
    published = _Published(app.state.event_bus)

    await _login_user(client, app, "bob")
    refused = await client.post(_cancel_url("cy-6", "tc-6"), json={})
    assert refused.status_code == 403, f"a gate that resolved to nothing was open to a plain user: {refused.text}"
    assert refused.json()["extensions"]["error"] == "approver_mismatch"
    assert published.events == []

    login = await client.post("/v1/auth/login", json={"username": "aradmin", "password": "aradminpass1"})
    assert login.status_code == 200, login.text
    accepted = await client.post(_cancel_url("cy-6", "tc-6"), json={})
    assert accepted.status_code == 202, accepted.text


# ---- a graph park's top-level tool_name is "_approval" whatever its primary is (review round 2 of #540) -----------------------------


def _graph_park_with_a_primary_that_is_not_an_approval(session_id: str, *, tool_call_id: str, where: str, tool_name: str) -> WorkspaceSession:
    """A graph park whose primary gate is NOT an approval, shaped as production writes it.

    ``_CheckpointMixin._build_pending_park_yield`` hard-codes the top-level ``yielded.tool_name`` to ``"_approval"`` for EVERY graph
    park, whatever the primary is, so an agent node's ``ask_user`` yield (``pending_agent_yields``), a value-yielding ToolCall node
    (``pending_toolcalls`` with its own ``tool_name``) or a node waiting on an external tool all look like an approval from the top.
    The pending entry carries the real kind.
    """
    now = datetime.now(UTC)
    event_key = f"{tool_name}:{session_id}:{tool_call_id}"
    metadata = {"prompt": "which one?"}
    if where == "toolcalls":
        entry = {"node_id": "worker", "tool_call_id": tool_call_id, "parked_event_key": event_key, "arguments": {}, "tool_name": tool_name,
                 "resume_metadata": metadata, "scoped_tool_call_id": None}
        checkpoint = {"pending_toolcalls": [entry], "pending_agent_yields": [], "pending_dispatch": []}
    else:
        entry = {"node_id": "worker", "tool_call_id": tool_call_id, "event_key": event_key, "tool_name": tool_name, "resume_metadata": metadata}
        checkpoint = {"pending_toolcalls": [], "pending_agent_yields": [entry], "pending_dispatch": []}
    return WorkspaceSession(
        id=session_id, workspace_id="ws", binding=AgentSessionBinding(kind="agent", agent_id="agt"), status=SessionStatus.RUNNING,
        created_at=now, parked_status="parked", parked_at=now, parked_event_key=event_key, parked_event_keys=[event_key],
        parked_state={
            "tool_call_id": tool_call_id,
            "yielded": {"tool_name": "_approval", "event_key": event_key, "resume_metadata": dict(metadata), "event_keys": [event_key]},
            "graph_checkpoint": checkpoint,
        },
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("where", "tool_name"),
    [("agent_yields", "ask_user"), ("toolcalls", "ask_user"), ("agent_yields", "_external")],
    ids=["agent-ask-user", "toolcall-value-yield", "agent-external-wait"],
)
async def test_a_plain_user_can_still_cancel_a_graph_parks_primary_that_is_not_an_approval(client, app, where, tool_name) -> None:
    """Resolving the gate as an ``_approval`` made every graph park whose primary is something else resolve to nothing, which fails
    closed to admin-only: the session owner's cancel of a plain question became a 403. Only an approval is judged."""
    await _register_admin(client)
    await app.state.storage_provider.get_storage(WorkspaceSession).create(
        _graph_park_with_a_primary_that_is_not_an_approval("cy-7", tool_call_id="tc-7", where=where, tool_name=tool_name),
    )
    published = _Published(app.state.event_bus)

    await _login_user(client, app, "bob")
    accepted = await client.post(_cancel_url("cy-7", "tc-7"), json={"reason": "never mind"})

    assert accepted.status_code == 202, f"a plain user could not cancel a {tool_name} yield of a graph park: {accepted.text}"
    assert [key for key, _ in published.events] == [f"{tool_name}:cy-7:tc-7"]
