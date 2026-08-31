"""turn.started + graph.node_entered/exited from the dispatch seams."""
from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest_asyncio

from primer.model.provider import SqliteConfig
from primer.model.workspace_session import (
    AgentSessionBinding,
    SessionMessageKind,
    SessionStatus,
    WorkspaceSession,
)
from primer.session.dispatch import _emit_graph_transition
from primer.storage.sqlite import SqliteStorageProvider

# asyncio_mode = "auto" in pyproject.toml: async tests need no marker.


@pytest_asyncio.fixture
async def sp(tmp_path: Path) -> AsyncIterator[SqliteStorageProvider]:
    provider = SqliteStorageProvider(SqliteConfig(path=str(tmp_path / "t.sqlite")))
    await provider.initialize()
    await provider.get_event_store().ensure_schema()
    try:
        yield provider
    finally:
        await provider.aclose()


def _session() -> WorkspaceSession:
    return WorkspaceSession(
        id="sess-g", workspace_id="w",
        binding=AgentSessionBinding(agent_id="agent-a"),
        status=SessionStatus.RUNNING,
        created_at=datetime.now(timezone.utc),
    )


def _rec(phase: str, *, status: str | None = None) -> SimpleNamespace:
    return SimpleNamespace(
        kind=SessionMessageKind.GRAPH_TRANSITION,
        payload={
            "node_id": "work", "node_kind": "agent",
            "phase": phase, "status": status,
        },
    )


async def test_enter_and_exit_map_to_events(sp):
    deps = SimpleNamespace(storage_provider=sp, event_bus=None)
    await _emit_graph_transition(deps, _session(), _rec("enter"))
    await _emit_graph_transition(deps, _session(), _rec("exit", status="ok"))

    events = await sp.get_event_store().read_after(0)
    assert [e.event_type for e in events] == [
        "graph.node_entered", "graph.node_exited",
    ]
    assert events[0].payload == {
        "graph_node_id": "work", "node_kind": "agent",
    }
    assert events[1].payload["status"] == "ok"
    assert all(e.session_id == "sess-g" for e in events)


async def test_non_lifecycle_phase_is_ignored(sp):
    deps = SimpleNamespace(storage_provider=sp, event_bus=None)
    await _emit_graph_transition(deps, _session(), _rec("resumed"))
    assert await sp.get_event_store().read_after(0) == []
