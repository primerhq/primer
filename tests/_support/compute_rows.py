"""Row builders for the agent / graph reference-block tests (finding A-09), shared by the REST and the system-tool suites so both seed
EXACTLY the same blockers.

Every row names its target by id only; nothing here requires the target to exist, which is the point: a block must hold on the
reference, not on whether the thing it points at is healthy.
"""

from __future__ import annotations

from datetime import UTC, datetime

from primer.model.agent import Agent, AgentModel
from primer.model.graph import Graph, _AgentNodeRef, _BeginNode, _EndNode, _GraphNodeRef, _StaticEdge
from primer.model.trigger import AgentFreshSubConfig, GraphFreshSubConfig, Subscription
from primer.model.workspace_session import (
    AgentSessionBinding,
    GraphSessionBinding,
    SessionStatus,
    WorkspaceSession,
)


def agent_row(agent_id: str) -> Agent:
    return Agent(
        id=agent_id, description="a test agent", model=AgentModel(profile_id="mp-ref"), tools=[], system_prompt=["x"],
    )


def graph_row(graph_id: str, *middle_nodes) -> Graph:
    """``begin -> <middle nodes in a chain> -> end``; with no middle node, ``begin -> end``."""
    ids = ["begin", *[node.id for node in middle_nodes], "exit"]
    return Graph(
        id=graph_id,
        description="a test graph",
        nodes=[_BeginNode(id="begin"), *middle_nodes, _EndNode(id="exit")],
        edges=[_StaticEdge(from_node=a, to_node=b) for a, b in zip(ids, ids[1:])],
    )


def graph_naming_agent(graph_id: str, agent_id: str) -> Graph:
    return graph_row(graph_id, _AgentNodeRef(id="n1", agent_id=agent_id))


def graph_naming_graph(graph_id: str, target_graph_id: str) -> Graph:
    return graph_row(graph_id, _GraphNodeRef(id="n1", graph_id=target_graph_id))


def session_bound_to_agent(session_id: str, agent_id: str, status: SessionStatus) -> WorkspaceSession:
    return WorkspaceSession(
        id=session_id, workspace_id="ws-ref", binding=AgentSessionBinding(agent_id=agent_id), status=status,
        created_at=datetime.now(UTC),
    )


def session_bound_to_graph(session_id: str, graph_id: str, status: SessionStatus) -> WorkspaceSession:
    return WorkspaceSession(
        id=session_id, workspace_id="ws-ref", binding=GraphSessionBinding(graph_id=graph_id), status=status,
        created_at=datetime.now(UTC),
    )


def subscription_for_agent(subscription_id: str, agent_id: str) -> Subscription:
    return Subscription(
        id=subscription_id, trigger_id="trg-ref", config=AgentFreshSubConfig(workspace_id="ws-ref", agent_id=agent_id),
        created_at=datetime.now(UTC),
    )


def subscription_for_graph(subscription_id: str, graph_id: str) -> Subscription:
    return Subscription(
        id=subscription_id, trigger_id="trg-ref", config=GraphFreshSubConfig(workspace_id="ws-ref", graph_id=graph_id),
        created_at=datetime.now(UTC),
    )


# What each blocker is called in the 409 ("in_use_by: 1 <kind>(s) reference ...") and the id the seeded row carries. ONE table for the
# REST and the system-tool suites: both must refuse with the same words, so both expect the same string.
AGENT_BLOCKERS = {
    "graph node": (lambda: graph_naming_agent("g-1", "ag-1"), "in_use_by: 1 graph(s) reference 'ag-1' (first: 'g-1')"),
    "live session": (
        lambda: session_bound_to_agent("s-1", "ag-1", SessionStatus.WAITING),
        "in_use_by: 1 session(s) reference 'ag-1' (first: 's-1')",
    ),
    "trigger subscription": (
        lambda: subscription_for_agent("sub-1", "ag-1"),
        "in_use_by: 1 trigger subscription(s) reference 'ag-1' (first: 'sub-1')",
    ),
}

GRAPH_BLOCKERS = {
    "sub-graph node": (
        lambda: graph_naming_graph("g-parent", "g-1"),
        "in_use_by: 1 graph(s) reference 'g-1' (first: 'g-parent')",
    ),
    "live session": (
        lambda: session_bound_to_graph("s-1", "g-1", SessionStatus.RUNNING),
        "in_use_by: 1 session(s) reference 'g-1' (first: 's-1')",
    ),
    "trigger subscription": (
        lambda: subscription_for_graph("sub-1", "g-1"),
        "in_use_by: 1 trigger subscription(s) reference 'g-1' (first: 'sub-1')",
    ),
}

LIVE_STATUSES = [status for status in SessionStatus if status is not SessionStatus.ENDED]


async def insert_unreadable_graph(storage_provider, graph_id: str, data: str = '{"nodes": "not a list"}') -> None:
    """Put a row in the graph table that the model cannot decode (a shape that drifted, a hand edit, text that is not JSON, JSON that is
    not an object): the row exists, reading a page that holds it raises. Only for a real SQLite provider; the in-memory fakes never decode."""
    storage = storage_provider.get_storage(Graph)
    await storage._ensure_table()  # noqa: SLF001 - the table is created lazily; a raw insert needs it
    connection = storage_provider.connection
    await connection.execute(f'INSERT INTO "{storage._table}" (id, data) VALUES (?, ?)', (graph_id, data))  # noqa: SLF001
    await connection.commit()
