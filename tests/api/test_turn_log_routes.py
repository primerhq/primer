"""REST tests for the three turn-log endpoints.

* GET /v1/sessions/{id}/turn_log               - workspace-backed read
* GET /v1/graphs/{gid}/runs/{rid}/turn_log     - graph-level (workspace or storage)
* GET /v1/graphs/{gid}/runs/{rid}/nodes/{nid}/turn_log
"""

from __future__ import annotations

from datetime import datetime, timezone

import httpx
import pytest

from primer.model.turn_log import TurnLogKind, TurnLogRecord


def _now() -> datetime:
    return datetime(2026, 6, 5, 10, 0, 0, tzinfo=timezone.utc)


class _FakeWorkspace:
    """A workspace double whose `read_file` returns scripted bytes.

    Exposes the public ``state_path`` property the route uses to build
    JSONL paths. Real backends resolve this via
    ``self.template.state_path`` (default ``.state``).
    """

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


@pytest.fixture
async def session_turn_log_setup(app, fake_storage_provider):
    """Seed a WorkspaceSession + fake workspace with a turns.jsonl."""
    from primer.model.workspace_session import (
        AgentSessionBinding,
        SessionStatus,
        WorkspaceSession,
    )

    sess = WorkspaceSession(
        id="sess-turn-log-1",
        workspace_id="ws-1",
        binding=AgentSessionBinding(agent_id="ag1"),
        status=SessionStatus.RUNNING,
        created_at=_now(),
        turn_status="running",
    )
    storage = fake_storage_provider.get_storage(WorkspaceSession)
    await storage.create(sess)

    ws = _FakeWorkspace()
    lines = [
        '{"seq":1,"kind":"started","ts":"2026-06-05T10:00:00Z","model":"m","input_message_count":1}',
        '{"seq":2,"kind":"completed","ts":"2026-06-05T10:00:05Z","duration_ms":5000,"finish_reason":"stop"}',
        '{"seq":3,"kind":"started","ts":"2026-06-05T10:01:00Z","model":"m","input_message_count":2}',
    ]
    ws.write(
        ".state/sessions/sess-turn-log-1/turns.jsonl",
        "\n".join(lines) + "\n",
    )

    # Inject the workspace into the registry. The real WorkspaceRegistry
    # has `get_workspace(workspace_id)`; patch it on the app.
    registry = app.state.workspace_registry

    async def _get(workspace_id: str):
        return ws if workspace_id == "ws-1" else None

    registry.get_workspace = _get  # type: ignore[assignment]
    return sess, ws


@pytest.mark.asyncio
async def test_session_turn_log_returns_events(
    client: httpx.AsyncClient, session_turn_log_setup,
):
    sess, _ws = session_turn_log_setup
    r = await client.get(f"/v1/sessions/{sess.id}/turn_log")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["total"] == 3
    assert len(body["items"]) == 3
    kinds = [i["kind"] for i in body["items"]]
    assert kinds == [
        TurnLogKind.STARTED.value,
        TurnLogKind.COMPLETED.value,
        TurnLogKind.STARTED.value,
    ]


@pytest.mark.asyncio
async def test_session_turn_log_pagination(
    client: httpx.AsyncClient, session_turn_log_setup,
):
    sess, _ = session_turn_log_setup
    r = await client.get(
        f"/v1/sessions/{sess.id}/turn_log",
        params={"limit": 2, "offset": 1},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["total"] == 3
    assert len(body["items"]) == 2
    assert body["items"][0]["seq"] == 2
    assert body["items"][1]["seq"] == 3


@pytest.mark.asyncio
async def test_session_turn_log_since_seq(
    client: httpx.AsyncClient, session_turn_log_setup,
):
    sess, _ = session_turn_log_setup
    r = await client.get(
        f"/v1/sessions/{sess.id}/turn_log",
        params={"since_seq": 2},
    )
    assert r.status_code == 200
    body = r.json()
    # Only seq=3 has seq > 2
    assert body["total"] == 1
    assert body["items"][0]["seq"] == 3


@pytest.mark.asyncio
async def test_session_turn_log_honours_custom_state_path(
    client: httpx.AsyncClient, app, fake_storage_provider,
):
    """Operators can override the default `.state` via WorkspaceTemplate;
    the route MUST honour the workspace's state_path so writer + reader
    agree on the file location."""
    from primer.model.workspace_session import (
        AgentSessionBinding,
        SessionStatus,
        WorkspaceSession,
    )

    sess = WorkspaceSession(
        id="sess-custom-state",
        workspace_id="ws-custom",
        binding=AgentSessionBinding(agent_id="ag1"),
        status=SessionStatus.RUNNING,
        created_at=_now(),
        turn_status="running",
    )
    await fake_storage_provider.get_storage(WorkspaceSession).create(sess)

    class _CustomWorkspace(_FakeWorkspace):
        state_path = ".meta/state"

    ws = _CustomWorkspace()
    ws.write(
        ".meta/state/sessions/sess-custom-state/turns.jsonl",
        '{"seq":1,"kind":"started","ts":"2026-06-05T10:00:00Z","model":"m","input_message_count":1}\n',
    )

    async def _get(workspace_id):
        return ws if workspace_id == "ws-custom" else None

    app.state.workspace_registry.get_workspace = _get  # type: ignore[assignment]
    r = await client.get("/v1/sessions/sess-custom-state/turn_log")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["total"] == 1
    assert body["items"][0]["kind"] == "started"


@pytest.mark.asyncio
async def test_session_turn_log_404_when_unknown(
    client: httpx.AsyncClient,
):
    r = await client.get("/v1/sessions/unknown-sess/turn_log")
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_session_turn_log_empty_when_no_file(
    client: httpx.AsyncClient, app, fake_storage_provider,
):
    """A session that hasn't written any events returns an empty page,
    not a 5xx."""
    from primer.model.workspace_session import (
        AgentSessionBinding,
        SessionStatus,
        WorkspaceSession,
    )

    sess = WorkspaceSession(
        id="sess-empty",
        workspace_id="ws-empty",
        binding=AgentSessionBinding(agent_id="ag1"),
        status=SessionStatus.CREATED,
        created_at=_now(),
        turn_status="idle",
    )
    await fake_storage_provider.get_storage(WorkspaceSession).create(sess)

    ws = _FakeWorkspace()  # no files written

    async def _get(workspace_id):
        return ws if workspace_id == "ws-empty" else None

    app.state.workspace_registry.get_workspace = _get  # type: ignore[assignment]
    r = await client.get("/v1/sessions/sess-empty/turn_log")
    assert r.status_code == 200
    body = r.json()
    assert body["total"] == 0
    assert body["items"] == []


@pytest.mark.asyncio
async def test_graph_run_turn_log_workspace_backed(
    client: httpx.AsyncClient, app, fake_storage_provider,
):
    """A workspace-backed graph run reads .state/graphs/<gsid>/turns.jsonl."""
    from primer.model.workspace_session import (
        GraphSessionBinding,
        SessionStatus,
        WorkspaceSession,
    )

    sess = WorkspaceSession(
        id="sess-graph-run-1",
        workspace_id="ws-graph",
        binding=GraphSessionBinding(graph_id="g-1"),
        status=SessionStatus.RUNNING,
        created_at=_now(),
        turn_status="running",
    )
    await fake_storage_provider.get_storage(WorkspaceSession).create(sess)

    ws = _FakeWorkspace()
    ws.write(
        ".state/graphs/sess-graph-run-1/turns.jsonl",
        '{"seq":1,"kind":"superstep_started","ts":"2026-06-05T10:00:00Z","iteration":0,"superstep_id":"ss-0-a","ready_node_ids":["begin"]}\n'
        '{"seq":2,"kind":"superstep_ended","ts":"2026-06-05T10:00:01Z","iteration":0,"superstep_id":"ss-0-a","completed_node_ids":["begin"],"failed_node_ids":[],"duration_ms":1000}\n',
    )
    ws.write(
        ".state/graphs/sess-graph-run-1/nodes/begin/turns.jsonl",
        '{"seq":1,"kind":"started","ts":"2026-06-05T10:00:00Z","node_id":"begin","iteration":0,"superstep_id":"ss-0-a"}\n'
        '{"seq":2,"kind":"completed","ts":"2026-06-05T10:00:01Z","node_id":"begin","iteration":0,"superstep_id":"ss-0-a","duration_ms":900}\n',
    )

    async def _get(wid):
        return ws if wid == "ws-graph" else None

    app.state.workspace_registry.get_workspace = _get  # type: ignore[assignment]

    # Graph-level endpoint
    r = await client.get("/v1/graphs/g-1/runs/sess-graph-run-1/turn_log")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["total"] == 2
    kinds = [i["kind"] for i in body["items"]]
    assert TurnLogKind.SUPERSTEP_STARTED.value in kinds
    assert TurnLogKind.SUPERSTEP_ENDED.value in kinds

    # Per-node endpoint
    r2 = await client.get(
        "/v1/graphs/g-1/runs/sess-graph-run-1/nodes/begin/turn_log",
    )
    assert r2.status_code == 200, r2.text
    body2 = r2.json()
    assert body2["total"] == 2
    assert all(i["node_id"] == "begin" for i in body2["items"])


@pytest.mark.asyncio
async def test_graph_run_turn_log_404_for_unknown(
    client: httpx.AsyncClient,
):
    r = await client.get("/v1/graphs/g-x/runs/unknown-run/turn_log")
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_graph_run_turn_log_storage_backed(
    client: httpx.AsyncClient, app, fake_storage_provider,
):
    """A GraphThread-backed run queries TurnLogRecord storage."""
    from primer.model.graph import GraphThread

    thread = GraphThread(
        id="gt-storage-1",
        graph_id="g-stor",
        title="t",
        created_at=_now(),
        last_activity_at=_now(),
    )
    await fake_storage_provider.get_storage(GraphThread).create(thread)

    # Seed a couple of TurnLogRecord rows: one graph-level (node_id=None)
    # and one per-node (node_id="A").
    log_storage = fake_storage_provider.get_storage(TurnLogRecord)
    await log_storage.create(TurnLogRecord(
        id="tlr-1",
        run_id="gt-storage-1",
        node_id=None,
        seq=1,
        kind=TurnLogKind.SUPERSTEP_STARTED,
        iteration=0,
        superstep_id="ss-0-x",
        payload={"ready_node_ids": ["A"]},
        created_at=_now(),
    ))
    await log_storage.create(TurnLogRecord(
        id="tlr-2",
        run_id="gt-storage-1",
        node_id="A",
        seq=1,
        kind=TurnLogKind.STARTED,
        iteration=0,
        superstep_id="ss-0-x",
        payload={"model": "m", "input_message_count": 1},
        created_at=_now(),
    ))

    r = await client.get("/v1/graphs/g-stor/runs/gt-storage-1/turn_log")
    assert r.status_code == 200, r.text
    body = r.json()
    # Only graph-level (node_id IS NULL) rows
    assert body["total"] == 1
    assert body["items"][0]["kind"] == TurnLogKind.SUPERSTEP_STARTED.value
    assert body["items"][0]["ready_node_ids"] == ["A"]

    # Per-node endpoint
    r2 = await client.get(
        "/v1/graphs/g-stor/runs/gt-storage-1/nodes/A/turn_log",
    )
    assert r2.status_code == 200, r2.text
    body2 = r2.json()
    assert body2["total"] == 1
    item = body2["items"][0]
    assert item["node_id"] == "A"
    assert item["model"] == "m"
