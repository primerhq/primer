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
async def test_node_states_fanout_instances_are_not_pending_forever(
    client: httpx.AsyncClient, app, fake_storage_provider,
):
    """01a05935 item 1: a fan-out target ("worker") runs as N instances
    keyed "worker[0]"/"worker[1]"/etc in the state map
    (_FanoutInstance.synthesized_id) - a bare-id lookup against
    state_map["worker"] never matches any of them, so every fan-out
    node showed "pending" forever regardless of its real status. Each
    instance must surface as its own row, grouped under the base node's
    declared kind."""
    from primer.model.graph import Graph

    graph = Graph(
        id="g-fanout",
        description="probe",
        nodes=[
            {"kind": "begin", "id": "begin"},
            {
                "kind": "fan_out", "id": "spread",
                "specs": [
                    {"kind": "broadcast", "target_node_id": "worker", "count": 3},
                ],
            },
            {"kind": "agent", "id": "worker", "agent_id": "ag1"},
            {"kind": "fan_in", "id": "gather"},
            {"kind": "end", "id": "end", "output_template": ""},
        ],
        edges=[
            {"kind": "static", "from_node": "begin", "to_node": "spread"},
            {"kind": "static", "from_node": "worker", "to_node": "gather"},
            {"kind": "static", "from_node": "gather", "to_node": "end"},
        ],
    )
    await fake_storage_provider.get_storage(Graph).create(graph)

    from primer.model.workspace_session import (
        GraphSessionBinding, SessionStatus, WorkspaceSession,
    )

    sess = WorkspaceSession(
        id="run-fanout-1",
        workspace_id="ws-fanout",
        binding=GraphSessionBinding(graph_id="g-fanout"),
        status=SessionStatus.RUNNING,
        created_at=_now(),
        turn_status="running",
    )
    await fake_storage_provider.get_storage(WorkspaceSession).create(sess)

    ws = _FakeWorkspace()
    ws.write(
        ".state/graphs/run-fanout-1/state.json",
        '{"iteration":1,"status":"running","ended_reason":null,'
        '"ended_detail":null,"node_states":{'
        '"begin":{"status":"ended","last_run_iteration":0,'
        '"last_run_at":"2026-06-05T10:00:00+00:00","error":null},'
        '"worker[0]":{"status":"ended","last_run_iteration":1,'
        '"last_run_at":"2026-06-05T10:00:05+00:00","error":null},'
        '"worker[1]":{"status":"running","last_run_iteration":1,'
        '"last_run_at":"2026-06-05T10:00:06+00:00","error":null},'
        '"worker[2]":{"status":"failed","last_run_iteration":1,'
        '"last_run_at":"2026-06-05T10:00:07+00:00",'
        '"error":"boom"}}}',
    )

    async def _get(wid):
        return ws if wid == "ws-fanout" else None

    app.state.workspace_registry.get_workspace = _get  # type: ignore[assignment]

    r = await client.get("/v1/graphs/g-fanout/runs/run-fanout-1/node_states")
    assert r.status_code == 200, r.text
    body = r.json()
    by_id = {it["node_id"]: it for it in body["items"]}

    # Three distinct instance rows, NOT a single "worker" row stuck on
    # pending, NOT collapsed/deduped into one entry.
    assert {"worker[0]", "worker[1]", "worker[2]"} <= set(by_id)
    assert "worker" not in by_id  # never ran under its bare id
    assert by_id["worker[0]"]["kind"] == "agent"
    assert by_id["worker[0]"]["status"] == "ended"
    assert by_id["worker[1]"]["kind"] == "agent"
    assert by_id["worker[1]"]["status"] == "running"
    assert by_id["worker[2]"]["kind"] == "agent"
    assert by_id["worker[2]"]["status"] == "failed"
    assert by_id["worker[2]"]["error"] == "boom"

    # Nodes downstream of the fan-out that never ran still surface as
    # a single pending row under their own bare id (no instances yet).
    assert by_id["gather"]["status"] == "pending"
    assert by_id["end"]["status"] == "pending"

    # Instance count matches exactly what ran - no phantom extra rows,
    # no dedup collapsing distinct siblings together.
    worker_rows = [it for it in body["items"] if it["node_id"].startswith("worker")]
    assert len(worker_rows) == 3


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


# ---------------------------------------------------------------------------
# 01a08bfb item 1: only "no state file yet" may read as a board of pending
# nodes. A crashed/unreadable run must not look identical to one that has not
# started, or an operator waits indefinitely on something already dead.
# ---------------------------------------------------------------------------


async def _seed_workspace_run(fake_storage_provider, app, gid, rid, ws):
    from primer.model.graph import Graph
    from primer.model.workspace_session import (
        GraphSessionBinding,
        SessionStatus,
        WorkspaceSession,
    )

    await fake_storage_provider.get_storage(Graph).create(
        _seed_graph(fake_storage_provider, gid)
    )
    await fake_storage_provider.get_storage(WorkspaceSession).create(
        WorkspaceSession(
            id=rid,
            workspace_id=f"ws-{rid}",
            binding=GraphSessionBinding(graph_id=gid),
            status=SessionStatus.RUNNING,
            created_at=_now(),
            turn_status="running",
        )
    )

    async def _get(wid):
        return ws if wid == f"ws-{rid}" else None

    app.state.workspace_registry.get_workspace = _get  # type: ignore[assignment]


@pytest.mark.asyncio
async def test_node_states_absent_state_file_is_a_run_not_started(
    client: httpx.AsyncClient, app, fake_storage_provider,
):
    """The ONE case that legitimately renders all-pending: no state.json yet."""
    await _seed_workspace_run(
        fake_storage_provider, app, "g-nf", "run-nf", _FakeWorkspace(),
    )
    r = await client.get("/v1/graphs/g-nf/runs/run-nf/node_states")
    assert r.status_code == 200, r.text
    assert {it["status"] for it in r.json()["items"]} == {"pending"}


@pytest.mark.asyncio
async def test_node_states_read_failure_is_an_error_not_pending(
    client: httpx.AsyncClient, app, fake_storage_provider,
):
    """A failed read (e.g. a network blip on a remote workspace) is not
    "has not started": it must surface as an error."""

    class _FlakyWorkspace(_FakeWorkspace):
        async def read_file(self, path: str) -> bytes:
            raise ConnectionError("runtime websocket dropped")

    await _seed_workspace_run(
        fake_storage_provider, app, "g-rf", "run-rf", _FlakyWorkspace(),
    )
    r = await client.get("/v1/graphs/g-rf/runs/run-rf/node_states")
    assert r.status_code == 500, r.text
    assert "run-rf" in r.json()["detail"]
    assert "ConnectionError" in r.json()["detail"]


@pytest.mark.asyncio
async def test_node_states_corrupt_state_file_is_an_error_not_pending(
    client: httpx.AsyncClient, app, fake_storage_provider,
):
    """A process that crashed mid-write leaves unparseable JSON."""
    ws = _FakeWorkspace()
    ws.write(".state/graphs/run-cj/state.json", '{"iteration":1,"node_sta')
    await _seed_workspace_run(fake_storage_provider, app, "g-cj", "run-cj", ws)
    r = await client.get("/v1/graphs/g-cj/runs/run-cj/node_states")
    assert r.status_code == 500, r.text
    assert "corrupt" in r.json()["detail"]


@pytest.mark.asyncio
async def test_node_states_malformed_payload_is_an_error_not_pending(
    client: httpx.AsyncClient, app, fake_storage_provider,
):
    ws = _FakeWorkspace()
    ws.write(".state/graphs/run-mp/state.json", '{"iteration":1}')
    await _seed_workspace_run(fake_storage_provider, app, "g-mp", "run-mp", ws)
    r = await client.get("/v1/graphs/g-mp/runs/run-mp/node_states")
    assert r.status_code == 500, r.text
    assert "malformed" in r.json()["detail"]


@pytest.mark.asyncio
async def test_node_states_missing_workspace_is_404_not_pending(
    client: httpx.AsyncClient, app, fake_storage_provider,
):
    """The run's workspace is gone: its state is unreadable, not "pending"."""
    await _seed_workspace_run(
        fake_storage_provider, app, "g-mw", "run-mw", _FakeWorkspace(),
    )

    async def _none(wid):
        return None

    app.state.workspace_registry.get_workspace = _none  # type: ignore[assignment]
    r = await client.get("/v1/graphs/g-mw/runs/run-mw/node_states")
    assert r.status_code == 404, r.text
    assert "ws-run-mw" in r.json()["detail"]
