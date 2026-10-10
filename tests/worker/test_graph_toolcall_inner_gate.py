"""A graph ToolCall node whose tool hits an INNER approval gate completes once the inner call is approved (ticket 01a1247f-b4de, found in the #712 round-2 re-review).

A ToolCall node dispatches its tool through the manager. The manager gates the node's OWN call; a tool that runs a gated tool through the manager (any tool that calls ``manager.execute``) or the ``call_tool``
meta-tool (whose handler gates the INNER tool against the inner tool's policy) raises a second gate from inside the dispatch, and that park's ``original_call`` is the INNER call. The graph resume re-dispatched
the NODE's own tool with ``bypass_approval=True`` whatever the park said, which skips only the manager's gate for the node's own call: the inner gate fired again under a new id, so every approval re-parked and the
node never completed (on main three approvals produced four distinct gates). The agent path re-dispatches ``resume_metadata.original_call`` (through the owning provider when the park carries ``via_call_tool``);
the graph resume now does the same for a park whose ``original_call`` is not the node's own call.

These cases drive the real ``WorkspaceGraphExecutor`` with a real ``ToolExecutionManager`` and policy-gated tools, the real ``resume_graph_from_checkpoint`` and the drain tap, after a live first run whose
records go through the real ``translate_stream_event``. The ``call_tool`` cases build the executor the way the worker does (``executor_builders``): no ``tool_manager``, the workspace session and the registry's
``get_toolset`` as the ``toolset_resolver``; the sibling case drives the real ``resume_graph_engine`` (round 2 of the #724 review: a decision runs only the gate it named, the card shows the call that runs).
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from primer.agent.tool_manager import ToolExecutionManager
from primer.graph.router import RouterRegistry
from primer.graph.workspace_executor import WorkspaceGraphExecutor
from primer.model.chat import Tool, ToolCallPart, ToolCallResult
from primer.model.common import dump_for_storage
from primer.model.graph import FanOutSpec, Graph, _BeginNode, _EndNode, _FanInNode, _FanOutNode, _StaticEdge, _ToolCallNode
from primer.model.principal import PrincipalRef
from primer.model.tool_approval import ApproverSpec, RequiredApprovalConfig, ToolApprovalPolicy
from primer.model.yield_ import YieldToWorker, gate_id_of, with_wake_gate
from primer.session.persistence import _CoalesceState, stash_graph_scoped_ids, translate_stream_event
from primer.toolset.system import SYSTEM_TOOLSET_ID
from primer.worker import graph_resume_coordinator
from primer.worker.graph_resume import resume_graph_from_checkpoint
from primer.worker.yield_runtime import ParkedState
from tests._resume_hook_fakes import (
    DrainTapPool,
    EngineFakePool,
    EngineStorageProvider,
    FakeSessionRow,
    FakeSessionStorage,
    FakeStorage,
    NullWorkspaceIO,
    RecordingWorkspaceIO,
    waiting_graph_session,
)
from tests.agent.test_tool_manager_approval_gate import _PoliciesOnlyResolver
from tests.graph.test_workspace_executor import _make_state_repo

# Re-export so pytest resolves the real system toolset fixtures the neighbouring call_tool tests use.
from tests.toolset.test_system import _llm, pr, sp, system_toolset  # noqa: F401


def _policy(toolset_id: str, tool_name: str, approvers: ApproverSpec | None = None) -> ToolApprovalPolicy:
    return ToolApprovalPolicy(id=f"p-{tool_name}", toolset_id=toolset_id, tool_name=tool_name, enabled=True, approval=RequiredApprovalConfig(), approvers=approvers)


# Arguments with the shapes a checkpoint round-trips (nested, a float, a null, a bool, non-ASCII, an empty object): the park of the node's OWN call is told from an inner call by comparing them
ARGS = {"a": 1, "nested": {"list": [1, 2.5, None, True, "\u00e9"], "empty": {}}}


def _workspace_session() -> Any:
    return SimpleNamespace(session_id="s", workspace_id="ws-1", agent_id=None, workspace_tools=[])


def _graph(tool_id: str, arguments: dict) -> Graph:
    return Graph.model_construct(
        id="g-inner", description="begin -> tool -> end", max_iterations=5, harness_id=None,
        nodes=[_BeginNode(id="begin"), _ToolCallNode(id="tool", tool_id=tool_id, arguments=arguments), _EndNode(id="end", output_template="{{ nodes.tool.text }}")],
        edges=[_StaticEdge(from_node="begin", to_node="tool"), _StaticEdge(from_node="tool", to_node="end")],
    )


async def _executor(tmp_path: Path, manager: Any, graph: Graph, *, toolset_resolver: Any = None) -> WorkspaceGraphExecutor:
    """With a ``manager`` the executor is handed it. Without one it is built the way the worker builds it (``executor_builders``): no ``tool_manager``, the workspace session and the registry's
    ``get_toolset`` as ``toolset_resolver``, and it makes its own manager."""
    async def agent_resolver(agent_id: str) -> Any:
        raise KeyError(agent_id)

    async def llm_resolver(agent: Any, *a: Any, **k: Any) -> Any:
        raise NotImplementedError

    return WorkspaceGraphExecutor(
        graph=graph, agent_resolver=agent_resolver, llm_resolver=llm_resolver,  # type: ignore[arg-type]
        state_repo=await _make_state_repo(tmp_path), graph_session_id="gsid", tool_manager=manager,  # type: ignore[arg-type]
        router_registry=RouterRegistry(),
        workspace_session=None if manager is not None else _workspace_session(), toolset_resolver=toolset_resolver,  # type: ignore[arg-type]
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


async def _create_the_provider(system_toolset: Any) -> None:
    """Create ``anthropic-1`` with its real secret (a create body that carries the served mask is refused), and say so when it is not created: the call_tool cases read it through the approved inner call."""
    result = await system_toolset.call(tool_name="create_llm_provider", arguments={"entity": dump_for_storage(_llm())})
    assert not result.is_error, result.output


def _resolver(provider: Any) -> Any:
    """``pool._provider_registry.get_toolset`` as ``executor_builders`` wires it: the system toolset for its id, nothing for another."""
    async def resolver(toolset_id: str) -> Any:
        return provider if toolset_id == SYSTEM_TOOLSET_ID else None

    return resolver


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
async def test_a_call_tool_node_completes_once_its_inner_tool_is_approved(system_toolset, sp, tmp_path: Path) -> None:  # noqa: F811
    """The common case: ``system__call_tool`` runs the tool it is asked to and gates THAT tool against its own policy, parking with ``via_call_tool``; the node's own tool (call_tool) has no policy."""
    await sp.get_storage(ToolApprovalPolicy).create(_policy(SYSTEM_TOOLSET_ID, "get_llm_provider"))
    await _create_the_provider(system_toolset)
    resolver = _resolver(system_toolset)      # the production shape: no injected manager (the worker builds none) and the registry's get_toolset as the toolset resolver
    graph = _graph(f"{SYSTEM_TOOLSET_ID}__call_tool", {"toolset_id": SYSTEM_TOOLSET_ID, "tool_name": "get_llm_provider", "arguments": {"id": "anthropic-1"}})

    checkpoint, live, seq0 = await _park(await _executor(tmp_path, None, graph, toolset_resolver=resolver))
    (parked,) = checkpoint["pending_toolcalls"]
    meta = parked["resume_metadata"]
    assert meta["original_call"]["name"] == "get_llm_provider" and meta["via_call_tool"]["toolset_id"] == SYSTEM_TOOLSET_ID, "the park is the inner tool's, not the node's"

    written, repark, _ = await _resume(checkpoint, await _executor(tmp_path, None, graph, toolset_resolver=resolver), parked, seq0)
    assert repark is None, "the approved inner tool was gated again by call_tool's handler: the node never completes"
    call = _call_row(live)
    (result,) = _results(written)
    assert (result["payload"]["call_id"], result["payload"]["error"]) == (call["payload"]["id"], False), f"the approved inner call answered: {result['payload']['output']}"
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
async def test_a_rejected_call_tool_inner_gate_fails_the_node_and_does_not_read_the_provider(system_toolset, sp, tmp_path: Path) -> None:  # noqa: F811
    await sp.get_storage(ToolApprovalPolicy).create(_policy(SYSTEM_TOOLSET_ID, "get_llm_provider"))
    await _create_the_provider(system_toolset)
    resolver = _resolver(system_toolset)      # the production shape: no injected manager (the worker builds none) and the registry's get_toolset as the toolset resolver
    graph = _graph(f"{SYSTEM_TOOLSET_ID}__call_tool", {"toolset_id": SYSTEM_TOOLSET_ID, "tool_name": "get_llm_provider", "arguments": {"id": "anthropic-1"}})

    checkpoint, live, seq0 = await _park(await _executor(tmp_path, None, graph, toolset_resolver=resolver))
    (parked,) = checkpoint["pending_toolcalls"]
    written, repark, _ = await _resume(checkpoint, await _executor(tmp_path, None, graph, toolset_resolver=resolver), parked, seq0, payload={"decision": "rejected", "reason": "no"})
    assert repark is None
    call = _call_row(live)
    (result,) = _results(written)
    assert (result["payload"]["call_id"], result["payload"]["error"]) == (call["payload"]["id"], True)
    assert "anthropic-1" not in str(result["payload"]["output"]), "the rejected inner call was run and its answer written"
    assert [e["payload"].get("code") for e in _errors(written)] == ["tool_approval_rejected"], _errors(written)


# ---- round 2 of the #724 review (security): siblings that share an event key, what the operator is shown, the production shape ------------------------------------------------------------------


async def _park_yield(executor: WorkspaceGraphExecutor) -> tuple[dict, Any, YieldToWorker]:
    """Run to the first park: the checkpoint, the stash ``dispatch.py`` keeps and the ``YieldToWorker`` the worker parks on (its ``yielded`` is the projection the Inbox and the channel read)."""
    state = _CoalesceState()
    executor.bind_coalesce_state(state)
    with pytest.raises(YieldToWorker) as parked:
        async for event in executor.invoke([]):
            translate_stream_event(event, state, turn_no=1)
    checkpoint = executor.snapshot_state()
    return checkpoint, stash_graph_scoped_ids(checkpoint, state), parked.value


def _fanout_graph(tool_id: str) -> Graph:
    return Graph.model_construct(
        id="g-fan", description="a fan-out of two ToolCall nodes", max_iterations=10, harness_id=None,
        nodes=[
            _BeginNode(id="begin"),
            _FanOutNode(id="fan", specs=[FanOutSpec(kind="broadcast", target_node_id="tool", count=2)]),
            _ToolCallNode(id="tool", tool_id=tool_id, arguments={"tag": "{{ fanout_index }}"}),
            _FanInNode(id="agg", aggregate_template="{% for n in nodes.tool %}{{ n.text }};{% endfor %}"),
            _EndNode(id="end", output_template="{{ nodes.agg.text }}"),
        ],
        edges=[_StaticEdge(from_node="begin", to_node="fan"), _StaticEdge(from_node="tool", to_node="agg"), _StaticEdge(from_node="agg", to_node="end")],
    )


class _SubAgentLike:
    """Toolset ``_z``: ``outer`` runs ONE inner call through a SEPARATE manager (as ``run_subagent`` does) under a provider-style id, ``call_0`` (Ollama and Gemini number the calls of a stream themselves),
    so the gates of two siblings share one event key; the sibling tagged 0 asks for ``safe``, the one tagged 1 for ``danger``."""

    def __init__(self) -> None:
        self.sub: Any = None
        self.runs: list[str] = []

    async def list_tools(self, *, principal: str | None = None) -> AsyncIterator[Tool]:
        for name in ("outer", "safe", "danger"):
            yield Tool(id=name, description=name, toolset_id="_z", args_schema={"type": "object", "properties": {}, "additionalProperties": True})

    def is_yielding(self, tool_name: str) -> bool:
        return False

    def required_role(self, tool_name: str) -> str:
        return "admin"

    async def call(self, *, tool_name: str, arguments: Any, principal: Any = None, ctx: Any = None) -> ToolCallResult:
        if tool_name == "outer":
            tag = (arguments or {}).get("tag")
            result = await self.sub.execute(ToolCallPart(id="call_0", name="_z__safe" if tag == "0" else "_z__danger", arguments={"tag": tag}), principal=principal)
            return ToolCallResult(output=f"outer({result.output})", is_error=result.error)
        self.runs.append(tool_name)
        return ToolCallResult(output=f"{tool_name} ran", is_error=False)


class _ToolCallPool(EngineFakePool):
    async def _graph_agent_tool_result(self, checkpoint: dict, tcid: str, payload: Any, *, session_id: str, event_key: str | None = None) -> None:
        return None            # an approval gate has no agent-node answer, as on the real pool


@pytest.mark.asyncio
@pytest.mark.parametrize("decided", [0, 1], ids=["decide-the-safe-gate", "decide-the-dangerous-gate"])
async def test_one_approval_runs_only_the_inner_call_of_the_gate_it_decided_when_siblings_share_a_key(tmp_path: Path, decided: int) -> None:
    """B1 of the review (security): two fan-out siblings whose INNER gates share one event key. A decision selects every pending entry on its key, and each entry now runs its own ``original_call`` with
    ``bypass_approval``: one approval, judged against ONE sibling's approvers, ran both. The wake names the gate it decided (``__yield_gate_id__``); only that gate's entry may run, the other stays parked."""
    provider = _SubAgentLike()
    policies = [_policy("_z", "safe"), _policy("_z", "danger", approvers=ApproverSpec(kind="users", users=["root"]))]

    def manager() -> ToolExecutionManager:
        built = ToolExecutionManager(toolset_providers={"_z": provider}, workspace_session=_workspace_session(), initiated_by=PrincipalRef.system())  # type: ignore[dict-item,arg-type]
        built._approval_resolver = _PoliciesOnlyResolver(policies)
        return built

    provider.sub = manager()
    node_manager = manager()
    graph = _fanout_graph("_z__outer")
    checkpoint, _seq, yld = await _park_yield(await _executor(tmp_path, node_manager, graph))
    entries = {e["node_id"]: e for e in checkpoint["pending_toolcalls"]}
    mine, other = entries[f"tool[{decided}]"], entries[f"tool[{1 - decided}]"]
    assert mine["parked_event_key"] == other["parked_event_key"], "the siblings share one event key"
    assert gate_id_of(mine["resume_metadata"]) != gate_id_of(other["resume_metadata"])
    payload = with_wake_gate({"decision": "approved", "decided_by": "mallory"}, gate_id_of(mine["resume_metadata"]))

    async def factory() -> WorkspaceGraphExecutor:
        return await _executor(tmp_path, node_manager, graph)

    pool = _ToolCallPool(storage=EngineStorageProvider(), workspace_io=NullWorkspaceIO(), executor_factory=factory)
    session = waiting_graph_session()
    session.parked_state = {"resume_event_payloads": {"k": {"event_key": mine["parked_event_key"], "payload": payload}}}
    parked = ParkedState(
        yielded=yld.yielded, llm_messages=[], turn_no=0, started_at=datetime.now(UTC), tool_call_id=yld.tool_call_id, resume_event_payload=payload, graph_checkpoint=checkpoint,
    )

    outcome = await graph_resume_coordinator.resume_graph_engine(pool, session, parked)

    assert provider.runs == ["safe" if decided == 0 else "danger"], f"one approval ran {provider.runs}: a sibling's unapproved call ran with it"
    assert outcome == "REPARKED" and len(pool.repark_calls) == 1
    assert [e["node_id"] for e in pool.repark_calls[0].graph_checkpoint["pending_toolcalls"]] == [other["node_id"]], "the sibling the decision did not name stays parked"


@pytest.mark.asyncio
async def test_what_the_operator_is_shown_is_the_call_that_runs(tmp_path: Path) -> None:
    """B2 of the review: the park's projection (the top-level ``yielded`` the Inbox row reads, and the ``pending_dispatch`` entry the channel prompt is built from) baked the NODE's tool and arguments,
    while an approval of an inner gate runs the INNER call: 'Approve _y__outer(...)?' for a run of _y__inner(...). Both project the call the gate gates, with who may decide it and why it gated."""
    provider = _GateInGateProvider()
    manager = ToolExecutionManager(toolset_providers={"_y": provider}, workspace_session=_workspace_session(), initiated_by=PrincipalRef.system())  # type: ignore[dict-item,arg-type]
    alice = ApproverSpec(kind="users", users=["alice"])
    manager._approval_resolver = _PoliciesOnlyResolver([_policy("_y", "outer"), _policy("_y", "inner", approvers=alice)])
    provider.manager = manager
    graph = _graph("_y__outer", ARGS)

    checkpoint, seq0, yld = await _park_yield(await _executor(tmp_path, manager, graph))
    own = yld.yielded.resume_metadata
    assert (own["original_call"]["name"], own["original_call"]["arguments"]) == ("_y__outer", ARGS), "the node's own gate shows the node's own call"
    (first,) = checkpoint["pending_toolcalls"]
    _written, repark, _seq = await _resume(checkpoint, await _executor(tmp_path, manager, graph), first, seq0)
    assert repark is not None

    shown = [repark.yielded.resume_metadata, repark.graph_checkpoint["pending_dispatch"][0]["resume_metadata"]]
    for where, meta in zip(("projection", "dispatch entry"), shown):
        assert meta["original_call"]["name"] == "_y__inner", f"the {where} shows the node's call and not the call that approving it runs: {meta['original_call']}"
        assert meta["approvers"] == alice.model_dump(), f"the {where} reads 'anyone' for a gate that admits only alice"
        assert "gate_reason" in meta and meta["policy_id"] == "p-inner" and meta["approval_type"], f"the {where} drops the stamps of the gate: {sorted(meta)}"


@pytest.mark.asyncio
async def test_what_the_operator_is_shown_for_a_call_tool_gate_is_the_inner_tool_and_how_it_is_dispatched(system_toolset, sp, tmp_path: Path) -> None:  # noqa: F811
    await sp.get_storage(ToolApprovalPolicy).create(_policy(SYSTEM_TOOLSET_ID, "get_llm_provider"))
    await _create_the_provider(system_toolset)
    graph = _graph(f"{SYSTEM_TOOLSET_ID}__call_tool", {"toolset_id": SYSTEM_TOOLSET_ID, "tool_name": "get_llm_provider", "arguments": {"id": "anthropic-1"}})

    checkpoint, _seq, yld = await _park_yield(await _executor(tmp_path, None, graph, toolset_resolver=_resolver(system_toolset)))
    for where, meta in (("projection", yld.yielded.resume_metadata), ("dispatch entry", checkpoint["pending_dispatch"][0]["resume_metadata"])):
        assert meta["original_call"]["name"] == "get_llm_provider" and meta["original_call"]["arguments"] == {"id": "anthropic-1"}, (where, meta["original_call"])
        assert meta["via_call_tool"]["toolset_id"] == SYSTEM_TOOLSET_ID, where
