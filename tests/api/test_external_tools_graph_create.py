"""Graph-session create validation for invoker-supplied tools."""

from __future__ import annotations

from primer.model.agent import Agent, AgentModel
from primer.model.graph import (
    Graph,
    _AgentNodeRef,
    _BeginNode,
    _EndNode,
    _StaticEdge,
)
from primer.model.workspace_session import WorkspaceSession

# The sessions suite's stack: its module-local ``app`` override wires a
# fake auto-creating workspace backend whose start_session works, which
# the create path's slot allocation needs.
from tests.api.test_sessions import (  # noqa: F401
    app,
    seeded_workspace,
    sessions_client,
)

DEF = {
    "name": "lookup_customer",
    "description": "Look up a customer.",
    "schema": {"type": "object"},
}


async def _seed(app, *, allow: bool) -> None:
    sp = app.state.storage_provider
    await sp.get_storage(Agent).create(
        Agent(
            id="agent-gext",
            description="d",
            model=AgentModel(profile_id="prof-1"),
            allow_external_tools=allow,
        )
    )
    await sp.get_storage(Graph).create(
        Graph(
            id="gr-ext-create",
            description="g",
            nodes=[
                _BeginNode(id="begin"),
                _AgentNodeRef(id="n1", agent_id="agent-gext"),
                _EndNode(id="end"),
            ],
            edges=[
                _StaticEdge(from_node="begin", to_node="n1"),
                _StaticEdge(from_node="n1", to_node="end"),
            ],
        )
    )


async def test_graph_create_rejects_defs_when_no_node_allows(
    sessions_client, seeded_workspace, app
):
    await _seed(app, allow=False)
    r = await sessions_client.post(
        f"/v1/workspaces/{seeded_workspace.id}/sessions",
        json={
            "binding": {"kind": "graph", "graph_id": "gr-ext-create"},
            "external_tools": [DEF],
        },
    )
    assert r.status_code == 422, r.text
    assert "allow_external_tools" in r.text


async def test_graph_create_accepts_defs_and_stamps_row(
    sessions_client, seeded_workspace, app
):
    await _seed(app, allow=True)
    r = await sessions_client.post(
        f"/v1/workspaces/{seeded_workspace.id}/sessions",
        json={
            "binding": {"kind": "graph", "graph_id": "gr-ext-create"},
            "external_tools": [DEF],
        },
    )
    assert r.status_code == 201, r.text
    sp = app.state.storage_provider
    row = await sp.get_storage(WorkspaceSession).get(r.json()["id"])
    assert row.external_tools
    assert row.external_tools[0]["name"] == "lookup_customer"
