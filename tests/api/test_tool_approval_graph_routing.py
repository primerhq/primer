"""REST routing for concurrent graph approval gates (01a06b82 / 01a06c94).

A graph superstep can suspend on SEVERAL approval gates in the same park
(e.g. two fan-out siblings, each awaiting its own decision). Before the
shared enumerate/resolve helpers in :mod:`primer.session.pending_gates`,
only the FIRST pending entry was ever visible over REST:
``GET .../yields/pending`` returned at most one item (the top-level
``parked_state.yielded`` projection) and ``POST .../tool_approval/respond``
404'd for every ``tool_call_id`` but the primary's -- non-primary gates
were stranded, answerable only through the channel/inbox surface (which
already searched the full checkpoint). These tests drive a hand-built
two-gate graph checkpoint through both REST surfaces and assert the
second (non-primary) gate is now visible and answerable.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from primer.model.storage import OffsetPage
from primer.model.tool_approval import ToolApprovalRecord
from primer.model.workspace_session import (
    AgentSessionBinding,
    SessionStatus,
    WorkspaceSession,
)


def _two_gate_graph_parked_session(
    *, session_id: str, workspace_id: str,
) -> WorkspaceSession:
    """A session parked on a graph superstep with TWO pending approval
    gates (two fan-out siblings), mirroring
    ``_CheckpointMixin._build_pending_park_yield`` + ``snapshot_state``'s
    real shape: the top-level ``yielded`` projects only the FIRST
    (``worker[0]``) entry; ``worker[1]``'s gate lives only in the
    checkpoint's ``pending_toolcalls``.
    """
    now = datetime.now(UTC)

    def _entry(node_id: str, tool_call_id: str) -> dict:
        return {
            "node_id": node_id,
            "tool_call_id": tool_call_id,
            # UNSCOPED: node-scoping (current_graph_node_id) only wraps
            # agent/subgraph node dispatch (_node_dispatch.py), never a
            # ToolCall node's own dispatch, so a ToolCall-node approval
            # gate's key never folds in node_id - its tool_call_id is
            # always a fresh uuid4 the graph engine mints itself, never
            # provider-raw/collision-prone, so scoping was never needed
            # for this gate type (see primer.channel.inbox._matching_
            # event_keys's own comment on this exact invariant).
            "parked_event_key": f"tool_approval:{session_id}:{tool_call_id}",
            "arguments": {"id": f"ws-{node_id}"},
            "tool_name": "_approval",
            "resume_metadata": {
                "policy_id": "pol-fanout",
                "approval_type": "required",
                "gate_reason": "matched policy",
                "approvers": None,
                "original_call": {
                    "id": tool_call_id, "name": "delete_workspace",
                    "arguments": {"id": f"ws-{node_id}"},
                },
            },
            "scoped_tool_call_id": None,
        }

    entries = [_entry("worker[0]", "call-0"), _entry("worker[1]", "call-1")]
    all_keys = [e["parked_event_key"] for e in entries]
    primary = entries[0]

    return WorkspaceSession(
        id=session_id,
        workspace_id=workspace_id,
        binding=AgentSessionBinding(kind="agent", agent_id="agt"),
        status=SessionStatus.RUNNING,
        created_at=now,
        parked_status="parked",
        parked_at=now,
        parked_event_key=primary["parked_event_key"],
        # A real multi-gate park stamps the full key set here (dispatch.py
        # ``parked_event_keys=getattr(yielded, "event_keys", None)``) --
        # this is what routes durably_mark_session_resumable into its
        # accumulating (not clobbering) branch.
        parked_event_keys=all_keys,
        parked_state={
            "tool_call_id": primary["tool_call_id"],
            "yielded": {
                "tool_name": "_approval",
                "event_key": primary["parked_event_key"],
                "resume_metadata": primary["resume_metadata"],
                "event_keys": all_keys,
            },
            "graph_checkpoint": {
                "pending_toolcalls": entries,
                "pending_agent_yields": [],
                "pending_dispatch": [
                    {
                        "kind": "_approval",
                        "node_id": e["node_id"],
                        "tool_call_id": e["tool_call_id"],
                        "resume_metadata": {
                            "original_call": e["resume_metadata"]["original_call"],
                        },
                    }
                    for e in entries
                ],
            },
        },
    )


@pytest.mark.asyncio
async def test_session_pending_yields_lists_every_concurrent_approval_gate(
    app, client,
):
    sess = _two_gate_graph_parked_session(session_id="g-multi1", workspace_id="ws-g")
    storage = app.state.storage_provider.get_storage(WorkspaceSession)
    await storage.create(sess)

    resp = await client.get("/v1/workspaces/ws-g/sessions/g-multi1/yields/pending")
    assert resp.status_code == 200, resp.text
    items = resp.json()["items"]
    assert {i["tool_call_id"] for i in items} == {"call-0", "call-1"}
    assert all(i["kind"] == "approval" for i in items)


@pytest.mark.asyncio
async def test_respond_to_second_of_two_concurrent_graph_approvals(app, client):
    """The primary gate (call-0) is never touched here -- responding to
    the SECOND (call-1) used to 404 because ``_publish_decision`` only
    ever matched the top-level ``yielded`` blob's tool_call_id (call-0).
    It must now resolve + wake call-1's own event_key and write a record
    scoped to call-1, not call-0."""
    sess = _two_gate_graph_parked_session(session_id="g-multi2", workspace_id="ws-g")
    storage = app.state.storage_provider.get_storage(WorkspaceSession)
    await storage.create(sess)

    resp = await client.post(
        "/v1/sessions/g-multi2/tool_approval/respond",
        json={
            "tool_call_id": "call-1", "decision": "approved",
            "reason": "sibling ok",
        },
    )
    assert resp.status_code == 202, resp.text

    row = await storage.get("g-multi2")
    assert row is not None
    assert row.parked_status == "resumable"
    # The multi-event accumulation path (parked_event_keys was set on the
    # row), not the singular resume_event_payload a single-gate park uses.
    payloads = row.parked_state.get("resume_event_payloads") or {}
    assert len(payloads) == 1
    entry = next(iter(payloads.values()))
    assert entry["payload"] == {
        "decision": "approved", "reason": "sibling ok", "decided_by": "testuser",
    }
    assert entry["event_key"] == "tool_approval:g-multi2:call-1"
    # The singular "last fired" hint still gets stamped too, per
    # durably_mark_session_resumable's contract.
    assert (
        row.parked_state["resume_event_key"]
        == "tool_approval:g-multi2:call-1"
    )

    records = app.state.storage_provider.get_storage(ToolApprovalRecord)
    page = await records.list(OffsetPage(offset=0, length=50))
    matches = [r for r in page.items if r.session_id == "g-multi2"]
    assert len(matches) == 1
    rec = matches[0]
    assert rec.tool_call_id == "call-1"
    assert rec.decision == "approved"
    assert rec.gate_event_key == "tool_approval:g-multi2:call-1"


@pytest.mark.asyncio
async def test_respond_to_unknown_tool_call_id_still_404s(app, client):
    sess = _two_gate_graph_parked_session(session_id="g-multi3", workspace_id="ws-g")
    storage = app.state.storage_provider.get_storage(WorkspaceSession)
    await storage.create(sess)

    resp = await client.post(
        "/v1/sessions/g-multi3/tool_approval/respond",
        json={"tool_call_id": "call-does-not-exist", "decision": "approved"},
    )
    assert resp.status_code == 404, resp.text
