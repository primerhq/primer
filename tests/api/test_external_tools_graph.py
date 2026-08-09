"""Graph sessions: external-tools gate, multi-node parks, partial resolution."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from primer.model.agent import Agent, AgentModel
from primer.model.external_tool import ExternalToolCall
from primer.model.graph import (
    Graph,
    _AgentNodeRef,
    _BeginNode,
    _EndNode,
    _StaticEdge,
)
from primer.model.workspace_session import (
    GraphSessionBinding,
    SessionStatus,
    WorkspaceSession,
)

# Reuse the steer suite's fixture stack (fake workspace backend + app).
from tests.api.test_external_tools_steer import (  # noqa: F401
    DEF,
    _seed_agent,
    _setup_ws,
    app,
    client,
    pr,
    sp,
    wsr,
)


async def _seed_graph(sp, *, agent_id: str = "agent-ext") -> str:
    graph = Graph(
        id="gr-ext",
        description="g",
        nodes=[
            _BeginNode(id="begin"),
            _AgentNodeRef(id="n1", agent_id=agent_id),
            _EndNode(id="end"),
        ],
        edges=[
            _StaticEdge(from_node="begin", to_node="n1"),
            _StaticEdge(from_node="n1", to_node="end"),
        ],
    )
    await sp.get_storage(Graph).create(graph)
    return graph.id


def _graph_parked_over(sid: str) -> dict:
    now = datetime.now(UTC)
    k1 = f"external_tool:{sid}:tc-g1"
    k2 = f"external_tool:{sid}:tc-g2"
    return dict(
        parked_status="parked",
        parked_event_key=k1,
        parked_event_keys=[k1, k2],
        parked_until=now + timedelta(seconds=600),
        parked_at=now,
        parked_state={
            "schema_version": 1,
            "tool_call_id": None,
            "yielded": {
                "tool_name": "_approval",
                "event_key": f"graph:{sid}",
                "resume_metadata": {},
            },
            "graph_checkpoint": {
                "pending_agent_yields": [
                    {
                        "node_id": "n1",
                        "tool_call_id": "tc-g1",
                        "event_key": k1,
                        "tool_name": "_external",
                        "resume_metadata": {
                            "original_call": {
                                "id": "tc-g1",
                                "name": "lookup_customer",
                                "arguments": {},
                            },
                            "external_call_row_id": "etool-g1",
                        },
                        "llm_messages": [],
                        "iteration": 1,
                        "frames": [],
                        "leaf": None,
                    },
                    {
                        "node_id": "n2",
                        "tool_call_id": "tc-g2",
                        "event_key": k2,
                        "tool_name": "_external",
                        "resume_metadata": {
                            "original_call": {
                                "id": "tc-g2",
                                "name": "pick_date",
                                "arguments": {},
                            },
                            "external_call_row_id": "etool-g2",
                        },
                        "llm_messages": [],
                        "iteration": 1,
                        "frames": [],
                        "leaf": None,
                    },
                ],
                "pending_toolcalls": [],
                "pending_dispatch": [],
            },
            "llm_messages": [],
            "turn_no": 1,
            "started_at": now.isoformat(),
            "resume_event_payload": None,
        },
    )


async def _seed_graph_session(sp, wid: str) -> None:
    now = datetime.now(UTC)
    row = WorkspaceSession(
        id="sess-1",
        workspace_id=wid,
        binding=GraphSessionBinding(graph_id="gr-ext"),
        status=SessionStatus.RUNNING,
        created_at=now,
        started_at=now,
        **_graph_parked_over("sess-1"),
    )
    await sp.get_storage(WorkspaceSession).create(row)


async def _seed_graph_calls(sp) -> None:
    calls = sp.get_storage(ExternalToolCall)
    for rid, tcid, name, node in (
        ("etool-g1", "tc-g1", "lookup_customer", "n1"),
        ("etool-g2", "tc-g2", "pick_date", "n2"),
    ):
        await calls.create(
            ExternalToolCall(
                id=rid,
                session_id="sess-1",
                node_id=node,
                tool_call_id=tcid,
                tool_name=name,
                arguments={},
                created_at=datetime.now(UTC),
            )
        )


# Create-path validation lives in test_external_tools_graph_create.py on
# the sessions suite's fixture stack (its fake backend supports the slot
# allocation the create path performs).


@pytest.mark.asyncio
async def test_pending_endpoint_lists_both_nodes(client, wsr, sp):
    wid = await _setup_ws(client, wsr)
    await _seed_agent(sp, allow=True)
    await _seed_graph(sp)
    await _seed_graph_session(sp, wid)
    await _seed_graph_calls(sp)
    r = await client.get("/v1/sessions/sess-1/external_tools/pending")
    ids = {i["tool_call_id"] for i in r.json()["items"]}
    assert ids == {"tc-g1", "tc-g2"}
    by_id = {i["tool_call_id"]: i for i in r.json()["items"]}
    assert by_id["tc-g1"]["node_id"] == "n1"
    del wid


@pytest.mark.asyncio
async def test_partial_resolution_leaves_other_pending(client, wsr, sp):
    wid = await _setup_ws(client, wsr)
    await _seed_agent(sp, allow=True)
    await _seed_graph(sp)
    await _seed_graph_session(sp, wid)
    await _seed_graph_calls(sp)
    r = await client.post(
        f"/v1/workspaces/{wid}/sessions/sess-1/steer",
        json={"tool_results": [{"tool_call_id": "tc-g1", "result": "ok"}]},
    )
    assert r.status_code == 200, r.text
    calls = sp.get_storage(ExternalToolCall)
    assert (await calls.get("etool-g1")).status == "completed"
    assert (await calls.get("etool-g2")).status == "pending"
    row = await sp.get_storage(WorkspaceSession).get("sess-1")
    # Multi-event park accumulates the fired key's payload.
    payloads = row.parked_state.get("resume_event_payloads") or {}
    assert any("tc-g1" in k or "ok" in str(v) for k, v in payloads.items())


@pytest.mark.asyncio
async def test_message_cancels_all_nodes(client, wsr, sp):
    wid = await _setup_ws(client, wsr)
    await _seed_agent(sp, allow=True)
    await _seed_graph(sp)
    await _seed_graph_session(sp, wid)
    await _seed_graph_calls(sp)
    r = await client.post(
        f"/v1/workspaces/{wid}/sessions/sess-1/steer",
        json={"instruction": "stop"},
    )
    assert r.status_code == 200, r.text
    calls = sp.get_storage(ExternalToolCall)
    for rid in ("etool-g1", "etool-g2"):
        assert (await calls.get(rid)).status == "cancelled"
