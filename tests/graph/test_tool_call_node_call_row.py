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

from pathlib import Path
from typing import Any

import pytest

from primer.agent.invoke import invocation_depth_guard, run_subagent
from primer.graph.router import RouterRegistry
from primer.graph.workspace_executor import WorkspaceGraphExecutor
from primer.model.chat import ToolResultPart
from primer.model.graph import FanOutSpec, Graph, _BeginNode, _EndNode, _FanInNode, _FanOutNode, _GraphNodeRef, _StaticEdge, _ToolCallNode
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


def _graph(arguments: dict | None = None, tool_id: str = "t1__delegate") -> Graph:
    return Graph.model_construct(
        id="g", description="a ToolCall node that delegates", max_iterations=5, harness_id=None,
        nodes=[_BeginNode(id="begin"), _ToolCallNode(id="tool", tool_id=tool_id, arguments=arguments or {}), _EndNode(id="end", output_template="{{ nodes.tool.text }}")],
        edges=[_StaticEdge(from_node="begin", to_node="tool"), _StaticEdge(from_node="tool", to_node="end")],
    )


async def _executor(
    tmp_path: Path, *, fail: bool = False, graph: Graph | None = None, graphs: dict[str, Graph] | None = None, answers: int = 1, arguments: dict | None = None
) -> tuple[WorkspaceGraphExecutor, _DelegatingManager]:
    storage, registry = _world([_text("sub answer" if answers == 1 else f"sub answer {i}") for i in range(answers)])
    manager = _DelegatingManager(storage, registry, fail=fail)

    async def agent_resolver(agent_id: str) -> Any:
        raise KeyError(agent_id)

    async def llm_resolver(agent: Any) -> Any:
        raise NotImplementedError

    async def graph_resolver(graph_id: str) -> Graph:
        return (graphs or {})[graph_id]

    executor = WorkspaceGraphExecutor(
        graph=graph or _graph(arguments), agent_resolver=agent_resolver, llm_resolver=llm_resolver,  # type: ignore[arg-type]
        state_repo=await _make_state_repo(tmp_path), graph_session_id="gsid", tool_manager=manager,  # type: ignore[arg-type]
        router_registry=RouterRegistry(), graph_resolver=graph_resolver,
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


async def test_the_approval_key_of_a_gated_tool_call_node_has_no_node_scope(tmp_path: Path) -> None:
    """The channel inbox rebuilds a ToolCall node's key as ``tool_approval:<session>:<call id>`` (``_matching_event_keys``), without a node scope: publishing ``current_graph_node_id`` for this dispatch
    would add one (``ToolExecutionManager`` folds it into the key). Driven through a REAL manager with a policy that gates the tool, so the key is the one production builds."""
    from primer.graph._node_identity import current_graph_node_id
    from primer.model.yield_ import YieldToWorker
    from tests.agent.conftest import _EchoProvider
    from tests.agent.test_tool_manager_approval_gate import _PoliciesOnlyResolver
    from tests.graph.test_fanout_delegation_order import _Log

    from primer.agent.tool_manager import ToolExecutionManager
    from primer.model.principal import PrincipalRef
    from primer.model.tool_approval import RequiredApprovalConfig, ToolApprovalPolicy

    manager = ToolExecutionManager(toolset_providers={"_test": _EchoProvider()}, initiated_by=PrincipalRef.system())  # type: ignore[arg-type]
    manager._approval_resolver = _PoliciesOnlyResolver([ToolApprovalPolicy(id="p", toolset_id="_test", tool_name="echo", approval=RequiredApprovalConfig())])
    seen: list[Any] = []
    real = manager.execute

    async def watching(call: Any, **kw: Any) -> ToolResultPart:
        seen.append(current_graph_node_id())
        return await real(call, **kw)

    manager.execute = watching  # type: ignore[method-assign]
    executor, _ = await _executor(tmp_path, graph=_graph({"x": 1}, tool_id="_test__echo"))
    executor._tool_manager = manager
    log = _Log()
    with pytest.raises(YieldToWorker):
        await _run(executor, log=log)  # type: ignore[arg-type]
    records = [r.model_dump(mode="json") for r in log.records]
    (call,) = _tool_calls(records)
    (pending,) = executor._pending_toolcalls
    assert pending.parked_event_key == f"tool_approval:unknown:{call['payload']['raw_id']}", pending.parked_event_key
    assert seen == [None], "no graph node identity is published for a ToolCall node's dispatch"


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


# ---- review of #712, round 1 -------------------------------------------------------------------------------------------------------------------------------------------------------------------


def _wrapper(graph_id: str, inner_id: str) -> Graph:
    """begin -> one subgraph node running ``inner_id`` -> end."""
    return Graph.model_construct(
        id=graph_id, description=f"a subgraph node that runs {inner_id}", max_iterations=5, harness_id=None,
        nodes=[_BeginNode(id="begin"), _GraphNodeRef(id="sub", graph_id=inner_id), _EndNode(id="end", output_template="{{ nodes.sub.text }}")],
        edges=[_StaticEdge(from_node="begin", to_node="sub"), _StaticEdge(from_node="sub", to_node="end")],
    )


@pytest.fixture(params=[0, 1, 2], ids=lambda d: f"depth{d}")
def depth(request: pytest.FixtureRequest) -> int:
    """How many subgraph nodes the ToolCall node sits inside."""
    return request.param


async def _nested(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, depth: int) -> tuple[list[dict], _DelegatingManager]:
    graphs = {"g0": _graph()}
    top = "g0"
    for level in range(1, depth + 1):
        graphs[f"g{level}"] = _wrapper(f"g{level}", top)
        top = f"g{level}"
    real_build = WorkspaceGraphExecutor._build_sub_executor

    async def build(self, *a: Any, **k: Any) -> Any:
        child = await real_build(self, *a, **k)
        child._tool_manager = self._tool_manager      # production builds one lazily from the workspace session; the test hands the child the manager it counts
        return child

    monkeypatch.setattr(WorkspaceGraphExecutor, "_build_sub_executor", build)
    executor, manager = await _executor(tmp_path, graph=graphs[top], graphs=graphs)
    return await _run(executor), manager  # type: ignore[arg-type]


async def test_a_tool_call_node_inside_subgraph_nodes_stamps_its_run_with_its_own_node_not_the_outer_one(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, depth: int) -> None:
    """B1 of round 1: the parent's ``_stream_node`` publishes the subgraph node's id for the whole child run, and ``run_subagent`` preferred it over the ToolCall node's own, so the run was stamped ``sub``
    while its call row is under ``tool`` (the innermost node, which is where ``translate_stream_event`` files it): the run nested under nothing in both readers."""
    records, manager = await _nested(tmp_path, monkeypatch, depth)
    (call,) = _tool_calls(records)
    assert call["node_id"] == "tool"
    stamps = {(r["payload"]["delegate_tool_call_id"], r["payload"]["delegate_node_id"]) for r in _delegated(records)}
    assert stamps == {(manager.handed[0], "tool")}, stamps


async def test_both_readers_nest_the_run_of_a_tool_call_node_inside_subgraph_nodes_under_its_call(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, depth: int) -> None:
    records, _ = await _nested(tmp_path, monkeypatch, depth)
    (call,) = _tool_calls(records)
    folded = {r["seq"] for r in _delegated(records) if r["kind"] in ("llm_call", "tool_call")}
    assert folded and set(_py_children(records).get(call["seq"], [])) >= folded, ("timeline", call["seq"], _py_children(records))
    assert _js_children(records).get(call["seq"]), ("console", call["seq"], _js_children(records))


def test_the_innermost_node_wins_the_identity_in_both_orders() -> None:
    """B1: a ToolCall node's dispatch inside a subgraph node reads the ToolCall's identity; a graph node (an agent node of a graph the tool runs in-process) entered inside a ToolCall node's dispatch hides it
    and is read instead; leaving it brings the ToolCall's back."""
    from primer.graph._node_identity import (
        current_graph_node_id,
        current_toolcall_id,
        current_toolcall_node_id,
        reset_current_graph_node_id,
        reset_current_toolcall,
        set_current_graph_node_id,
        set_current_toolcall,
    )

    outer = set_current_graph_node_id("sub")
    try:
        call = set_current_toolcall("tool", "c1")
        try:
            assert (current_toolcall_node_id(), current_toolcall_id(), current_graph_node_id()) == ("tool", "c1", "sub")
            inner = set_current_graph_node_id("agent")
            try:
                assert (current_toolcall_node_id(), current_toolcall_id(), current_graph_node_id()) == (None, None, "agent"), "an inner graph node hides the ToolCall's identity"
            finally:
                reset_current_graph_node_id(inner)
            assert (current_toolcall_node_id(), current_toolcall_id()) == ("tool", "c1"), "and leaving it brings the ToolCall's back"
        finally:
            reset_current_toolcall(call)
        assert (current_toolcall_node_id(), current_graph_node_id()) == (None, "sub")
    finally:
        reset_current_graph_node_id(outer)


async def test_a_run_delegated_from_an_agent_node_inside_a_tool_calls_dispatch_is_stamped_with_that_agent_node() -> None:
    """B1: ``run_subagent`` reads the innermost identity. Here the ToolCall's tool runs a graph in-process (``invoke_graph``) and an agent node of it delegates."""
    from primer.graph._node_identity import (
        reset_current_graph_node_id,
        reset_current_toolcall,
        set_current_graph_node_id,
        set_current_toolcall,
    )
    from primer.session.delegation import DelegationRecorder, reset_delegation_sink, set_delegation_sink
    from tests.agent.test_delegated_runs_carry_a_run_id import _Bus, _Writer, _payloads
    from tests.agent.test_delegated_runs_carry_the_graph_node import _one_agent_world

    async def stamps(*, toolcall: str | None, node: str | None, then_node: str | None = None) -> set[str | None]:
        storage, registry = _one_agent_world("answer")
        writer = _Writer()
        sink = set_delegation_sink(DelegationRecorder(writer=writer, event_bus=_Bus(), session_id="sess-1"))
        tokens: list[tuple[str, Any]] = []
        try:
            if node:
                tokens.append(("node", set_current_graph_node_id(node)))
            if toolcall:
                tokens.append(("call", set_current_toolcall(toolcall, "c1")))
            if then_node:
                tokens.append(("node", set_current_graph_node_id(then_node)))
            with invocation_depth_guard():
                await run_subagent(
                    agent_id="agent-sub", prompt="go", storage_provider=storage, provider_registry=registry,
                    principal="user-1", session_id="sess-1", workspace_id="ws-1", invoke_tool_call_id="c1", turn_no=1,
                )
        finally:
            for kind, token in reversed(tokens):
                (reset_current_graph_node_id if kind == "node" else reset_current_toolcall)(token)
            reset_delegation_sink(sink)
        return {p.get("delegate_node_id") for p in _payloads(writer)}

    assert await stamps(toolcall="tool", node=None) == {"tool"}
    assert await stamps(toolcall="tool", node="sub") == {"tool"}, "a ToolCall node inside a subgraph node: the innermost"
    assert await stamps(toolcall="tool", node="sub", then_node="agent") == {"agent"}, "an agent node entered inside the ToolCall's dispatch"
    assert await stamps(toolcall=None, node="sub") == {"sub"}


async def test_an_agent_node_of_a_graph_the_tool_runs_in_process_stamps_its_run_with_itself_through_the_real_executor(tmp_path: Path) -> None:
    """B1 through the production wiring. The hide is pinned above through ``set_current_graph_node_id`` and ``run_subagent`` called by hand, which an executor branch that never hid the ToolCall's identity would
    pass. Here the ToolCall's tool runs a child graph IN-PROCESS (the ``invoke_graph`` shape: a child ``GraphExecutor`` iterated in the dispatch's own task, so the identity the dispatch published is still set when
    the child's agent node starts) and that agent node delegates through a real ``ToolExecutionManager``. The node's own dispatch sees ('tool', no graph node); the delegating tool, called from the agent node,
    sees (no ToolCall, 'a'); the run it starts is stamped 'a'."""
    from primer.agent.invoke import build_subagent_toolmanager
    from primer.graph._node_identity import current_graph_node_id, current_toolcall_node_id
    from primer.graph.executor import GraphExecutor
    from primer.model.agent import Agent, AgentModel
    from primer.model.chat import ToolCallResult
    from primer.model.graph import GraphNodeMessage, GraphThread, _AgentNodeRef
    from primer.model.model_profile import ModelProfileConfig
    from primer.model_profile import ResolvedModel
    from primer.worker.frames import AgentResumeContext
    from tests.agent.test_delegated_runs_carry_a_run_id import _DelegatingToolset
    from tests.graph.test_fanout_broadcast_e2e import _InMemoryStorage
    from tests.graph.test_fanout_delegation_order import _WorkerLLM

    seen: list[tuple[str, str | None, str | None]] = []

    class _IdentityToolset(_DelegatingToolset):
        """``system__invoke_agent``'s shape (``run_subagent`` under the id the manager was handed), recording which identity the tool is called under."""

        async def call(self, *, tool_name: str, arguments: Any, principal: Any = None, ctx: Any = None) -> ToolCallResult:
            seen.append(("the delegating tool", current_toolcall_node_id(), current_graph_node_id()))
            with invocation_depth_guard():
                text = await run_subagent(
                    agent_id="agent-sub", prompt="go", storage_provider=self.storage, provider_registry=self.registry,
                    principal=principal, session_id="s", workspace_id="ws-1", invoke_tool_call_id=ctx.tool_call_id, turn_no=1,
                )
            return ToolCallResult(output=text, is_error=False)

    storage, registry = _world([_text("sub answer")])
    toolset = _IdentityToolset()
    toolset.storage, toolset.registry = storage, registry
    registry._toolset = toolset
    resume_ctx = AgentResumeContext(session_id="s", workspace_id="ws-1", chat_id=None, principal="user-1", tools=["t1__delegate"], turn_no=1)
    child_graph = Graph.model_construct(
        id="child", description="begin -> an agent node that delegates -> end", max_iterations=5, harness_id=None,
        nodes=[_BeginNode(id="begin"), _AgentNodeRef(id="a", agent_id="ag", input_template="go"), _EndNode(id="end", output_template="{{ nodes.a.text }}")],
        edges=[_StaticEdge(from_node="begin", to_node="a"), _StaticEdge(from_node="a", to_node="end")],
    )

    async def child_tool_manager(agent: Any) -> Any:
        return await build_subagent_toolmanager(resume_ctx, storage_provider=storage, provider_registry=registry)

    async def child_agent(agent_id: str) -> Agent:
        return Agent(id=agent_id, description="the child graph's agent", model=AgentModel(profile_id="p--m"), system_prompt=[])

    async def child_llm(agent: Any, *a: Any, **k: Any) -> Any:
        return (_WorkerLLM(), ResolvedModel(profile_id="p", provider_id="pv", model_name="m", context_length=128_000, config=ModelProfileConfig()))

    class _InProcessGraphManager:
        """The node's tool: runs the child graph inside this very call, the way ``invoke_graph`` does."""

        def __init__(self) -> None:
            self.handed: list[str] = []

        async def execute(self, call: Any, *, principal: str | None = None, bypass_approval: bool = False) -> ToolResultPart:
            self.handed.append(call.id)
            seen.append(("the node's own dispatch", current_toolcall_node_id(), current_graph_node_id()))
            threads: _InMemoryStorage[GraphThread] = _InMemoryStorage(GraphThread)
            messages: _InMemoryStorage[GraphNodeMessage] = _InMemoryStorage(GraphNodeMessage)
            thread = await GraphExecutor.open_thread(graph=child_graph, thread_storage=threads, title="child")  # type: ignore[arg-type]
            child = GraphExecutor(
                graph=child_graph, agent_resolver=child_agent, llm_resolver=child_llm, thread_storage=threads, message_storage=messages,  # type: ignore[arg-type]
                graph_thread_id=thread.id, router_registry=RouterRegistry(), tool_manager_resolver=child_tool_manager,
            )
            text = ""
            async for event in child.invoke([]):
                if type(event).__name__ == "_GraphEndOutputEvent" and isinstance(getattr(event, "text", None), str):
                    text = event.text
            return ToolResultPart(id=call.id, output=text or "child done")

    async def agent_resolver(agent_id: str) -> Any:
        raise KeyError(agent_id)

    async def llm_resolver(agent: Any) -> Any:
        raise NotImplementedError

    manager = _InProcessGraphManager()
    executor = WorkspaceGraphExecutor(
        graph=_graph(), agent_resolver=agent_resolver, llm_resolver=llm_resolver,  # type: ignore[arg-type]
        state_repo=await _make_state_repo(tmp_path), graph_session_id="gsid", tool_manager=manager,  # type: ignore[arg-type]
        router_registry=RouterRegistry(),
    )
    records = await _run(executor)  # type: ignore[arg-type]
    assert seen == [("the node's own dispatch", "tool", None), ("the delegating tool", None, "a")], seen
    (call,) = _tool_calls(records)
    assert call["node_id"] == "tool" and manager.handed == [call["payload"]["raw_id"]]
    stamps = {(r["payload"]["delegate_tool_call_id"], r["payload"]["delegate_node_id"], r["payload"]["delegate_depth"]) for r in _delegated(records)}
    assert stamps == {("call_0", "a", 1)}, "the run the child graph's agent node delegated to is stamped with that node, not with the outer ToolCall node"
    results = [r for r in records if r["kind"] == "tool_result" and not r["payload"].get("delegated")]
    assert [(r["payload"]["call_id"], r["payload"]["error"]) for r in results] == [(call["payload"]["id"], False)]


def _fanout_graph(instances: int = 2) -> Graph:
    return Graph.model_construct(
        id="gf", description="a fan-out of ToolCall nodes", max_iterations=10, harness_id=None,
        nodes=[
            _BeginNode(id="begin"),
            _FanOutNode(id="fan", specs=[FanOutSpec(kind="broadcast", target_node_id="tool", count=instances)]),
            _ToolCallNode(id="tool", tool_id="t1__delegate", arguments={}),
            _FanInNode(id="agg", aggregate_template="{% for n in nodes.tool %}{{ n.text }}{% endfor %}"),
            _EndNode(id="end", output_template="{{ nodes.agg.text }}"),
        ],
        edges=[_StaticEdge(from_node="begin", to_node="fan"), _StaticEdge(from_node="tool", to_node="agg"), _StaticEdge(from_node="agg", to_node="end")],
    )


async def test_a_fan_out_of_tool_call_nodes_writes_a_row_per_instance_and_each_run_nests_under_its_own(tmp_path: Path) -> None:
    """N1: the rows carry the fan-out-qualified node id (``tool[0]``), not the definition's ``tool``, with a call id of their own, and the runs are told apart by node, not by a raw id."""
    executor, manager = await _executor(tmp_path, graph=_fanout_graph(), answers=2)
    records = await _run(executor)  # type: ignore[arg-type]
    calls = {c["node_id"]: c for c in _tool_calls(records)}
    assert sorted(calls) == ["tool[0]", "tool[1]"], sorted(calls)
    assert len({c["payload"]["raw_id"] for c in calls.values()}) == 2 and {c["payload"]["raw_id"] for c in calls.values()} == set(manager.handed)
    by_seq = {r["seq"]: r for r in records}
    py, js = _py_children(records), _js_children(records)
    for node, call in calls.items():
        for children in (py[call["seq"]], js[call["seq"]]):
            assert children, (node, "nothing under its call")
            assert {by_seq[s]["payload"].get("delegate_node_id") for s in children} == {node}, (node, children)
        assert {r["payload"]["delegate_tool_call_id"] for r in _delegated(records) if r["payload"]["delegate_node_id"] == node} == {call["payload"]["raw_id"]}


async def test_a_tool_that_raises_after_delegating_answers_its_row_with_an_error(tmp_path: Path) -> None:
    """N1: an exception from the dispatch is an error answer under the row's id (the node fails as before), and the run it delegated to is still under the call."""
    executor, manager = await _executor(tmp_path)
    real = manager.execute

    async def raises_after_delegating(call: Any, **kw: Any) -> ToolResultPart:
        await real(call, **kw)
        raise RuntimeError("the tool broke after delegating")

    manager.execute = raises_after_delegating  # type: ignore[method-assign]
    records = await _run(executor)  # type: ignore[arg-type]
    (call,) = _tool_calls(records)
    results = [r for r in records if r["kind"] == "tool_result" and r["node_id"] == "tool"]
    assert [(r["payload"]["call_id"], r["payload"]["error"]) for r in results] == [(call["payload"]["id"], True)]
    assert "the tool broke after delegating" in str(results[0]["payload"]["output"])
    assert _py_children(records).get(call["seq"]), "the run it delegated to is still under the call"


async def test_the_call_row_carries_the_arguments_the_tool_was_called_with(tmp_path: Path) -> None:
    """N1: ``ToolCallEnd`` carries the node's rendered arguments, which are what the row and the console show."""
    records, _ = await _records(tmp_path, arguments={"path": ".", "depth": 2})
    (call,) = _tool_calls(records)
    assert call["payload"]["arguments"] == {"path": ".", "depth": 2}
