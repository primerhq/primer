"""Every failure exit of ``resume_from_checkpoint`` exposes how the graph ended (ticket 01a11f35, round 4 of #701, review B2-N3).

A graph that parks and is resumed ends through the pool, so the record that closes its turn is written by ``end_graph`` from ``graph_end_for(executor.last_done_reason, executor)``. ``last_done_reason`` reads
``_last_ended_reason``, which the main loop sets at its tail. The eight early exits of ``resume_from_checkpoint`` (a resumed tool that raises, a mapped tool error, a rejected approval, the resumed
agent or tool_wait node failing, routing failing) save ``ENDED/failed`` and ``return`` without it, so ``last_done_reason`` stayed ``None`` and the record said ``done(stop, graph_ended, completed)`` for a
graph that failed (found with real executors by the round 3 review; the round 3 tests used ``SimpleNamespace`` stand-ins and could not see it).

Each test parks a REAL executor, resumes a fresh one the way the worker does and reads what the executor says and what ``graph_end_for`` would write: ``done(error, graph_failed, failed)`` and the saved
state ``ENDED/failed``. The exits are grouped by what fails: a ToolCall node (the resumed tool raises, answers with an error, is rejected), a resumed agent node (an ask_user yield, a tool_wait batch) and routing.
"""

from __future__ import annotations

import pytest

from primer.graph.base import _GraphErrorEvent, _ToolApprovalRejected
from primer.graph.executor import GraphExecutor
from primer.model.chat import Message, ToolResultPart
from primer.model.graph import (
    BranchCondition,
    Graph,
    GraphNodeMessage,
    GraphThread,
    JsonPathBranch,
    _AgentNodeRef,
    _BeginNode,
    _ConditionalEdge,
    _EndNode,
    _JsonPathRouter,
    _StaticEdge,
    _ToolCallNode,
)
from primer.model.yield_ import ToolWaitPark, Yielded, YieldToWorker
from primer.session.graph_end import graph_end_for
from tests.graph.test_tool_wait_graph_park import _ask_user_yield, _mk_executor_for_graph, _mk_parallel_executor, _tool_wait_park
from tests.graph.test_toolcall_dispatch import _InMemoryStorage


def _says_failed(executor) -> None:
    """What ``end_graph`` writes for this executor: a node-less ``done(error)`` with the graph's own end, ``failed``."""
    assert executor.last_done_reason == "graph_failed", "the executor does not know how the resumed run ended"
    record = graph_end_for(executor.last_done_reason, executor)
    assert record is not None
    assert record.payload["stop_reason"] == "error" and record.payload["raw_reason"] == "graph_failed"
    assert record.payload["ended_reason"] == "failed" and record.payload["graph_end"] is True


# ---- a ToolCall node: the resumed tool raises, answers with an error, or is rejected ----------------------------------------------------------------------------------------


def _toolcall_graph() -> Graph:
    return Graph(
        id="g-tool", description="begin -> tool -> end",
        nodes=[_BeginNode(id="begin"), _ToolCallNode(id="t", tool_id="dangerous__tool", arguments={"q": "x"}), _EndNode(id="exit", output_template="done")],
        edges=[_StaticEdge(from_node="begin", to_node="t"), _StaticEdge(from_node="t", to_node="exit")],
    )


async def _resume_toolcall(second, *, tool_name: str = "_approval") -> GraphExecutor:
    """Park a ToolCall node (an approval gate), then resume a fresh executor whose dispatcher is ``second``."""
    graph = _toolcall_graph()
    gate = Yielded(tool_name=tool_name, event_key="tool_approval:sid:tc-1")

    async def first(node, arguments):
        raise YieldToWorker(gate, tool_call_id="tc-1")

    async def agent_resolver(agent_id):
        raise KeyError(agent_id)

    async def llm_resolver(agent):
        raise NotImplementedError

    ts, ms = _InMemoryStorage(GraphThread), _InMemoryStorage(GraphNodeMessage)
    thread = await GraphExecutor.open_thread(graph=graph, thread_storage=ts)
    ex1 = GraphExecutor(graph=graph, agent_resolver=agent_resolver, llm_resolver=llm_resolver, thread_storage=ts, message_storage=ms, graph_thread_id=thread.id, tool_dispatcher=first)
    with pytest.raises(YieldToWorker) as parked:
        async for _ in ex1.invoke([]):
            pass
    ex2 = GraphExecutor(graph=graph, agent_resolver=agent_resolver, llm_resolver=llm_resolver, thread_storage=ts, message_storage=ms, graph_thread_id=thread.id, tool_dispatcher=second)
    async for _ in ex2.resume_from_checkpoint(parked.value.graph_checkpoint):
        pass
    saved = await ts.get(thread.id)
    assert saved.ended_reason == "failed", "the saved state already said failed: only the executor's own outcome was missing"
    return ex2


@pytest.mark.asyncio
async def test_a_rejected_approval_ends_the_resumed_graph_failed() -> None:
    async def reject(node, arguments, bypass_approval=False):
        raise _ToolApprovalRejected("operator rejected", tool_call_id="tc-1")

    _says_failed(await _resume_toolcall(reject))


@pytest.mark.asyncio
async def test_a_resumed_tool_that_raises_ends_the_graph_failed() -> None:
    async def explode(node, arguments, bypass_approval=False):
        raise RuntimeError("the tool blew up")

    _says_failed(await _resume_toolcall(explode))


@pytest.mark.asyncio
async def test_a_resumed_tool_that_answers_with_an_error_ends_the_graph_failed() -> None:
    async def error_result(node, arguments, bypass_approval=False):
        return ToolResultPart(id="tc-1", output="the tool refused", error=True)

    _says_failed(await _resume_toolcall(error_result))


async def _never_dispatched(node, arguments, bypass_approval=False):
    raise AssertionError("a value-yielding ToolCall node is resumed by its hook, not re-dispatched")


@pytest.mark.asyncio
async def test_a_value_yield_resume_hook_that_raises_ends_the_graph_failed(monkeypatch) -> None:
    """A ToolCall node that parked on a value-yielding tool (ask_user) is resumed by the tool's resume hook on the operator's reply."""
    import primer.graph.base as base

    async def hook_fails(**kwargs):
        raise RuntimeError("the resume hook failed")

    monkeypatch.setattr(base, "_is_value_yield_toolcall", lambda entry: True)
    monkeypatch.setattr(base, "_resume_value_yield_toolcall", hook_fails)

    _says_failed(await _resume_toolcall(_never_dispatched, tool_name="ask_user"))


@pytest.mark.asyncio
async def test_a_value_yield_resume_hook_that_answers_with_an_error_ends_the_graph_failed(monkeypatch) -> None:
    import primer.graph.base as base

    async def hook_errors(**kwargs):
        return ToolResultPart(id="tc-1", output="the reply was refused", error=True)

    monkeypatch.setattr(base, "_is_value_yield_toolcall", lambda entry: True)
    monkeypatch.setattr(base, "_resume_value_yield_toolcall", hook_errors)

    _says_failed(await _resume_toolcall(_never_dispatched, tool_name="ask_user"))


# ---- a resumed agent node: an ask_user yield, a tool_wait batch -----------------------------------------------------------------------------------------------------------


def _fail_when_resumed(monkeypatch, parks: dict) -> None:
    """The first dispatch of an agent raises its park (``parks``); every later dispatch of it (the resumed continuation) raises a failure, as a model call that fails does."""
    import primer.graph._agent_node as agent_node_mod

    seen: set[str] = set()

    async def _spy(**kwargs):
        agent = kwargs["agent"]
        if agent.id in parks:
            if agent.id not in seen:
                seen.add(agent.id)
                raise parks[agent.id]
            raise RuntimeError("the model call failed")
        return
        yield  # pragma: no cover - keeps this an async generator

    monkeypatch.setattr(agent_node_mod, "run_agent_turn", _spy)


@pytest.mark.asyncio
async def test_a_resumed_agent_node_that_fails_ends_the_graph_failed(monkeypatch) -> None:
    ex1 = await _mk_parallel_executor()
    _fail_when_resumed(monkeypatch, {"agent-a": _ask_user_yield("A", "tc-a")})
    with pytest.raises(YieldToWorker) as parked:
        async for _ in ex1.invoke([]):
            pass
    ex2 = await _mk_parallel_executor()
    answer = Message(role="tool", parts=[ToolResultPart(id="tc-a", output="blue")])

    async for _ in ex2.resume_from_checkpoint(parked.value.graph_checkpoint, resumed_tcid="tc-a", agent_tool_result=answer):
        pass

    _says_failed(ex2)


@pytest.mark.asyncio
async def test_a_resumed_tool_wait_node_that_fails_ends_the_graph_failed(monkeypatch) -> None:
    ex1 = await _mk_parallel_executor()
    _fail_when_resumed(monkeypatch, {"agent-a": _tool_wait_park("A", "1")})
    with pytest.raises(ToolWaitPark) as parked:
        async for _ in ex1.invoke([]):
            pass
    ex2 = await _mk_parallel_executor()

    async for _ in ex2.resume_from_checkpoint(parked.value.graph_checkpoint, resolved_tool_wait={"A": [ToolResultPart(id="A:tool:0:1", output="result A", error=False)]}):
        pass

    _says_failed(ex2)


# ---- routing ------------------------------------------------------------------------------------------------------------------------------------------------------------------------


def _routing_graph() -> Graph:
    """begin -> A -> a conditional edge none of whose branches matches A's output (and no ``default_to``) -> exit: the shape of tests/graph/test_routing_failed.py."""
    return Graph(
        id="g-routing", description="begin -> A -> conditional (no match, no default) -> exit",
        nodes=[_BeginNode(id="begin"), _AgentNodeRef(id="A", agent_id="agent-a"), _EndNode(id="exit")],
        edges=[
            _StaticEdge(from_node="begin", to_node="A"),
            _ConditionalEdge(
                from_node="A",
                router=_JsonPathRouter(branches=[JsonPathBranch(conditions=[BranchCondition(path="go", op="eq", value="exit")], to_node="exit")]),
            ),
        ],
    )


@pytest.mark.asyncio
async def test_routing_that_fails_after_a_resume_ends_the_graph_failed(monkeypatch) -> None:
    ex1 = await _mk_executor_for_graph(_routing_graph())
    import primer.graph._agent_node as agent_node_mod

    first = {"done": False}

    async def _park_a_once(**kwargs):
        if kwargs["agent"].id == "agent-a" and not first["done"]:
            first["done"] = True
            raise _tool_wait_park("A", "1")
        return
        yield  # pragma: no cover

    monkeypatch.setattr(agent_node_mod, "run_agent_turn", _park_a_once)
    with pytest.raises(ToolWaitPark) as parked:
        async for _ in ex1.invoke([]):
            pass
    ex2 = await _mk_executor_for_graph(_routing_graph())

    events = [ev async for ev in ex2.resume_from_checkpoint(parked.value.graph_checkpoint, resolved_tool_wait={"A": [ToolResultPart(id="A:tool:0:1", output="result A", error=False)]})]

    assert [ev.code for ev in events if isinstance(ev, _GraphErrorEvent)] == ["routing_failed"], "the failure under test is the router's own, not a stand-in"
    _says_failed(ex2)
