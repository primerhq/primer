"""A graph ToolCall node whose tool hits an INNER approval gate completes once the inner call is approved (ticket 01a1247f-b4de, found in the #712 round-2 re-review).

A ToolCall node dispatches its tool through the manager. The manager gates the node's OWN call; a tool that runs a gated tool through the manager (any tool that calls ``manager.execute``) or the ``call_tool``
meta-tool (whose handler gates the INNER tool against the inner tool's policy) raises a second gate from inside the dispatch, and that park's ``original_call`` is the INNER call. The graph resume re-dispatched
the NODE's own tool with ``bypass_approval=True`` whatever the park said, which skips only the manager's gate for the node's own call: the inner gate fired again under a new id, so every approval re-parked and the
node never completed (on main three approvals produced four distinct gates). The agent path re-dispatches ``resume_metadata.original_call`` (through the owning provider when the park carries ``via_call_tool``);
the graph resume now does the same for a park whose ``original_call`` is not the node's own call.

These cases drive the real ``WorkspaceGraphExecutor`` with a real ``ToolExecutionManager`` and policy-gated tools, the real ``resume_graph_from_checkpoint`` and the drain tap, after a live first run whose
records go through the real ``translate_stream_event``.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from primer.agent.tool_manager import ToolExecutionManager
from primer.graph.router import RouterRegistry
from primer.graph.workspace_executor import WorkspaceGraphExecutor
from primer.model.chat import Tool, ToolCallPart, ToolCallResult
from primer.model.graph import Graph, _BeginNode, _EndNode, _StaticEdge, _ToolCallNode
from primer.model.principal import PrincipalRef
from primer.model.tool_approval import RequiredApprovalConfig, ToolApprovalPolicy
from primer.model.yield_ import YieldToWorker
from primer.session.persistence import _CoalesceState, stash_graph_scoped_ids, translate_stream_event
from primer.toolset.system import SYSTEM_TOOLSET_ID
from primer.worker.graph_resume import resume_graph_from_checkpoint
from tests._resume_hook_fakes import DrainTapPool, FakeSessionRow, FakeSessionStorage, FakeStorage, RecordingWorkspaceIO
from tests.agent.test_tool_manager_approval_gate import _PoliciesOnlyResolver
from tests.graph.test_workspace_executor import _make_state_repo

# Re-export so pytest resolves the real system toolset fixtures the neighbouring call_tool tests use.
from tests.toolset.test_system import _llm, pr, sp, system_toolset  # noqa: F401


def _policy(toolset_id: str, tool_name: str) -> ToolApprovalPolicy:
    return ToolApprovalPolicy(id=f"p-{tool_name}", toolset_id=toolset_id, tool_name=tool_name, enabled=True, approval=RequiredApprovalConfig())


# Arguments with the shapes a checkpoint round-trips (nested, a float, a null, a bool, non-ASCII, an empty object): the park of the node's OWN call is told from an inner call by comparing them
ARGS = {"a": 1, "nested": {"list": [1, 2.5, None, True, "\u00e9"], "empty": {}}}


def _workspace_session() -> Any:
    return SimpleNamespace(session_id="s", workspace_id="ws-1", agent_id=None)


def _graph(tool_id: str, arguments: dict) -> Graph:
    return Graph.model_construct(
        id="g-inner", description="begin -> tool -> end", max_iterations=5, harness_id=None,
        nodes=[_BeginNode(id="begin"), _ToolCallNode(id="tool", tool_id=tool_id, arguments=arguments), _EndNode(id="end", output_template="{{ nodes.tool.text }}")],
        edges=[_StaticEdge(from_node="begin", to_node="tool"), _StaticEdge(from_node="tool", to_node="end")],
    )


async def _executor(tmp_path: Path, manager: Any, graph: Graph) -> WorkspaceGraphExecutor:
    async def agent_resolver(agent_id: str) -> Any:
        raise KeyError(agent_id)

    async def llm_resolver(agent: Any, *a: Any, **k: Any) -> Any:
        raise NotImplementedError

    return WorkspaceGraphExecutor(
        graph=graph, agent_resolver=agent_resolver, llm_resolver=llm_resolver,  # type: ignore[arg-type]
        state_repo=await _make_state_repo(tmp_path), graph_session_id="gsid", tool_manager=manager,  # type: ignore[arg-type]
        router_registry=RouterRegistry(),
    )


async def _park(executor: WorkspaceGraphExecutor) -> tuple[dict, list[dict], Any]:
    """Run the node to its first park: the checkpoint, the live records (through ``translate_stream_event``) and the stash ``dispatch.py`` keeps."""
    state = _CoalesceState()
    executor.bind_coalesce_state(state)
    live: list[dict] = []
    with pytest.raises(YieldToWorker):
        async for event in executor.invoke([]):
            record = translate_stream_event(event, state, turn_no=1)
            live.extend(r.model_dump(mode="json") for r in ([] if record is None else record if isinstance(record, list) else [record]))
    checkpoint = executor.snapshot_state()
    return checkpoint, live, stash_graph_scoped_ids(checkpoint, state)


async def _resume(checkpoint: dict, executor: WorkspaceGraphExecutor, entry: dict, seq: Any, *, payload: Any = None) -> tuple[list[dict], Any, Any]:
    """Resume the park ``entry`` names: the records the drain wrote, the re-park (``None`` when the graph drained) and the stash for the next resume."""
    io = RecordingWorkspaceIO()
    row = FakeSessionRow(sid="gs-park", workspace_id="ws-1", turn_no=1, last_seq=10)
    pool = DrainTapPool(workspace_io=io, storage=FakeStorage(FakeSessionStorage(row)))
    _decision, repark, next_seq = await resume_graph_from_checkpoint(
        executor=executor, checkpoint=checkpoint, payload=payload or {"decision": "approved"}, resumed_tcid=entry["tool_call_id"], pool=pool, session=row,  # type: ignore[arg-type]
        resumed_event_key=entry["parked_event_key"], node_tool_call_seq=seq,
    )
    written = [json.loads(one) for _sid, line in io.lines for one in line.decode().splitlines() if one.strip()]
    return written, repark, next_seq


def _results(written: list[dict]) -> list[dict]:
    return [r for r in written if r.get("kind") == "tool_result"]


def _call_row(live: list[dict]) -> dict:
    (call,) = [r for r in live if r["kind"] == "tool_call"]
    return call


class _GateInGateProvider:
    """Toolset ``_y``: ``outer`` runs ``inner`` through the SAME manager under the id it was called with (what any tool that runs a gated tool through the manager does); both are gated by policy."""

    def __init__(self) -> None:
        self.manager: Any = None
        self.outer_runs: list[str] = []
        self.inner_runs = 0

    async def list_tools(self, *, principal: str | None = None) -> AsyncIterator[Tool]:
        for name in ("outer", "inner"):
            yield Tool(id=name, description=name, toolset_id="_y", args_schema={"type": "object", "properties": {}, "additionalProperties": True})

    def is_yielding(self, tool_name: str) -> bool:
        return False

    def required_role(self, tool_name: str) -> str:
        return "admin"

    async def call(self, *, tool_name: str, arguments: Any, principal: Any = None, ctx: Any = None) -> ToolCallResult:
        if tool_name == "outer":
            self.outer_runs.append(ctx.tool_call_id)
            result = await self.manager.execute(ToolCallPart(id=ctx.tool_call_id, name="_y__inner", arguments={}), principal=principal)
            return ToolCallResult(output=f"outer({result.output})", is_error=result.error)
        self.inner_runs += 1
        return ToolCallResult(output="inner ran", is_error=False)


@pytest.mark.asyncio
async def test_a_tool_that_runs_a_gated_tool_through_the_manager_completes_once_the_inner_call_is_approved(tmp_path: Path) -> None:
    provider = _GateInGateProvider()
    manager = ToolExecutionManager(toolset_providers={"_y": provider}, workspace_session=_workspace_session(), initiated_by=PrincipalRef.system())  # type: ignore[dict-item,arg-type]
    manager._approval_resolver = _PoliciesOnlyResolver([_policy("_y", "outer"), _policy("_y", "inner")])
    provider.manager = manager
    graph = _graph("_y__outer", ARGS)

    checkpoint, live, seq0 = await _park(await _executor(tmp_path, manager, graph))
    (first,) = checkpoint["pending_toolcalls"]
    assert first["resume_metadata"]["original_call"]["name"] == "_y__outer", "the first gate is the node's own"

    # approval 1: the node's own call is approved, runs, and the tool it runs hits the INNER gate
    _written1, repark, seq1 = await _resume(checkpoint, await _executor(tmp_path, manager, graph), first, seq0)
    assert repark is not None and provider.inner_runs == 0, "the approved outer tool must park on the inner gate"
    (second,) = repark.graph_checkpoint["pending_toolcalls"]
    assert second["resume_metadata"]["original_call"]["name"] == "_y__inner" and second["parked_event_key"] != first["parked_event_key"]

    # approval 2: the inner call is approved. The node completes with the inner tool's answer, under the row it wrote
    written2, repark2, _ = await _resume(repark.graph_checkpoint, await _executor(tmp_path, manager, graph), second, seq1)
    assert repark2 is None, "the approved inner call was run again behind a new gate: the node never completes"
    assert provider.inner_runs == 1 and len(provider.outer_runs) == 1, "the approved inner call runs once and the outer tool is not run a second time"
    call = _call_row(live)
    assert [(r["payload"]["call_id"], r["payload"]["output"], r["payload"]["error"]) for r in _results(written2)] == [(call["payload"]["id"], "inner ran", False)]


@pytest.mark.asyncio
async def test_a_call_tool_node_completes_once_its_inner_tool_is_approved(system_toolset, sp, pr, tmp_path: Path) -> None:  # noqa: F811
    """The common case: ``system__call_tool`` runs the tool it is asked to and gates THAT tool against its own policy, parking with ``via_call_tool``; the node's own tool (call_tool) has no policy."""
    await sp.get_storage(ToolApprovalPolicy).create(_policy(SYSTEM_TOOLSET_ID, "get_llm_provider"))
    await system_toolset.call(tool_name="create_llm_provider", arguments={"entity": _llm().model_dump(mode="json")})
    manager = ToolExecutionManager(  # the production manager holds the provider registry, which an approved call_tool inner call is dispatched through
        toolset_providers={SYSTEM_TOOLSET_ID: system_toolset}, workspace_session=_workspace_session(), initiated_by=PrincipalRef.system(), provider_registry=pr,  # type: ignore[dict-item,arg-type]
    )
    graph = _graph(f"{SYSTEM_TOOLSET_ID}__call_tool", {"toolset_id": SYSTEM_TOOLSET_ID, "tool_name": "get_llm_provider", "arguments": {"id": "anthropic-1"}})

    checkpoint, live, seq0 = await _park(await _executor(tmp_path, manager, graph))
    (parked,) = checkpoint["pending_toolcalls"]
    meta = parked["resume_metadata"]
    assert meta["original_call"]["name"] == "get_llm_provider" and meta["via_call_tool"]["toolset_id"] == SYSTEM_TOOLSET_ID, "the park is the inner tool's, not the node's"

    written, repark, _ = await _resume(checkpoint, await _executor(tmp_path, manager, graph), parked, seq0)
    assert repark is None, "the approved inner tool was gated again by call_tool's handler: the node never completes"
    call = _call_row(live)
    (result,) = _results(written)
    assert (result["payload"]["call_id"], result["payload"]["error"]) == (call["payload"]["id"], False)
    assert "anthropic-1" in str(result["payload"]["output"])


def _errors(written: list[dict]) -> list[dict]:
    return [r for r in written if r.get("kind") == "error"]


@pytest.mark.asyncio
async def test_a_rejected_inner_gate_fails_the_node_and_never_runs_the_inner_call(tmp_path: Path) -> None:
    """The inner call goes through the hook a rejection replaces: the operator who rejects the inner gate must not get the inner tool run."""
    provider = _GateInGateProvider()
    manager = ToolExecutionManager(toolset_providers={"_y": provider}, workspace_session=_workspace_session(), initiated_by=PrincipalRef.system())  # type: ignore[dict-item,arg-type]
    manager._approval_resolver = _PoliciesOnlyResolver([_policy("_y", "outer"), _policy("_y", "inner")])
    provider.manager = manager
    graph = _graph("_y__outer", ARGS)

    checkpoint, live, seq0 = await _park(await _executor(tmp_path, manager, graph))
    (first,) = checkpoint["pending_toolcalls"]
    _written1, repark, seq1 = await _resume(checkpoint, await _executor(tmp_path, manager, graph), first, seq0)
    assert repark is not None
    (second,) = repark.graph_checkpoint["pending_toolcalls"]

    written, again, _ = await _resume(repark.graph_checkpoint, await _executor(tmp_path, manager, graph), second, seq1, payload={"decision": "rejected", "reason": "no"})
    assert again is None
    assert provider.inner_runs == 0, "a rejected inner gate ran the inner call"
    call = _call_row(live)
    assert [(r["payload"]["call_id"], r["payload"]["error"]) for r in _results(written)] == [(call["payload"]["id"], True)]
    assert [e["payload"].get("code") for e in _errors(written)] == ["tool_approval_rejected"], _errors(written)


@pytest.mark.asyncio
async def test_a_rejected_call_tool_inner_gate_fails_the_node_and_does_not_read_the_provider(system_toolset, sp, pr, tmp_path: Path) -> None:  # noqa: F811
    await sp.get_storage(ToolApprovalPolicy).create(_policy(SYSTEM_TOOLSET_ID, "get_llm_provider"))
    await system_toolset.call(tool_name="create_llm_provider", arguments={"entity": _llm().model_dump(mode="json")})
    manager = ToolExecutionManager(
        toolset_providers={SYSTEM_TOOLSET_ID: system_toolset}, workspace_session=_workspace_session(), initiated_by=PrincipalRef.system(), provider_registry=pr,  # type: ignore[dict-item,arg-type]
    )
    graph = _graph(f"{SYSTEM_TOOLSET_ID}__call_tool", {"toolset_id": SYSTEM_TOOLSET_ID, "tool_name": "get_llm_provider", "arguments": {"id": "anthropic-1"}})

    checkpoint, live, seq0 = await _park(await _executor(tmp_path, manager, graph))
    (parked,) = checkpoint["pending_toolcalls"]
    written, repark, _ = await _resume(checkpoint, await _executor(tmp_path, manager, graph), parked, seq0, payload={"decision": "rejected", "reason": "no"})
    assert repark is None
    call = _call_row(live)
    (result,) = _results(written)
    assert (result["payload"]["call_id"], result["payload"]["error"]) == (call["payload"]["id"], True)
    assert "anthropic-1" not in str(result["payload"]["output"]), "the rejected inner call was run and its answer written"
    assert [e["payload"].get("code") for e in _errors(written)] == ["tool_approval_rejected"], _errors(written)
