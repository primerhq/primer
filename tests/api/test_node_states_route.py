"""REST tests for GET /v1/graphs/{gid}/runs/{rid}/node_states.

Mirrors tests/api/test_turn_log_routes.py: a workspace-backed run reads
.state/graphs/<rid>/state.json; a GraphThread-backed run reads
thread.node_states. Nodes present in the graph definition but absent
from the persisted state map surface as `pending`.
"""

from __future__ import annotations

from datetime import datetime, timezone

import httpx
import pytest


def _now() -> datetime:
    return datetime(2026, 6, 5, 10, 0, 0, tzinfo=timezone.utc)


class _FakeWorkspace:
    state_path = ".state"

    def __init__(self) -> None:
        self._files: dict[str, bytes] = {}

    def write(self, path: str, content: str) -> None:
        self._files[path] = content.encode("utf-8")

    async def read_file(self, path: str) -> bytes:
        if path not in self._files:
            from primer.model.except_ import NotFoundError
            raise NotFoundError(f"{path!r} not found")
        return self._files[path]


def _seed_graph(fake_storage_provider, gid: str):
    """A begin -> drafter(agent) -> end graph with three nodes."""
    from primer.model.graph import Graph

    return Graph(
        id=gid,
        description="probe",
        nodes=[
            {"kind": "begin", "id": "begin"},
            {"kind": "agent", "id": "drafter", "agent_id": "ag1"},
            {"kind": "end", "id": "end", "output_template": ""},
        ],
        edges=[
            {"kind": "static", "from_node": "begin", "to_node": "drafter"},
            {"kind": "static", "from_node": "drafter", "to_node": "end"},
        ],
    )


@pytest.mark.asyncio
async def test_node_states_workspace_backed(
    client: httpx.AsyncClient, app, fake_storage_provider,
):
    from primer.model.graph import Graph
    from primer.model.workspace_session import (
        GraphSessionBinding,
        SessionStatus,
        WorkspaceSession,
    )

    await fake_storage_provider.get_storage(Graph).create(
        _seed_graph(fake_storage_provider, "g-1")
    )
    sess = WorkspaceSession(
        id="run-ws-1",
        workspace_id="ws-graph",
        binding=GraphSessionBinding(graph_id="g-1"),
        status=SessionStatus.RUNNING,
        created_at=_now(),
        turn_status="running",
    )
    await fake_storage_provider.get_storage(WorkspaceSession).create(sess)

    ws = _FakeWorkspace()
    # 'begin' ended, 'drafter' running; 'end' absent -> pending.
    ws.write(
        ".state/graphs/run-ws-1/state.json",
        '{"iteration":1,"status":"running","ended_reason":null,'
        '"ended_detail":null,"node_states":{'
        '"begin":{"status":"ended","last_run_iteration":0,'
        '"last_run_at":"2026-06-05T10:00:00+00:00","error":null},'
        '"drafter":{"status":"running","last_run_iteration":1,'
        '"last_run_at":"2026-06-05T10:00:05+00:00","error":null}}}',
    )

    async def _get(wid):
        return ws if wid == "ws-graph" else None

    app.state.workspace_registry.get_workspace = _get  # type: ignore[assignment]

    r = await client.get("/v1/graphs/g-1/runs/run-ws-1/node_states")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["run_id"] == "run-ws-1"
    assert body["graph_id"] == "g-1"
    by_id = {it["node_id"]: it for it in body["items"]}
    assert set(by_id) == {"begin", "drafter", "end"}
    assert by_id["begin"]["kind"] == "begin"
    assert by_id["begin"]["status"] == "ended"
    assert by_id["drafter"]["kind"] == "agent"
    assert by_id["drafter"]["status"] == "running"
    assert by_id["drafter"]["iteration"] == 1
    # Unrun node defaults to pending with null metrics.
    assert by_id["end"]["status"] == "pending"
    assert by_id["end"]["error"] is None
    assert by_id["end"]["tokens_in"] is None


@pytest.mark.asyncio
async def test_node_states_failed_node_carries_error(
    client: httpx.AsyncClient, app, fake_storage_provider,
):
    from primer.model.graph import Graph
    from primer.model.workspace_session import (
        GraphSessionBinding,
        SessionStatus,
        WorkspaceSession,
    )

    await fake_storage_provider.get_storage(Graph).create(
        _seed_graph(fake_storage_provider, "g-2")
    )
    sess = WorkspaceSession(
        id="run-ws-2",
        workspace_id="ws-graph2",
        binding=GraphSessionBinding(graph_id="g-2"),
        status=SessionStatus.RUNNING,
        created_at=_now(),
        turn_status="running",
    )
    await fake_storage_provider.get_storage(WorkspaceSession).create(sess)

    ws = _FakeWorkspace()
    ws.write(
        ".state/graphs/run-ws-2/state.json",
        '{"iteration":1,"status":"running","node_states":{'
        '"drafter":{"status":"failed","last_run_iteration":1,'
        '"last_run_at":"2026-06-05T10:00:05+00:00",'
        '"error":"references missing Agent \'drafter\'"}}}',
    )

    async def _get(wid):
        return ws if wid == "ws-graph2" else None

    app.state.workspace_registry.get_workspace = _get  # type: ignore[assignment]

    r = await client.get("/v1/graphs/g-2/runs/run-ws-2/node_states")
    assert r.status_code == 200, r.text
    by_id = {it["node_id"]: it for it in r.json()["items"]}
    assert by_id["drafter"]["status"] == "failed"
    assert "missing Agent" in by_id["drafter"]["error"]


@pytest.mark.asyncio
async def test_node_states_storage_backed(
    client: httpx.AsyncClient, app, fake_storage_provider,
):
    from primer.model.graph import Graph, GraphThread, NodeRuntimeState

    await fake_storage_provider.get_storage(Graph).create(
        _seed_graph(fake_storage_provider, "g-3")
    )
    thread = GraphThread(
        id="gt-1",
        graph_id="g-3",
        title="t",
        created_at=_now(),
        last_activity_at=_now(),
        node_states={
            "begin": NodeRuntimeState(status="ended", last_run_iteration=0),
            "drafter": NodeRuntimeState(status="running", last_run_iteration=1),
        },
    )
    await fake_storage_provider.get_storage(GraphThread).create(thread)

    r = await client.get("/v1/graphs/g-3/runs/gt-1/node_states")
    assert r.status_code == 200, r.text
    by_id = {it["node_id"]: it for it in r.json()["items"]}
    assert by_id["begin"]["status"] == "ended"
    assert by_id["drafter"]["status"] == "running"
    assert by_id["end"]["status"] == "pending"


@pytest.mark.asyncio
async def test_node_states_404_for_unknown_run(
    client: httpx.AsyncClient, app, fake_storage_provider,
):
    from primer.model.graph import Graph

    await fake_storage_provider.get_storage(Graph).create(
        _seed_graph(fake_storage_provider, "g-4")
    )
    r = await client.get("/v1/graphs/g-4/runs/nope/node_states")
    assert r.status_code == 404
