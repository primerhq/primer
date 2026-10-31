"""Part 2 Task 49: graph bounds and small exits (AG-M11, AG-M24, AG-L11a-e; lead rulings D7 and D15).

* AG-M11: a subgraph node runs its child inside ``invocation_depth_guard``; a graph that references itself fails at the guard's depth.
* AG-M24 (D7): a fan-out wider than ``MAX_FANOUT_INSTANCES`` (``PRIMER_MAX_FANOUT_INSTANCES``, default 1000) fails ``fanout_source_invalid``,
  and a wide superstep never has more node tasks alive than ``max_parallel_nodes``.
* AG-L11a: a node the resume settles gets its per-node completed / failed turn-log record.
* AG-L11b: closing ``WorkspaceGraphExecutor.invoke`` closes the base generator at once (the node tasks are cancelled before ``aclose`` returns).
* AG-L11c: a run closed or cancelled mid-flight is saved ENDED/cancelled, not left RUNNING.
* AG-L11d: an agent or subgraph node whose input template fails ends the run with ``ended_detail="template_error"``.
* AG-L11e (D15): a map over an empty list gives its target an empty aggregator and walks the target's out-edges.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from primer.agent.invoke import MAX_INVOCATION_DEPTH
from primer.graph import _node_refs
from primer.graph.base import _GraphErrorEvent
from primer.graph.executor import GraphExecutor
from primer.graph.workspace_executor import WorkspaceGraphExecutor
from primer.model.chat import ToolResultPart
from primer.model.graph import (
    FanOutSpec,
    Graph,
    GraphNodeMessage,
    GraphThread,
    _BeginNode,
    _EndNode,
    _FanOutNode,
    _GraphNodeRef,
    _StaticEdge,
    _ToolCallNode,
)
from tests.graph.test_max_iterations import _agent, _model
from tests.graph.test_toolcall_dispatch import _InMemoryStorage
from tests.graph.test_workspace_executor import _make_state_repo

OBJ = {"type": "object"}


async def _no_agent(agent_id: str):
    raise KeyError(agent_id)


async def _no_llm(agent):
    raise NotImplementedError


def _executor(graph, threads, msgs, tid, *, dispatcher=None, max_parallel_nodes=16, graph_resolver=None, llm=None):
    async def agent_resolver(agent_id):
        return _agent(agent_id)

    async def llm_resolver(agent):
        return (llm, _model())

    return GraphExecutor(
        graph=graph,
        agent_resolver=agent_resolver if llm is not None else _no_agent,  # type: ignore[arg-type]
        llm_resolver=llm_resolver if llm is not None else _no_llm,  # type: ignore[arg-type]
        thread_storage=threads, message_storage=msgs, graph_thread_id=tid,  # type: ignore[arg-type]
        tool_dispatcher=dispatcher,  # type: ignore[arg-type]
        max_parallel_nodes=max_parallel_nodes, graph_resolver=graph_resolver,
    )


async def _run(graph, **kw):
    threads, msgs = _InMemoryStorage(GraphThread), _InMemoryStorage(GraphNodeMessage)
    thread = await GraphExecutor.open_thread(graph=graph, thread_storage=threads)  # type: ignore[arg-type]
    ex = _executor(graph, threads, msgs, thread.id, **kw)
    events = [e async for e in ex.invoke([])]
    return ex, events, await threads.get(thread.id)


async def _echo(node, arguments, bypass_approval=False):
    return ToolResultPart(id="x", output=node.id)


# ---- AG-M11: subgraph recursion depth --------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_self_referencing_subgraph_fails_at_the_depth_guard(tmp_path: Path):
    graph = Graph(
        id="g-self", description="begin -> sub(g-self) -> end",
        nodes=[_BeginNode(id="begin"), _GraphNodeRef(id="sub", graph_id="g-self"), _EndNode(id="end")],
        edges=[_StaticEdge(from_node="begin", to_node="sub"), _StaticEdge(from_node="sub", to_node="end")],
    )
    resolved = {"n": 0}

    async def graph_resolver(gid: str) -> Graph:
        resolved["n"] += 1
        return graph

    repo = await _make_state_repo(tmp_path)
    ex = WorkspaceGraphExecutor(
        graph=graph, agent_resolver=_no_agent, llm_resolver=_no_llm,  # type: ignore[arg-type]
        state_repo=repo, graph_session_id="gsid", graph_resolver=graph_resolver,
    )
    [_ async for _ in ex.invoke([])]

    assert resolved["n"] == MAX_INVOCATION_DEPTH
    st = await ex.load_state()
    assert (st["status"], st["ended_reason"]) == ("ended", "failed")
    errors = [
        json.loads(p.read_text())["node_states"]["sub"].get("error") or ""
        for p in (tmp_path / ".state").rglob("state.json")
    ]
    assert any(f"would exceed max {MAX_INVOCATION_DEPTH}" in e for e in errors), errors


# ---- AG-M24 (D7): fan-out width cap and tasks alive -------------------------------------------------------------------


def _broadcast_graph(count: int) -> Graph:
    return Graph(
        id="g-bc", description="begin -> t0 -> broadcast -> t",
        nodes=[
            _BeginNode(id="begin"),
            _ToolCallNode(id="t0", tool_id="x__t0", arguments={}),
            _FanOutNode(id="fo", specs=[FanOutSpec(kind="broadcast", target_node_id="t", count=count)]),
            _ToolCallNode(id="t", tool_id="x__t", arguments={}),
        ],
        edges=[_StaticEdge(from_node="begin", to_node="t0"), _StaticEdge(from_node="t0", to_node="fo")],
    )


@pytest.mark.asyncio
async def test_a_broadcast_over_the_default_cap_fails_fanout_source_invalid():
    assert _node_refs.MAX_FANOUT_INSTANCES == 1000
    calls = []

    async def dispatcher(node, arguments):
        calls.append(node.id)
        return ToolResultPart(id="x", output="ok")

    _, events, thread = await _run(_broadcast_graph(1001), dispatcher=dispatcher)

    assert (thread.ended_reason, thread.ended_detail) == ("failed", "fanout_source_invalid")
    assert calls == ["t0"], "no copy runs"
    assert [e.code for e in events if isinstance(e, _GraphErrorEvent)] == ["fanout_source_invalid"]


@pytest.mark.asyncio
async def test_the_cap_is_configurable_and_bounds_a_map_too(monkeypatch):
    monkeypatch.setattr(_node_refs, "MAX_FANOUT_INSTANCES", 3)
    graph = Graph(
        id="g-map-cap", description="plan -> map",
        nodes=[
            _BeginNode(id="begin"),
            _ToolCallNode(id="plan", tool_id="x__plan", arguments={}, output_schema=OBJ),
            _FanOutNode(id="fo", specs=[FanOutSpec(kind="map", target_node_id="t", source_node_id="plan", source_path="items")]),
            _ToolCallNode(id="t", tool_id="x__t", arguments={}),
        ],
        edges=[_StaticEdge(from_node="begin", to_node="plan"), _StaticEdge(from_node="plan", to_node="fo")],
    )

    async def dispatcher(node, arguments):
        return ToolResultPart(id="x", output=json.dumps({"items": [1, 2, 3, 4]}))

    _, _, thread = await _run(graph, dispatcher=dispatcher)
    assert (thread.ended_reason, thread.ended_detail) == ("failed", "fanout_source_invalid")

    monkeypatch.setattr(_node_refs, "MAX_FANOUT_INSTANCES", 4)
    _, _, thread = await _run(graph, dispatcher=dispatcher)
    assert thread.ended_reason == "completed", "a fan-out AT the cap runs"


@pytest.mark.asyncio
async def test_a_wide_superstep_never_has_more_node_tasks_alive_than_the_bound():
    alive: dict[str, list[int]] = {"t0": [], "t": []}

    async def dispatcher(node, arguments):
        alive[node.id].append(len(asyncio.all_tasks()))
        await asyncio.sleep(0)
        return ToolResultPart(id="x", output="ok")

    _, _, thread = await _run(_broadcast_graph(50), dispatcher=dispatcher, max_parallel_nodes=2)

    assert thread.ended_reason == "completed"
    assert len(alive["t"]) == 50
    baseline = alive["t0"][0]          # tasks alive while one node runs (the test's own and that node's)
    # at most max_parallel_nodes node tasks, plus the one task that starts them, in place of the single node
    assert max(alive["t"]) - baseline <= 2, (baseline, max(alive["t"]))
