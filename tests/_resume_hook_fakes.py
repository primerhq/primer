"""Fakes shared by the graph resume tests (no database, no real pool).

Five groups, each moved here from the test module that first defined it so a
test that needs several of them imports one helper instead of other test modules:

* the ``ask_user`` tool_call graph and its executor (from
  ``tests/graph/test_toolcall_ask_user_value_resume.py``): ``build_ask_user_graph``,
  ``make_toolcall_executor``, ``drain``, ``drain_until_yield``;
* the resume-drain tap's pool and session fakes (from
  ``tests/worker/test_pool_graph_resume.py``): ``DrainTapPool``,
  ``RecordingWorkspaceIO``, ``FakeSessionRow``, ``FakeSessionStorage``, ``FakeStorage``;
* the ``resume_graph_engine`` pool fakes (from
  ``tests/worker/test_resume_graph_tool_wait.py``): ``EngineFakePool``,
  ``NullWorkspaceIO``, ``EngineStorageProvider``, ``waiting_graph_session``;
* the provider registries a resume hook reaches through ``ResumeContext.resolve_provider`` (from
  ``tests/worker/test_graph_toolcall_value_yield_context.py``): ``IdentityToolsetRegistry``,
  ``PythonToolsetRegistry`` (and its ``ResumeOnlyPythonProvider``);
* the pool slice the continuation walk's invocation services read (from
  ``tests/worker/test_child_graph_value_yield_resume.py``): ``AgentNodeHookPool``.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any

from primer.graph.executor import GraphExecutor
from primer.model.agent import Agent
from primer.model.chat import Message, StreamEvent, ToolCallResult, ToolResultPart
from primer.model.graph import Graph, _BeginNode, _EndNode, _StaticEdge, _ToolCallNode
from primer.model.workspace_session import AgentSessionBinding, SessionStatus, WorkspaceSession
from primer.model.yield_ import YieldToWorker
from primer.toolset.python_runner.provider import PythonToolsetProvider
from primer.worker import graph_resume_coordinator
from primer.worker.pool import WorkerPool

from tests.conftest import _FakeStorageProvider


# ---------------------------------------------------------------------------
# the ask_user tool_call graph
# ---------------------------------------------------------------------------


async def drain_until_yield(
    it: AsyncIterator[StreamEvent],
) -> tuple[list[StreamEvent], YieldToWorker | None]:
    events: list[StreamEvent] = []
    try:
        async for ev in it:
            events.append(ev)
    except YieldToWorker as exc:
        return events, exc
    return events, None


async def drain(it: AsyncIterator[StreamEvent]) -> list[StreamEvent]:
    return [ev async for ev in it]


def build_ask_user_graph() -> Graph:
    return Graph(
        id="g-ask-user-value",
        description="begin -> tool(ask_user) -> end",
        nodes=[
            _BeginNode(id="begin"),
            _ToolCallNode(
                id="ask",
                tool_id="system__ask_user",
                arguments={"prompt": "Approve access?"},
            ),
            _EndNode(id="exit", output_template="{{ nodes.ask.text }}"),
        ],
        edges=[
            _StaticEdge(from_node="begin", to_node="ask"),
            _StaticEdge(from_node="ask", to_node="exit"),
        ],
    )


async def _agent_resolver(agent_id: str) -> Agent:
    raise KeyError(agent_id)


async def _llm_resolver(agent):  # pragma: no cover - never reached
    raise NotImplementedError


def make_toolcall_executor(graph, thread, thread_storage, message_storage, dispatcher):
    return GraphExecutor(
        graph=graph,
        agent_resolver=_agent_resolver,
        llm_resolver=_llm_resolver,  # type: ignore[arg-type]
        thread_storage=thread_storage,  # type: ignore[arg-type]
        message_storage=message_storage,  # type: ignore[arg-type]
        graph_thread_id=thread.id,
        tool_dispatcher=dispatcher,
    )


# ---------------------------------------------------------------------------
# the resume-drain tap's pool (resume_graph_from_checkpoint with pool/session)
# ---------------------------------------------------------------------------


class RecordingWorkspaceIO:
    def __init__(self) -> None:
        self.lines: list[tuple[str, bytes]] = []

    async def append_message_line(self, session_id: str, line: bytes) -> None:
        self.lines.append((session_id, line))


class FakeSessionRow:
    def __init__(self, *, sid: str, workspace_id: str, turn_no: int, last_seq: int) -> None:
        self.id = sid
        self.workspace_id = workspace_id
        self.turn_no = turn_no
        self.last_seq = last_seq

    def model_copy(self, *, update: dict):
        merged = {**self.__dict__, **update}
        return FakeSessionRow(
            sid=merged["id"], workspace_id=merged["workspace_id"],
            turn_no=merged["turn_no"], last_seq=merged["last_seq"],
        )


class FakeSessionStorage:
    def __init__(self, row) -> None:
        self._row = row

    async def get(self, sid: str):
        return self._row if self._row.id == sid else None

    async def update(self, row) -> None:
        self._row = row

    async def patch_if(self, id: str, patch: dict, *, where: dict, set_paths=None, conn=None):
        """The field-scoped fenced write ``advance_last_seq`` makes (the drain tap's ``finish``): applied when every field of ``where`` holds one of the listed values, else ``None``."""
        from primer.model.except_ import NotFoundError

        if self._row.id != id:
            raise NotFoundError(id)
        if any(getattr(self._row, field) not in allowed for field, allowed in where.items()):
            return None
        for field, value in patch.items():
            setattr(self._row, field, value)
        return self._row


class FakeStorage:
    def __init__(self, session_storage) -> None:
        self._session_storage = session_storage

    def get_storage(self, _model_cls):
        return self._session_storage


class NoopClaimEngine:
    """Stands in for WorkerPool._engine: the tests that use these pools exercise the drain tap's persistence or
    readiness / repark routing, not row-creation content, so upserts are discarded."""

    async def upsert(self, kind, entity_id: str, **kwargs) -> None:
        return None


class DrainTapPool:
    def __init__(self, *, workspace_io, storage) -> None:
        self._storage = storage
        self._event_bus = None
        self._workspace_io = workspace_io
        self._engine = NoopClaimEngine()

    async def _load_workspace_for_persist(self, _workspace_id: str):
        return self._workspace_io


# ---------------------------------------------------------------------------
# the resume_graph_engine pool
# ---------------------------------------------------------------------------


def _now() -> datetime:
    return datetime.now(timezone.utc)


def waiting_graph_session(session_id: str = "gs-1") -> WorkspaceSession:
    return WorkspaceSession(
        id=session_id, workspace_id="ws-1", binding=AgentSessionBinding(agent_id="ag1"),
        status=SessionStatus.WAITING, created_at=_now(), turn_no=0, parked_at=_now(),
    )


class EngineStorageProvider:
    """Routes every model through a real _FakeStorageProvider."""

    def __init__(self) -> None:
        self._inner = _FakeStorageProvider()

    def get_storage(self, model_cls):
        return self._inner.get_storage(model_cls)


class EngineFakePool:
    def __init__(self, *, storage, workspace_io, executor_factory) -> None:
        self._storage = storage
        self._workspace_io = workspace_io
        self._event_bus = None
        self._engine = NoopClaimEngine()
        self._executor_factory = executor_factory
        self.end_session_calls: list[str] = []
        self.repark_calls: list = []
        self.agent_tool_result_session_ids: list = []
        self.agent_tool_result_tcids: list[str] = []   # every reply the engine delivered, by its tool_call_id
        self.agent_tool_result_event_keys: list = []   # ... and by the event key that fired (None for a key-less drain)
        self.approval_record_event_keys: list = []     # the fired key of every approval record the engine asked for

    async def _load_workspace_for_persist(self, workspace_id: str):
        return self._workspace_io

    async def _build_graph_executor(self, session, workspace):
        return await self._executor_factory()

    async def _end_session(self, session, *, reason: str):
        self.end_session_calls.append(reason)
        return f"ENDED:{reason}"

    def _repark_graph_outcome(self, session, repark, *, node_tool_call_seq=None):
        self.repark_calls.append(repark)
        return "REPARKED"

    # -- resume_graph_engine's own delegating surface --------------------
    def _graph_nested_agent_yield(self, checkpoint, tcid, event_key=None):
        # The fired key is forwarded only when there is one: the coordinator's own helpers took the tool_call_id alone before the engine was taught to
        # select by event key, and a key-less drain (the legacy path) still calls them that way.
        extra = {"event_key": event_key} if event_key is not None else {}
        return graph_resume_coordinator.graph_nested_agent_yield(self, checkpoint, tcid, **extra)

    def _graph_value_yield_toolcall(self, checkpoint, tcid, event_key=None):
        extra = {"event_key": event_key} if event_key is not None else {}
        return graph_resume_coordinator.graph_value_yield_toolcall(self, checkpoint, tcid, **extra)

    async def _graph_agent_tool_result(self, checkpoint, tcid, payload, *, session_id, event_key=None):
        # Directly supplies the ask_user answer, bypassing the global
        # resume-hook registry - irrelevant to what these tests prove (the
        # real hook call is pinned by test_graph_agent_tool_result_real_hooks.py).
        # It does record the session id the engine passes, because the hook's
        # ResumeContext is built from it.
        self.agent_tool_result_session_ids.append(session_id)
        self.agent_tool_result_tcids.append(tcid)
        self.agent_tool_result_event_keys.append(event_key)
        # ... but it answers only where the real lookup does (graph_resume_coordinator.graph_agent_tool_result): None when the checkpoint has no pending agent
        # yield for the fired tool_call_id / event key, or when that yield is an approval (the decision is applied by the coordinator's bypass dispatch, not by
        # a reply). tests/worker/test_engine_fake_pool_graph_reply.py checks the two against each other.
        from primer.session.pending_gates import pending_entries

        matches = pending_entries(checkpoint, "pending_agent_yields", tool_call_id=tcid, event_key=event_key)
        ay = matches[0] if matches else None
        if ay is None or ay.get("tool_name") in (None, "_approval"):
            return None
        return Message(role="tool", parts=[ToolResultPart(id=tcid, output="blue")])

    async def _write_approval_record_for_graph(self, *, session, checkpoint, tcid, payload, event_key=None):
        self.approval_record_event_keys.append(event_key)
        return None

    async def _persist_resume_tool_result_record_for_graph(
        self, *, session, checkpoint, tcid, agent_tool_result, event_key=None,
    ):
        return None

    async def _resume_graph_continuation(self, *args, **kwargs):
        raise AssertionError("no nested continuation in these tests")


class NullWorkspaceIO:
    async def append_message_line(self, session_id: str, line: bytes) -> None:
        return None


# ---------------------------------------------------------------------------
# provider registries a resume hook reaches through ResumeContext.resolve_provider
# ---------------------------------------------------------------------------


class IdentityToolsetRegistry:
    """``get_toolset`` hands back the id it was asked for, so a test can tell the resolver reaches the registry."""

    async def get_toolset(self, toolset_id: str):
        return toolset_id


class ResumeOnlyPythonProvider(PythonToolsetProvider):
    def __init__(self) -> None:  # no runner or source: only the resume half is exercised
        pass

    async def resume_tool(self, *, tool_id: str, payload: Any, resume_metadata: dict[str, Any]) -> ToolCallResult:
        return ToolCallResult(output=json.dumps({"tool_id": tool_id, "answer": payload["response"]}), is_error=False)


class PythonToolsetRegistry:
    """Resolves the toolset ``ts-vy`` to a :class:`ResumeOnlyPythonProvider`, anything else to ``None``."""

    async def get_toolset(self, toolset_id: str):
        return ResumeOnlyPythonProvider() if toolset_id == "ts-vy" else None


# ---------------------------------------------------------------------------
# the pool slice build_invocation_services reads
# ---------------------------------------------------------------------------


class AgentNodeHookPool(SimpleNamespace):
    """The slice of ``WorkerPool`` the invocation services read, with the real agent-node hook seam."""

    async def _graph_agent_tool_result(self, checkpoint, tcid, payload, *, session_id, event_key=None):
        return await WorkerPool._graph_agent_tool_result(self, checkpoint, tcid, payload, session_id=session_id, event_key=event_key)
