"""Part 2 Task 48: graph loops, edges and drift (AG-M7, AG-M8, AG-M9, AG-M10).

From the review probes (``/tmp/review-agent/graph/``), each turned into the behaviour the fix gives:

* AG-M7: a graph edited while parked ends ``failed``/``topology_drift``. A ready successor the edit deleted used to hang the superstep drain
  (its lookup sat outside the ``try`` that posts ``_NodeDone``); a deleted pending ToolCall node used to leave the resume as a raw ``KeyError``.
* AG-M8: an edge that cannot be evaluated for any reason (a json_path router from a fan-out target, a callable router that raises) ends the run
  ``failed`` like a ``ConfigError`` edge, in the main loop and after a resume; it used to escape and leave the run RUNNING.
* AG-M9: a loop-back that maps a shorter list fans in only this pass's results.
* AG-M10: an ``on_max_iterations`` node inside a cycle no longer disables the cap: the run ends ``max_iterations_exceeded`` once the iteration
  reaches ``max_iterations + len(graph.nodes)``.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from primer.graph.base import _GraphEndOutputEvent, _GraphErrorEvent
from primer.graph.executor import GraphExecutor
from primer.graph.router import RouterRegistry
from primer.model.chat import ToolResultPart
from primer.model.graph import (
    BranchCondition,
    FanOutSpec,
    Graph,
    GraphNodeMessage,
    GraphThread,
    JsonPathBranch,
    _BeginNode,
    _CallableRouter,
    _ConditionalEdge,
    _EndNode,
    _FanInNode,
    _FanOutNode,
    _JsonPathRouter,
    _StaticEdge,
    _ToolCallNode,
)
from primer.model.workspace_session import SessionStatus
from primer.model.yield_ import Yielded, YieldToWorker
from tests.graph.test_toolcall_dispatch import _InMemoryStorage

OBJ = {"type": "object"}


async def _agent_resolver(agent_id: str):
    raise KeyError(agent_id)


async def _llm_resolver(agent):
    raise NotImplementedError


def _make(graph, threads, msgs, tid, dispatcher, registry=None):
    return GraphExecutor(
        graph=graph, agent_resolver=_agent_resolver, llm_resolver=_llm_resolver,  # type: ignore[arg-type]
        thread_storage=threads, message_storage=msgs, graph_thread_id=tid,  # type: ignore[arg-type]
        tool_dispatcher=dispatcher, router_registry=registry,
    )


async def _run(graph, dispatcher, registry=None):
    threads = _InMemoryStorage(GraphThread)
    msgs = _InMemoryStorage(GraphNodeMessage)
    thread = await GraphExecutor.open_thread(graph=graph, thread_storage=threads)  # type: ignore[arg-type]
    ex = _make(graph, threads, msgs, thread.id, dispatcher, registry)
    events, exc = [], None
    try:
        async for e in ex.invoke([]):
            events.append(e)
    except BaseException as e:  # noqa: BLE001
        exc = e
    return ex, events, exc, await threads.get(thread.id)


async def _ok(node, arguments, bypass_approval=False):
    return ToolResultPart(id="x", output=node.id)


# ---- AG-M7: drift ----------------------------------------------------------------------------------------------------


def _drift_graph(with_c: bool) -> Graph:
    nodes = [
        _BeginNode(id="begin"),
        _ToolCallNode(id="a", tool_id="x__a", arguments={}),
        _ToolCallNode(id="b", tool_id="x__b", arguments={}),
        _EndNode(id="endb", output_template="b"),
    ]
    edges = [
        _StaticEdge(from_node="begin", to_node="a"),
        _StaticEdge(from_node="begin", to_node="b"),
        _StaticEdge(from_node="b", to_node="endb"),
    ]
    if with_c:
        nodes += [_ToolCallNode(id="c", tool_id="x__c", arguments={}), _EndNode(id="endc", output_template="c")]
        edges += [_StaticEdge(from_node="a", to_node="c"), _StaticEdge(from_node="c", to_node="endc")]
    else:
        nodes.append(_EndNode(id="enda", output_template="a"))
        edges.append(_StaticEdge(from_node="a", to_node="enda"))
    return Graph(id="g-drift", description="drift", nodes=nodes, edges=edges)


@pytest.mark.asyncio
async def test_a_deleted_ready_successor_ends_topology_drift_instead_of_hanging():
    g1 = _drift_graph(True)
    threads, msgs = _InMemoryStorage(GraphThread), _InMemoryStorage(GraphNodeMessage)
    thread = await GraphExecutor.open_thread(graph=g1, thread_storage=threads)  # type: ignore[arg-type]

    async def gate(node, arguments):
        raise YieldToWorker(Yielded(tool_name="_approval", event_key=f"tool_approval:sid:{node.id}"), tool_call_id=f"tc-{node.id}")

    with pytest.raises(YieldToWorker) as e1:
        async for _ in _make(g1, threads, msgs, thread.id, gate).invoke([]):
            pass
    # Resume a only: c is folded into the ready set, b still pending -> re-park.
    with pytest.raises(YieldToWorker) as e2:
        async for _ in _make(g1, threads, msgs, thread.id, _ok).resume_from_checkpoint(
            e1.value.graph_checkpoint, resumed_tcid="tc-a", resumed_event_key="tool_approval:sid:a",
        ):
            pass
    assert "c" in e2.value.graph_checkpoint["ready_set"]
    # The graph is edited while parked: node c is gone.
    ex3 = _make(_drift_graph(False), threads, msgs, thread.id, _ok)

    async def drain():
        return [ev async for ev in ex3.resume_from_checkpoint(
            e2.value.graph_checkpoint, resumed_tcid="tc-b", resumed_event_key="tool_approval:sid:b",
        )]

    events = await asyncio.wait_for(drain(), timeout=10)
    t = await threads.get(thread.id)
    assert (t.status, t.ended_reason, t.ended_detail) == (SessionStatus.ENDED, "failed", "topology_drift")
    assert [e.node_id for e in events if isinstance(e, _GraphErrorEvent) and e.code == "topology_drift"] == ["c"]


@pytest.mark.asyncio
async def test_a_deleted_pending_toolcall_node_ends_topology_drift_instead_of_raising():
    g1 = _drift_graph(True)
    threads, msgs = _InMemoryStorage(GraphThread), _InMemoryStorage(GraphNodeMessage)
    thread = await GraphExecutor.open_thread(graph=g1, thread_storage=threads)  # type: ignore[arg-type]

    async def first(node, arguments):
        if node.id == "a":
            raise YieldToWorker(Yielded(tool_name="_approval", event_key="tool_approval:sid:a"), tool_call_id="tc-a")
        return ToolResultPart(id="x", output=node.id)

    with pytest.raises(YieldToWorker) as e1:
        async for _ in _make(g1, threads, msgs, thread.id, first).invoke([]):
            pass
    g3 = Graph(id="g-drift", description="a removed", nodes=[_BeginNode(id="begin"), _EndNode(id="endb")],
               edges=[_StaticEdge(from_node="begin", to_node="endb")])
    events = [ev async for ev in _make(g3, threads, msgs, thread.id, _ok).resume_from_checkpoint(e1.value.graph_checkpoint)]

    t = await threads.get(thread.id)
    assert (t.status, t.ended_reason, t.ended_detail) == (SessionStatus.ENDED, "failed", "topology_drift")
    drift = [e for e in events if isinstance(e, _GraphErrorEvent) and e.code == "topology_drift"]
    assert [e.node_id for e in drift] == ["a"] and "no longer" in drift[0].message


# ---- AG-M8: edge evaluation errors -----------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_json_path_edge_from_a_fan_out_target_ends_failed_with_a_clear_reason():
    graph = Graph(
        id="g-cond-from-target", description="conditional from a fan-out target",
        nodes=[
            _BeginNode(id="begin"),
            _FanOutNode(id="fo", specs=[FanOutSpec(kind="broadcast", target_node_id="t", count=2)]),
            _ToolCallNode(id="t", tool_id="x__work", arguments={}, output_schema=OBJ),
            _EndNode(id="end", output_template="done"),
        ],
        edges=[
            _StaticEdge(from_node="begin", to_node="fo"),
            _ConditionalEdge(from_node="t", router=_JsonPathRouter(
                branches=[JsonPathBranch(conditions=[BranchCondition(path="ok", op="exists")], to_node="end")], default_to="end",
            )),
        ],
    )

    async def dispatcher(node, arguments):
        return ToolResultPart(id="w", output='{"ok": true}')

    ex, _, exc, thread = await _run(graph, dispatcher)

    assert exc is None
    assert (thread.status, thread.ended_reason, thread.ended_detail) == (SessionStatus.ENDED, "failed", None)
    assert "fan-out target" in (ex._last_failure_text or "")


def _raising_router_graph() -> Graph:
    return Graph(
        id="g-raising-router", description="t -> a callable router that raises",
        nodes=[_BeginNode(id="begin"), _ToolCallNode(id="t", tool_id="x__t", arguments={}), _EndNode(id="end", output_template="done")],
        edges=[_StaticEdge(from_node="begin", to_node="t"), _ConditionalEdge(from_node="t", router=_CallableRouter(callable_id="boom"))],
    )


def _raising_registry() -> RouterRegistry:
    def boom(ctx, src):
        raise RuntimeError("router exploded")

    reg = RouterRegistry()
    reg.register("boom", boom)
    return reg


@pytest.mark.asyncio
async def test_a_raising_callable_router_ends_the_run_failed():
    ex, _, exc, thread = await _run(_raising_router_graph(), _ok, _raising_registry())

    assert exc is None
    assert (thread.status, thread.ended_reason, thread.ended_detail) == (SessionStatus.ENDED, "failed", None)
    assert ex.last_done_reason == "graph_failed"
    assert "router exploded" in (ex._last_failure_text or "")


@pytest.mark.asyncio
async def test_a_raising_callable_router_after_a_resume_ends_the_run_failed():
    graph = _raising_router_graph()
    threads, msgs = _InMemoryStorage(GraphThread), _InMemoryStorage(GraphNodeMessage)
    thread = await GraphExecutor.open_thread(graph=graph, thread_storage=threads)  # type: ignore[arg-type]

    async def gate(node, arguments):
        raise YieldToWorker(Yielded(tool_name="_approval", event_key="tool_approval:sid:tc-1"), tool_call_id="tc-1")

    with pytest.raises(YieldToWorker) as parked:
        async for _ in _make(graph, threads, msgs, thread.id, gate, _raising_registry()).invoke([]):
            pass
    ex2 = _make(graph, threads, msgs, thread.id, _ok, _raising_registry())
    [ev async for ev in ex2.resume_from_checkpoint(parked.value.graph_checkpoint)]

    t = await threads.get(thread.id)
    assert (t.status, t.ended_reason, t.ended_detail) == (SessionStatus.ENDED, "failed", None)
    assert ex2.last_done_reason == "graph_failed"
    assert "router exploded" in (ex2._last_failure_text or "")


# ---- AG-M9: stale fan-out results after a loop-back ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_loop_back_fans_in_only_this_passes_results():
    graph = Graph(
        id="g-loop-map", description="loop with map fan-out", max_iterations=40,
        nodes=[
            _BeginNode(id="begin"),
            _ToolCallNode(id="plan", tool_id="x__plan", arguments={}, output_schema=OBJ),
            _FanOutNode(id="fo", specs=[FanOutSpec(kind="map", target_node_id="t", source_node_id="plan", source_path="items")]),
            _ToolCallNode(id="t", tool_id="x__work", arguments={"v": "{{ fanout_item }}"}),
            _FanInNode(id="fi", aggregate_template="{{ nodes.t | map(attribute='text') | join(',') }}"),
            _ToolCallNode(id="judge", tool_id="x__judge", arguments={"agg": "{{ nodes.fi.text }}"}, output_schema=OBJ),
            _EndNode(id="end", output_template="{{ nodes.fi.text }}"),
        ],
        edges=[
            _StaticEdge(from_node="begin", to_node="plan"),
            _StaticEdge(from_node="plan", to_node="fo"),
            _StaticEdge(from_node="t", to_node="fi"),
            _StaticEdge(from_node="fi", to_node="judge"),
            _ConditionalEdge(from_node="judge", router=_JsonPathRouter(
                branches=[JsonPathBranch(conditions=[BranchCondition(path="again", op="eq", value=True)], to_node="plan")],
                default_to="end",
            )),
        ],
    )
    calls = {"plan": 0, "judge": 0}
    judged: list[str] = []

    async def dispatcher(node, arguments):
        if node.tool_id == "x__plan":
            calls["plan"] += 1
            return ToolResultPart(id="p", output=json.dumps({"items": ["a", "b", "c"] if calls["plan"] == 1 else ["z"]}))
        if node.tool_id == "x__work":
            return ToolResultPart(id="w", output=str(arguments["v"]))
        calls["judge"] += 1
        judged.append(arguments["agg"])
        return ToolResultPart(id="j", output='{"again": true}' if calls["judge"] == 1 else '{"again": false}')

    ex, events, exc, thread = await _run(graph, dispatcher)

    assert exc is None
    assert judged == ["a,b,c", "z"]
    assert [e.text for e in events if isinstance(e, _GraphEndOutputEvent)] == ["z"]
    assert thread.ended_reason == "completed"
    assert ex._fanout_target_expected_count["t"] == 1
