"""01a0690a piece 2 — persist_resume_tool_result_record_for_graph.

A resumed graph yield's ``agent_tool_result`` (ask_user answer / unwound
nested continuation) is a pure in-memory Message built purely for LLM
continuation — it has no corresponding StreamEvent, so nothing in the live
tap pipeline ever records it. These tests drive
``persist_resume_tool_result_record_for_graph`` directly (a standalone
module function, not a full ``resume_graph_engine`` orchestration) against
lightweight fakes for the pool's storage/workspace/event-bus deps, mirroring
the direct-call style ``session_resume_coordinator``'s sibling function
(``_persist_resume_tool_result_record``, 0b4e8bfc) is already unit-tested
with in ``test_engine_session_resume.py``.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from primer.model.chat import Message, ToolResultPart
from primer.model.workspace_session import (
    GraphSessionBinding,
    SessionStatus,
    WorkspaceSession,
)
from primer.worker import graph_resume_coordinator


async def _async_return(value):
    return value


class _FakeEventBus:
    def __init__(self) -> None:
        self.published: list[tuple[str, dict]] = []

    async def publish(self, key: str, payload: dict) -> None:
        self.published.append((key, payload))


class _FakeWorkspaceIO:
    def __init__(self) -> None:
        self.lines: list[tuple[str, bytes]] = []

    async def append_message_line(self, session_id: str, line: bytes) -> None:
        self.lines.append((session_id, line))


class _FakeSessionStorage:
    def __init__(self) -> None:
        self.updated: list[WorkspaceSession] = []

    async def update(self, session: WorkspaceSession) -> None:
        self.updated.append(session)


class _FakeStorage:
    def __init__(self, session_storage: _FakeSessionStorage) -> None:
        self._session_storage = session_storage

    def get_storage(self, model_cls):
        assert model_cls is WorkspaceSession
        return self._session_storage


class _FakePool:
    def __init__(self, *, workspace_io, storage, event_bus=None) -> None:
        self._storage = storage
        self._event_bus = event_bus
        self._workspace_io = workspace_io

    async def _load_workspace_for_persist(self, workspace_id: str):
        return self._workspace_io


def _make_graph_session(*, last_seq: int = 5) -> WorkspaceSession:
    return WorkspaceSession(
        id="gs-1",
        workspace_id="ws-1",
        binding=GraphSessionBinding(graph_id="g-1"),
        status=SessionStatus.RUNNING,
        created_at=datetime.now(timezone.utc),
        last_seq=last_seq,
    )


def _make_agent_tool_result(tcid: str = "tc-raw") -> Message:
    return Message(
        role="tool",
        parts=[ToolResultPart(id=tcid, output="Alice", error=False)],
    )


@pytest.mark.asyncio
async def test_writes_tool_result_with_scoped_call_id_and_node_tag():
    ws = _FakeWorkspaceIO()
    session_storage = _FakeSessionStorage()
    pool = _FakePool(workspace_io=ws, storage=_FakeStorage(session_storage))
    session = _make_graph_session()
    checkpoint = {
        "pending_agent_yields": [
            {
                "node_id": "asker",
                "tool_call_id": "tc-raw",
                "scoped_tool_call_id": "asker:tool:0:1",
            },
        ],
    }

    await graph_resume_coordinator.persist_resume_tool_result_record_for_graph(
        pool, session=session, checkpoint=checkpoint, tcid="tc-raw",
        agent_tool_result=_make_agent_tool_result("tc-raw"),
    )

    assert len(ws.lines) == 1
    _sid, blob = ws.lines[0]
    record = json.loads(blob.decode().splitlines()[0])
    assert record["kind"] == "tool_result"
    assert record["payload"]["call_id"] == "asker:tool:0:1"
    assert record["payload"]["output"] == "Alice"
    assert record["node_id"] == "asker"
    assert session_storage.updated
    assert session_storage.updated[-1].last_seq > session.last_seq


@pytest.mark.asyncio
async def test_falls_back_to_raw_tcid_without_scoped_id():
    """A checkpoint written before piece 1 has no scoped_tool_call_id."""
    ws = _FakeWorkspaceIO()
    session_storage = _FakeSessionStorage()
    pool = _FakePool(workspace_io=ws, storage=_FakeStorage(session_storage))
    session = _make_graph_session()
    checkpoint = {
        "pending_agent_yields": [
            {"node_id": "asker", "tool_call_id": "tc-raw"},
        ],
    }

    await graph_resume_coordinator.persist_resume_tool_result_record_for_graph(
        pool, session=session, checkpoint=checkpoint, tcid="tc-raw",
        agent_tool_result=_make_agent_tool_result("tc-raw"),
    )

    record = json.loads(ws.lines[0][1].decode().splitlines()[0])
    assert record["payload"]["call_id"] == "tc-raw"


@pytest.mark.asyncio
async def test_publishes_tick():
    ws = _FakeWorkspaceIO()
    session_storage = _FakeSessionStorage()
    event_bus = _FakeEventBus()
    pool = _FakePool(
        workspace_io=ws, storage=_FakeStorage(session_storage), event_bus=event_bus,
    )
    session = _make_graph_session()
    checkpoint = {
        "pending_agent_yields": [
            {"node_id": "asker", "tool_call_id": "tc-raw", "scoped_tool_call_id": "x"},
        ],
    }

    await graph_resume_coordinator.persist_resume_tool_result_record_for_graph(
        pool, session=session, checkpoint=checkpoint, tcid="tc-raw",
        agent_tool_result=_make_agent_tool_result("tc-raw"),
    )

    assert len(event_bus.published) == 1
    key, payload = event_bus.published[0]
    assert key == f"session:{session.id}:tick"
    assert "seq" in payload


@pytest.mark.asyncio
async def test_none_agent_tool_result_is_noop():
    """No agent_tool_result -> nothing to write (approval-gate resumes,
    which re-run for real and get tapped by piece 3 instead)."""
    ws = _FakeWorkspaceIO()
    session_storage = _FakeSessionStorage()
    pool = _FakePool(workspace_io=ws, storage=_FakeStorage(session_storage))
    session = _make_graph_session()

    await graph_resume_coordinator.persist_resume_tool_result_record_for_graph(
        pool, session=session, checkpoint={}, tcid="tc-raw", agent_tool_result=None,
    )

    assert ws.lines == []
    assert session_storage.updated == []
