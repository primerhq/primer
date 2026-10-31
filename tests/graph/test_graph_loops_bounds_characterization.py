"""Characterization for Part 2 Tasks 48 and 49 (graph loops, edges, drift, bounds): what a graph run does today, pinned before the fixes.

Each test passes on the code before the fixes and must pass unchanged after them: an edge that cannot be evaluated keeps its reason, an
acyclic ``on_max_iterations`` tail still finishes, a first-pass fan-in and a fan-out under the cap keep their outputs and order, and the
turn-log records of nodes the main loop runs keep their shape.
"""

from __future__ import annotations

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
from primer.model.turn_log import TurnLogRecord
from primer.model.workspace_session import SessionStatus
from tests.graph.test_toolcall_dispatch import _InMemoryStorage

OBJ = {"type": "object"}


async def _agent_resolver(agent_id: str):
    raise KeyError(agent_id)


async def _llm_resolver(agent):
    raise NotImplementedError


async def _run(graph: Graph, dispatcher, *, registry: RouterRegistry | None = None, turn_logs=None, max_parallel_nodes: int = 16):
    threads = _InMemoryStorage(GraphThread)
    msgs = _InMemoryStorage(GraphNodeMessage)
    thread = await GraphExecutor.open_thread(graph=graph, thread_storage=threads)  # type: ignore[arg-type]
    ex = GraphExecutor(
        graph=graph, agent_resolver=_agent_resolver, llm_resolver=_llm_resolver,  # type: ignore[arg-type]
        thread_storage=threads, message_storage=msgs, graph_thread_id=thread.id,  # type: ignore[arg-type]
        tool_dispatcher=dispatcher, router_registry=registry, turn_log_storage=turn_logs,  # type: ignore[arg-type]
        max_parallel_nodes=max_parallel_nodes,
    )
    events = [e async for e in ex.invoke([])]
    return ex, events, await threads.get(thread.id)


def _ends(events) -> list[str]:
    return [e.text for e in events if isinstance(e, _GraphEndOutputEvent)]


async def _echo(node, arguments):
    return ToolResultPart(id="x", output=node.id)


def _router_graph(router: _CallableRouter) -> Graph:
    return Graph(
        id="g-router", description="begin -> t -> callable router",
        nodes=[_BeginNode(id="begin"), _ToolCallNode(id="t", tool_id="x__t", arguments={}), _EndNode(id="end", output_template="done")],
        edges=[_StaticEdge(from_node="begin", to_node="t"), _ConditionalEdge(from_node="t", router=router)],
    )


@pytest.mark.asyncio
async def test_an_unregistered_router_in_the_main_loop_ends_failed_with_no_detail():
    ex, events, thread = await _run(_router_graph(_CallableRouter(callable_id="not-registered")), _echo)

    assert (thread.status, thread.ended_reason, thread.ended_detail) == (SessionStatus.ENDED, "failed", None)
    assert ex.last_done_reason == "graph_failed"
    assert "not-registered" in (ex._last_failure_text or "")
    assert not [e for e in events if isinstance(e, _GraphErrorEvent)], "a ConfigError edge failure emits no error event"


@pytest.mark.asyncio
async def test_a_router_naming_an_unknown_node_ends_failed_with_no_detail():
    reg = RouterRegistry()
    reg.register("r", lambda ctx, src: "nowhere")
    ex, _, thread = await _run(_router_graph(_CallableRouter(callable_id="r")), _echo, registry=reg)

    assert (thread.status, thread.ended_reason, thread.ended_detail) == (SessionStatus.ENDED, "failed", None)
    assert "nowhere" in (ex._last_failure_text or "")


@pytest.mark.asyncio
async def test_an_acyclic_on_max_iterations_tail_still_finishes():
    graph = Graph(
        id="g-cap-tail", description="a loops on itself; the cap lands on a three-node finishing tail",
        max_iterations=3, on_max_iterations="fin",
        nodes=[
            _BeginNode(id="begin"),
            _ToolCallNode(id="a", tool_id="x__a", arguments={}),
            _ToolCallNode(id="fin", tool_id="x__fin", arguments={}),
            _ToolCallNode(id="report", tool_id="x__report", arguments={}),
            _EndNode(id="end", output_template="{{ nodes.report.text }}"),
        ],
        edges=[
            _StaticEdge(from_node="begin", to_node="a"),
            _StaticEdge(from_node="a", to_node="a"),
            _StaticEdge(from_node="fin", to_node="report"),
            _StaticEdge(from_node="report", to_node="end"),
        ],
    )
    ex, events, thread = await _run(graph, _echo)

    assert _ends(events) == ["report"]
    assert (thread.ended_reason, thread.ended_detail) == ("completed", None)
    assert not [e for e in events if isinstance(e, _GraphErrorEvent)]


def _map_graph(items: list) -> Graph:
    return Graph(
        id="g-map", description="plan -> map -> t -> fan-in -> end",
        nodes=[
            _BeginNode(id="begin"),
            _ToolCallNode(id="plan", tool_id="x__plan", arguments={}, output_schema=OBJ),
            _FanOutNode(id="fo", specs=[FanOutSpec(kind="map", target_node_id="t", source_node_id="plan", source_path="items")]),
            _ToolCallNode(id="t", tool_id="x__work", arguments={"v": "{{ fanout_item }}"}),
            _FanInNode(id="fi", aggregate_template="{{ nodes.t | map(attribute='text') | join(',') }}"),
            _EndNode(id="end", output_template="{{ nodes.fi.text }}"),
        ],
        edges=[
            _StaticEdge(from_node="begin", to_node="plan"),
            _StaticEdge(from_node="plan", to_node="fo"),
            _StaticEdge(from_node="t", to_node="fi"),
            _StaticEdge(from_node="fi", to_node="end"),
        ],
    )


def _map_dispatcher(items: list):
    async def dispatcher(node, arguments):
        if node.tool_id == "x__plan":
            return ToolResultPart(id="p", output=json.dumps({"items": items}))
        return ToolResultPart(id="w", output=str(arguments["v"]))
    return dispatcher


@pytest.mark.asyncio
async def test_a_first_pass_map_fans_in_every_item_in_order():
    ex, events, thread = await _run(_map_graph(["a", "b", "c"]), _map_dispatcher(["a", "b", "c"]), max_parallel_nodes=2)

    assert _ends(events) == ["a,b,c"]
    assert thread.ended_reason == "completed"
    assert [n.text for n in ex._context.nodes["t"]] == ["a", "b", "c"]
    assert ex._fanout_target_expected_count["t"] == 3


@pytest.mark.asyncio
async def test_a_wide_broadcast_under_the_cap_runs_every_copy_in_index_order():
    graph = Graph(
        id="g-bc", description="broadcast 40 -> fan-in -> end",
        nodes=[
            _BeginNode(id="begin"),
            _FanOutNode(id="fo", specs=[FanOutSpec(kind="broadcast", target_node_id="t", count=40)]),
            _ToolCallNode(id="t", tool_id="x__t", arguments={"i": "{{ fanout_index }}"}),
            _FanInNode(id="fi", aggregate_template="{{ nodes.t | map(attribute='text') | join(',') }}"),
            _EndNode(id="end", output_template="{{ nodes.fi.text }}"),
        ],
        edges=[_StaticEdge(from_node="begin", to_node="fo"), _StaticEdge(from_node="t", to_node="fi"), _StaticEdge(from_node="fi", to_node="end")],
    )
    order: list[str] = []

    async def dispatcher(node, arguments):
        order.append(str(arguments["i"]))
        return ToolResultPart(id="x", output=str(arguments["i"]))

    _, events, thread = await _run(graph, dispatcher, max_parallel_nodes=3)

    assert _ends(events) == [",".join(str(i) for i in range(40))]
    assert thread.ended_reason == "completed"
    assert order == [sid[2:-1] for sid in sorted(f"t[{i}]" for i in range(40))], "instances are admitted in their sorted id order"


@pytest.mark.asyncio
async def test_main_loop_turn_log_records_keep_their_shape():
    turn_logs = _InMemoryStorage(TurnLogRecord)
    graph = Graph(
        id="g-tl", description="begin -> t -> end",
        nodes=[_BeginNode(id="begin"), _ToolCallNode(id="t", tool_id="x__t", arguments={}), _EndNode(id="end", output_template="ok")],
        edges=[_StaticEdge(from_node="begin", to_node="t"), _StaticEdge(from_node="t", to_node="end")],
    )
    await _run(graph, _echo, turn_logs=turn_logs)

    per_node = sorted((r.node_id, r.kind, r.seq) for r in turn_logs._data.values() if r.node_id is not None)
    assert per_node == [
        ("begin", "completed", 2), ("begin", "started", 1),
        ("end", "completed", 2), ("end", "started", 1),
        ("t", "completed", 2), ("t", "started", 1),
    ]


@pytest.mark.asyncio
async def test_a_json_path_router_from_a_plain_node_still_routes():
    graph = Graph(
        id="g-jp", description="t routes on its parsed output",
        nodes=[
            _BeginNode(id="begin"),
            _ToolCallNode(id="t", tool_id="x__t", arguments={}, output_schema=OBJ),
            _EndNode(id="yes", output_template="yes"),
            _EndNode(id="no", output_template="no"),
        ],
        edges=[
            _StaticEdge(from_node="begin", to_node="t"),
            _ConditionalEdge(from_node="t", router=_JsonPathRouter(
                branches=[JsonPathBranch(conditions=[BranchCondition(path="ok", op="eq", value=True)], to_node="yes")], default_to="no",
            )),
        ],
    )

    async def dispatcher(node, arguments):
        return ToolResultPart(id="x", output='{"ok": true}')

    _, events, thread = await _run(graph, dispatcher)

    assert _ends(events) == ["yes"]
    assert thread.ended_reason == "completed"
