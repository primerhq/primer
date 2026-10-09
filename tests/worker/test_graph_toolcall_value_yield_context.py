"""A graph ``tool_call`` node that value-yields hands its resume hook a real ``ResumeContext``.

The agent-session resume and the graph agent-node resume build the context from the session being resumed and the provider
registry (``session_id``, ``resolve_provider=registry.get_toolset``). The graph ``tool_call`` node path
(``_resume_value_yield_toolcall``) built a bare one, ``session_id=None, resolve_provider=None``, so a hook that needs either
(every python-toolset tool does: it reaches its provider through ``ctx.resolve_provider``) could not work from a graph
node. These tests drive the REAL ``resume_graph_from_checkpoint`` (the worker adapter both resume coordinators call) and the
REAL ``GraphExecutor.resume_from_checkpoint`` with a value-yielding test hook that records the context it receives.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any

import pytest

from primer.graph.base import _GraphErrorEvent
from primer.graph.executor import GraphExecutor
from primer.model.chat import ToolCallResult
from primer.model.graph import GraphNodeMessage, GraphThread, NodeRuntimeStatus
from primer.model.workspace_session import WorkspaceSession
from primer.model.yield_ import Yielded, YieldToWorker
from primer.toolset.python_runner.provider import python_tool_resume, scoped_tool_name
from primer.worker import graph_resume_coordinator
from primer.worker.graph_resume import resume_graph_from_checkpoint
from primer.worker.yield_resume_registry import ResumeContext, register_resume_hook
from primer.worker.yield_runtime import ParkedState

from tests._resume_hook_fakes import (
    DrainTapPool as _FakePool,
    EngineFakePool as _EngineFakePool,
    EngineStorageProvider as _StorageProvider,
    FakeSessionRow as _FakeSessionRow,
    FakeSessionStorage as _FakeSessionStorage,
    FakeStorage as _FakeStorage,
    IdentityToolsetRegistry as _Registry,
    NullWorkspaceIO as _EngineWorkspaceIO,
    PythonToolsetRegistry as _PythonRegistry,
    RecordingWorkspaceIO as _FakeWorkspaceIO,
    build_ask_user_graph as _build_graph,
    drain as _drain,
    drain_until_yield as _drain_until_yield,
    make_toolcall_executor as _make_executor,
    waiting_graph_session as _session,
)
from tests.graph.test_toolcall_dispatch import _InMemoryStorage

_TCID = "tc-vy"


class _PoolWithRegistry(_FakePool):
    def __init__(self, *, registry: Any, workspace_io: Any, storage: Any) -> None:
        super().__init__(workspace_io=workspace_io, storage=storage)
        self._provider_registry = registry


def _recording_hook(tool_name: str) -> list[ResumeContext]:
    seen: list[ResumeContext] = []

    def hook(meta, payload, ctx: ResumeContext) -> ToolCallResult:
        seen.append(ctx)
        return ToolCallResult(output=json.dumps({"response": payload["response"]}), is_error=False)

    register_resume_hook(tool_name, hook)
    return seen


async def _parked(tool_name: str, resume_metadata: dict[str, Any] | None = None):
    """Park the ``ask`` tool_call node on a value-yield under ``tool_name``; return what a resume needs."""
    graph = _build_graph()

    async def first_dispatcher(node, arguments):
        raise YieldToWorker(
            Yielded(
                tool_name=tool_name, event_key=f"{tool_name}:s:{_TCID}", resume_metadata=resume_metadata or {"q": "?"},
            ),
            tool_call_id=_TCID,
        )

    async def resume_dispatcher(node, arguments, bypass_approval=False):  # pragma: no cover - must not re-dispatch
        raise AssertionError("a value-yielding tool_call must not be re-dispatched")

    thread_storage: _InMemoryStorage[GraphThread] = _InMemoryStorage(GraphThread)
    message_storage: _InMemoryStorage[GraphNodeMessage] = _InMemoryStorage(GraphNodeMessage)
    thread = await GraphExecutor.open_thread(graph=graph, thread_storage=thread_storage)  # type: ignore[arg-type]
    parker = _make_executor(graph, thread, thread_storage, message_storage, first_dispatcher)
    _events, raised = await _drain_until_yield(parker.invoke([]))
    assert raised is not None
    checkpoint = parker.snapshot_state()
    resumer = _make_executor(graph, thread, thread_storage, message_storage, resume_dispatcher)
    return checkpoint, resumer, raised


def _pool_and_session(registry: Any) -> tuple[_PoolWithRegistry, _FakeSessionRow]:
    row = _FakeSessionRow(sid="gs-vy", workspace_id="ws-1", turn_no=1, last_seq=0)
    pool = _PoolWithRegistry(
        registry=registry, workspace_io=_FakeWorkspaceIO(), storage=_FakeStorage(_FakeSessionStorage(row)),
    )
    return pool, row


@pytest.mark.asyncio
async def test_the_worker_adapter_gives_the_hook_the_session_and_the_registry():
    seen = _recording_hook("test_vy_ctx_adapter")
    checkpoint, resumer, _raised = await _parked("test_vy_ctx_adapter")
    registry = _Registry()
    pool, session = _pool_and_session(registry)

    decision, repark, _seq = await resume_graph_from_checkpoint(
        executor=resumer, checkpoint=checkpoint, payload={"response": "blue"}, resumed_tcid=_TCID,
        pool=pool, session=session,  # type: ignore[arg-type]
    )

    assert repark is None
    (ctx,) = seen
    assert (ctx.tool_name, ctx.tool_call_id) == ("test_vy_ctx_adapter", _TCID)
    assert ctx.session_id == "gs-vy", "the hook was not told which session it is answering"
    assert ctx.resolve_provider == registry.get_toolset, "a python toolset's hook could not reach its provider"
    assert json.loads(resumer._context.nodes["ask"].text) == {"response": "blue"}


@pytest.mark.asyncio
async def test_a_pool_without_a_provider_registry_still_names_the_session():
    seen = _recording_hook("test_vy_ctx_no_registry")
    checkpoint, resumer, _raised = await _parked("test_vy_ctx_no_registry")
    pool, session = _pool_and_session(None)

    await resume_graph_from_checkpoint(
        executor=resumer, checkpoint=checkpoint, payload={"response": "x"}, resumed_tcid=_TCID,
        pool=pool, session=session,  # type: ignore[arg-type]
    )

    assert seen[0].resolve_provider is None
    assert seen[0].session_id == "gs-vy"


@pytest.mark.asyncio
async def test_the_executor_hands_the_hook_what_its_caller_passes():
    seen = _recording_hook("test_vy_ctx_executor")
    checkpoint, resumer, _raised = await _parked("test_vy_ctx_executor")
    registry = _Registry()

    await _drain(resumer.resume_from_checkpoint(
        checkpoint, resumed_tcid=_TCID, toolcall_payload={"response": "x"},
        resume_session_id="sess-direct", resolve_provider=registry.get_toolset,
    ))

    (ctx,) = seen
    assert ctx.session_id == "sess-direct"
    assert ctx.resolve_provider == registry.get_toolset


@pytest.mark.asyncio
async def test_a_caller_that_passes_neither_gets_the_bare_context():
    """The direct executor callers (tests) hold no session or registry."""
    seen = _recording_hook("test_vy_ctx_bare")
    checkpoint, resumer, _raised = await _parked("test_vy_ctx_bare")

    await _drain(resumer.resume_from_checkpoint(checkpoint, resumed_tcid=_TCID, toolcall_payload={"response": "x"}))

    assert seen[0].session_id is None and seen[0].resolve_provider is None


class _EnginePool(_EngineFakePool):
    """The engine test pool, with the REAL agent-node hook seam (a tool_call yield resolves to None there) and a registry."""

    _provider_registry: Any = None

    async def _graph_agent_tool_result(self, checkpoint, tcid, payload, *, session_id, event_key=None):
        return await graph_resume_coordinator.graph_agent_tool_result(
            self, checkpoint, tcid, payload, session_id=session_id, event_key=event_key,
        )


@pytest.mark.asyncio
async def test_the_engine_resume_gives_the_hook_the_session_and_the_registry(caplog):
    """``resume_graph_engine`` (the coordinator behind every ask_user reply to a graph) reaches the hook with both."""
    seen = _recording_hook("test_vy_ctx_engine")
    checkpoint, resumer, raised = await _parked("test_vy_ctx_engine")
    registry = _Registry()

    async def factory():
        return resumer

    storage = _StorageProvider()
    pool = _EnginePool(storage=storage, workspace_io=_EngineWorkspaceIO(), executor_factory=factory)
    pool._provider_registry = registry
    session = _session("gs-engine")
    session.parked_state = {"resume_event_key": f"test_vy_ctx_engine:s:{_TCID}"}
    # the resume-drain tap's flush writes last_seq onto the stored row; without one it logs a swallowed NotFoundError
    await storage.get_storage(WorkspaceSession).create(session)
    parked = ParkedState(
        yielded=raised.yielded, llm_messages=[], turn_no=0, started_at=datetime.now(timezone.utc),
        tool_call_id=_TCID, resume_event_payload={"response": "blue"}, graph_checkpoint=checkpoint,
    )

    with caplog.at_level(logging.WARNING):
        outcome = await graph_resume_coordinator.resume_graph_engine(pool, session, parked)

    assert outcome == "ENDED:completed"
    (ctx,) = seen
    assert ctx.session_id == "gs-engine"
    assert ctx.resolve_provider == registry.get_toolset
    errors = [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert not errors, f"the resume logged an error it swallowed: {[r.getMessage() for r in errors]}"


@pytest.mark.asyncio
async def test_a_python_toolset_yield_on_a_graph_tool_call_node_resumes():
    """The real ``python_tool_resume`` hook is async and reaches its provider through ``ctx.resolve_provider``."""
    name = scoped_tool_name("ts-vy", "ask")
    register_resume_hook(name, python_tool_resume)
    checkpoint, resumer, _raised = await _parked(name, {"toolset_id": "ts-vy", "tool_id": "ask"})
    pool, session = _pool_and_session(_PythonRegistry())

    _decision, repark, _seq = await resume_graph_from_checkpoint(
        executor=resumer, checkpoint=checkpoint, payload={"response": "blue"}, resumed_tcid=_TCID,
        pool=pool, session=session,  # type: ignore[arg-type]
    )

    assert repark is None
    node = resumer._context.nodes["ask"]
    assert node.error is None, f"the python tool's resume failed the node: {node.error}"
    assert json.loads(node.text) == {"tool_id": "ask", "answer": "blue"}


def _register(tool_name: str, *, is_async: bool, raises: Exception | None = None, result: ToolCallResult | None = None):
    """Register a value-yield hook that raises ``raises`` or returns ``result``, as a sync or an async function."""

    def sync_hook(meta, payload, ctx: ResumeContext) -> ToolCallResult:
        if raises is not None:
            raise raises
        assert result is not None
        return result

    async def async_hook(meta, payload, ctx: ResumeContext) -> ToolCallResult:
        return sync_hook(meta, payload, ctx)

    register_resume_hook(tool_name, async_hook if is_async else sync_hook)


async def _resume_events(tool_name: str):
    checkpoint, resumer, _raised = await _parked(tool_name)
    events = await _drain(resumer.resume_from_checkpoint(
        checkpoint, resumed_tcid=_TCID, toolcall_payload={"response": "x"},
    ))
    return events, resumer


@pytest.mark.asyncio
@pytest.mark.parametrize("is_async", [False, True], ids=["sync", "async"])
async def test_a_hook_that_raises_fails_the_node(is_async: bool):
    """Sync or async, a raising hook fails the resumed node with the exception text, as the dispatch path does."""
    name = f"test_vy_raises_{'async' if is_async else 'sync'}"
    _register(name, is_async=is_async, raises=ValueError("the hook blew up"))

    events, resumer = await _resume_events(name)

    (error,) = [e for e in events if isinstance(e, _GraphErrorEvent)]
    assert (error.code, error.message, error.node_id) == ("tool_execution_failed", "the hook blew up", "ask")
    assert resumer._node_states["ask"].status == NodeRuntimeStatus.FAILED
    assert resumer._context.nodes["ask"].error == "the hook blew up"
    assert "exit" not in resumer._context.nodes, "the graph ran past a failed node"


@pytest.mark.asyncio
@pytest.mark.parametrize("is_async", [False, True], ids=["sync", "async"])
async def test_a_hook_result_marked_is_error_fails_the_node(is_async: bool):
    """01a10b50: ``ToolResultPart.error`` fails a graph node, as it does in live dispatch: ``_map_toolcall_result`` judges
    the result of every ``tool_call`` path (this one included) and an error result is a failed call, with the hook's
    own text as the message. (It used to end the node with that text as its output and run the rest of the graph.)"""
    name = f"test_vy_is_error_{'async' if is_async else 'sync'}"
    _register(name, is_async=is_async, result=ToolCallResult(output="denied by the hook", is_error=True))

    events, resumer = await _resume_events(name)

    (error,) = [e for e in events if isinstance(e, _GraphErrorEvent)]
    assert (error.code, error.message, error.node_id) == ("tool_execution_failed", "denied by the hook", "ask")
    assert resumer._node_states["ask"].status == NodeRuntimeStatus.FAILED
    assert resumer._context.nodes["ask"].error == "denied by the hook"
    assert "exit" not in resumer._context.nodes, "the graph ran past a failed node"
