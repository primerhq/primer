"""A graph ToolCall node's call is a transcript row, and the run it delegates to nests under it (ticket 01a11faa, found in the #659 review ruling).

A ToolCall node dispatches its tool through the executor's ``_dispatch_toolcall`` with a call id the dispatcher mints (a fresh uuid) and no event of its own: no ``tool_call`` row, no ``tool_result`` row, and the
graph-node identity (``delegate_node_id``) is only published for agent and subgraph nodes. So a ToolCall node whose tool delegates (``system__invoke_agent`` and the like) left the delegated run's records at the root
of the console and of the timeline, with no call to hang under, in a graph whose agent nodes nest correctly.

The node now mints the call id itself, writes the call row (``ToolCallStart`` + ``ToolCallEnd`` under the node's id) and waits for the drainer to have written it before the tool runs (the barrier an agent node's
loop awaits too), hands the id to the dispatcher so the manager's ``ToolCallPart`` carries it, publishes which node the dispatch belongs to for the delegation recorder, and writes the result row when the tool answers.

These cases drive the real ``WorkspaceGraphExecutor`` (its own ``_dispatch_toolcall`` builds the ``ToolCallPart``), a manager that runs the real ``run_subagent`` under the id of the call it was handed (what the
``system__invoke_agent`` tool does), the real ``DelegationRecorder`` and the real ``translate_stream_event`` the way ``dispatch.py`` wires them, and fold the log with both readers (the timeline and the console).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from primer.agent.invoke import invocation_depth_guard, run_subagent
from primer.graph.router import RouterRegistry
from primer.graph.workspace_executor import WorkspaceGraphExecutor
from primer.model.chat import ToolResultPart
from primer.model.graph import Graph, _BeginNode, _EndNode, _StaticEdge, _ToolCallNode
from primer.session.persistence import _CoalesceState
from tests.agent.test_delegated_runs_carry_a_run_id import _text, _world
from tests.graph.test_fanout_delegation_order import _js_children, _py_children, _run
from tests.graph.test_workspace_executor import _make_state_repo


class _DelegatingManager:
    """What a ToolCall node's tool does when it delegates: run a subagent under the id of the call this manager was handed."""

    def __init__(self, storage: Any, registry: Any, *, fail: bool = False) -> None:
        self.storage, self.registry, self.fail = storage, registry, fail
        self.handed: list[str] = []

    async def execute(self, call: Any, *, principal: str | None = None, bypass_approval: bool = False) -> ToolResultPart:
        self.handed.append(call.id)
        with invocation_depth_guard():
            text = await run_subagent(
                agent_id="agent-sub", prompt="go", storage_provider=self.storage, provider_registry=self.registry,
                principal="user-1", session_id="s", workspace_id="ws-1", invoke_tool_call_id=call.id, turn_no=1,
            )
        if self.fail:
            return ToolResultPart(id=call.id, output="the tool failed", error=True)
        return ToolResultPart(id=call.id, output=text)


def _graph() -> Graph:
    return Graph.model_construct(
        id="g", description="a ToolCall node that delegates", max_iterations=5, harness_id=None,
        nodes=[_BeginNode(id="begin"), _ToolCallNode(id="tool", tool_id="t1__delegate", arguments={}), _EndNode(id="end", output_template="{{ nodes.tool.text }}")],
        edges=[_StaticEdge(from_node="begin", to_node="tool"), _StaticEdge(from_node="tool", to_node="end")],
    )


async def _executor(tmp_path: Path, *, fail: bool = False) -> tuple[WorkspaceGraphExecutor, _DelegatingManager]:
    storage, registry = _world([_text("sub answer")])
    manager = _DelegatingManager(storage, registry, fail=fail)

    async def agent_resolver(agent_id: str) -> Any:
        raise KeyError(agent_id)

    async def llm_resolver(agent: Any) -> Any:
        raise NotImplementedError

    executor = WorkspaceGraphExecutor(
        graph=_graph(), agent_resolver=agent_resolver, llm_resolver=llm_resolver,  # type: ignore[arg-type]
        state_repo=await _make_state_repo(tmp_path), graph_session_id="gsid", tool_manager=manager,  # type: ignore[arg-type]
        router_registry=RouterRegistry(),
    )
    return executor, manager


async def _records(tmp_path: Path, **kw: Any) -> tuple[list[dict], _DelegatingManager]:
    executor, manager = await _executor(tmp_path, **kw)
    return await _run(executor), manager  # type: ignore[arg-type]


def _tool_calls(records: list[dict]) -> list[dict]:
    """The node's own call rows: not the delegated run's."""
    return [r for r in records if r["kind"] == "tool_call" and not r["payload"].get("delegated")]


def _delegated(records: list[dict]) -> list[dict]:
    return [r for r in records if r["payload"].get("delegated")]


async def test_the_tool_call_node_writes_one_call_row_under_its_own_node_id(tmp_path: Path) -> None:
    records, manager = await _records(tmp_path)
    calls = _tool_calls(records)
    assert [(c["node_id"], c["payload"]["name"]) for c in calls] == [("tool", "t1__delegate")], [r["kind"] for r in records]
    assert calls[0]["payload"]["raw_id"] == manager.handed[0], "the row carries the id the manager was handed, so a run delegated by that call can name it"


async def test_the_call_row_is_answered_by_a_result_row_with_the_same_id(tmp_path: Path) -> None:
    records, _ = await _records(tmp_path)
    call = _tool_calls(records)[0]
    results = [r for r in records if r["kind"] == "tool_result" and r["node_id"] == "tool"]
    assert [(r["payload"]["call_id"], r["payload"]["output"], r["payload"]["error"]) for r in results] == [(call["payload"]["id"], "sub answer", False)]
    assert results[0]["seq"] > call["seq"]


async def test_the_delegated_run_says_which_call_and_which_node_it_belongs_to(tmp_path: Path) -> None:
    records, manager = await _records(tmp_path)
    delegated = _delegated(records)
    assert delegated, "the scene delegates"
    assert {(r["payload"]["delegate_tool_call_id"], r["payload"]["delegate_node_id"]) for r in delegated} == {(manager.handed[0], "tool")}


async def test_every_delegated_record_is_written_after_the_call_row_that_started_it(tmp_path: Path) -> None:
    records, _ = await _records(tmp_path)
    call = _tool_calls(records)[0]
    assert [r["seq"] for r in _delegated(records) if r["seq"] < call["seq"]] == []


async def test_the_timeline_puts_the_run_under_the_nodes_call(tmp_path: Path) -> None:
    records, _ = await _records(tmp_path)
    call = _tool_calls(records)[0]
    children = _py_children(records)
    folded = {r["seq"] for r in _delegated(records) if r["kind"] in ("llm_call", "tool_call")}
    assert folded and set(children.get(call["seq"], [])) >= folded, (call["seq"], children)


async def test_the_console_puts_the_run_under_the_nodes_call(tmp_path: Path) -> None:
    records, _ = await _records(tmp_path)
    call = _tool_calls(records)[0]
    children = _js_children(records)
    assert children.get(call["seq"]), (call["seq"], children)
    assert {r["seq"] for r in _delegated(records)} >= set(children[call["seq"]])


async def test_a_tool_that_answers_with_an_error_still_gets_its_result_row(tmp_path: Path) -> None:
    """The node fails as before (``tool_execution_failed``); the call row must not be left open."""
    records, _ = await _records(tmp_path, fail=True)
    call = _tool_calls(records)[0]
    results = [r for r in records if r["kind"] == "tool_result" and r["node_id"] == "tool"]
    assert [(r["payload"]["call_id"], r["payload"]["error"]) for r in results] == [(call["payload"]["id"], True)]


async def test_the_dispatch_runs_without_a_graph_node_identity_so_the_approval_key_keeps_its_shape(tmp_path: Path) -> None:
    """The channel inbox rebuilds a ToolCall node's approval key as ``tool_approval:<session>:<call id>`` (``_matching_event_keys``), without a node scope: publishing ``current_graph_node_id`` for this dispatch
    would add one (``ToolExecutionManager`` folds it into the key). The delegation recorder reads its own ambient value instead."""
    from primer.graph._node_identity import current_graph_node_id

    seen: list[Any] = []
    executor, manager = await _executor(tmp_path)
    real = manager.execute

    async def spy(call: Any, **kw: Any) -> ToolResultPart:
        seen.append(current_graph_node_id())
        return await real(call, **kw)

    manager.execute = spy  # type: ignore[method-assign]
    await _run(executor)  # type: ignore[arg-type]
    assert seen == [None]


async def test_a_call_that_parks_on_approval_has_its_call_row_and_no_result_row_yet(tmp_path: Path) -> None:
    """The park (an approval gate) is recorded under the id the call row carries, so ``stash_graph_scoped_ids`` (it looks the scoped id up by node id and tool call id) finds the row the resume's result row has to
    pair with; before the call had no row at all and the resume wrote a result for nothing. The call stays open until the operator decides."""
    from primer.model.yield_ import Yielded, YieldToWorker
    from tests.graph.test_fanout_delegation_order import _Log

    executor, manager = await _executor(tmp_path)

    async def park(call: Any, *, principal: str | None = None, bypass_approval: bool = False) -> ToolResultPart:
        manager.handed.append(call.id)
        raise YieldToWorker(Yielded(tool_name="_approval", event_key=f"tool_approval:s:{call.id}", timeout=60.0), tool_call_id=call.id)

    manager.execute = park  # type: ignore[method-assign]
    log = _Log()
    with pytest.raises(YieldToWorker):
        await _run(executor, log=log)  # type: ignore[arg-type]
    records = [r.model_dump(mode="json") for r in log.records]
    call = _tool_calls(records)[0]
    (pending,) = executor._pending_toolcalls
    assert (call["node_id"], call["payload"]["raw_id"]) == (pending.node_id, pending.tool_call_id) == ("tool", manager.handed[0])
    assert pending.parked_event_key == f"tool_approval:s:{manager.handed[0]}", "no node scope in the approval key: the channel inbox rebuilds it without one"
    assert [r for r in records if r["kind"] == "tool_result"] == []
