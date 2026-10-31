"""Characterization of graph runs, parks and resumes (Part 2 Task 47 guard).

Pins what the executor does today for the shapes graph resume touches: linear,
branching, fan-out and loop runs with no park, and approval, ask_user and
agent-node parks with their answers. Each scenario returns a compact trace (the
transition, End and error events in stream order) plus the thread's terminal
state, so a change to the resume path that moves any of it shows up here.
"""

from __future__ import annotations

from typing import Any

import pytest

import primer.toolset.system  # noqa: F401  (registers the ask_user resume hook)
from primer.graph.base import (
    _GraphEndOutputEvent,
    _GraphErrorEvent,
    _GraphTransitionEvent,
    _ToolApprovalRejected,
)
from primer.graph.executor import GraphExecutor
from primer.model.agent import Agent
from primer.model.chat import Message, ToolResultPart
from primer.model.graph import (
    FanOutSpec,
    Graph,
    GraphNodeMessage,
    GraphThread,
    _AgentNodeRef,
    _BeginNode,
    _EndNode,
    _FanInNode,
    _FanOutNode,
    _StaticEdge,
    _ToolCallNode,
)
from primer.model.yield_ import ToolWaitPark, Yielded, YieldToWorker

from tests._resume_hook_fakes import build_ask_user_graph
from tests.graph.test_tool_wait_graph_park import (
    _ask_user_yield,
    _mk_executor_for_graph,
    _patch_run_agent_turn,
)
from tests.graph.test_toolcall_dispatch import _InMemoryStorage


# ---------------------------------------------------------------------------
# harness
# ---------------------------------------------------------------------------


def _trace(events: list[Any]) -> list[str]:
    out: list[str] = []
    for e in events:
        if isinstance(e, _GraphTransitionEvent):
            out.append(f"{e.phase}:{e.node_id}" + (f":{e.status}" if e.status else ""))
        elif isinstance(e, _GraphEndOutputEvent):
            out.append(f"end:{e.end_node_id}={e.text}")
        elif isinstance(e, _GraphErrorEvent):
            out.append(f"error:{e.code}@{e.node_id}")
    return out


async def _drive(it) -> tuple[list[Any], BaseException | None]:
    events: list[Any] = []
    try:
        async for ev in it:
            events.append(ev)
    except (YieldToWorker, ToolWaitPark) as exc:
        return events, exc
    return events, None


async def _agent_resolver(agent_id: str) -> Agent:
    raise KeyError(agent_id)


async def _llm_resolver(agent):  # pragma: no cover - never reached
    raise NotImplementedError


class _Run:
    """One graph thread driven through an invoke and any number of resumes, each by a fresh executor."""

    def __init__(self, graph: Graph) -> None:
        self.graph = graph
        self.threads = _InMemoryStorage(GraphThread)
        self.msgs = _InMemoryStorage(GraphNodeMessage)
        self.thread_id: str | None = None
        self.ex: GraphExecutor | None = None

    async def _executor(self, dispatcher) -> GraphExecutor:
        if self.thread_id is None:
            thread = await GraphExecutor.open_thread(graph=self.graph, thread_storage=self.threads)  # type: ignore[arg-type]
            self.thread_id = thread.id
        self.ex = GraphExecutor(
            graph=self.graph, agent_resolver=_agent_resolver, llm_resolver=_llm_resolver,  # type: ignore[arg-type]
            thread_storage=self.threads, message_storage=self.msgs,  # type: ignore[arg-type]
            graph_thread_id=self.thread_id, tool_dispatcher=dispatcher,
        )
        return self.ex

    async def invoke(self, dispatcher):
        ex = await self._executor(dispatcher)
        return await _drive(ex.invoke([]))

    async def resume(self, dispatcher, checkpoint, **kw):
        ex = await self._executor(dispatcher)
        return await _drive(ex.resume_from_checkpoint(checkpoint, **kw))

    async def ended(self) -> tuple[str, str | None, str | None]:
        t = await self.threads.get(self.thread_id)  # type: ignore[arg-type]
        return (str(t.status.value), t.ended_reason, t.ended_detail)

    def outputs(self) -> dict[str, Any]:
        assert self.ex is not None and self.ex._context is not None
        out: dict[str, Any] = {}
        for nid, v in sorted(self.ex._context.nodes.items()):
            if isinstance(v, list):
                out[nid] = [None if x is None else (x.text, x.error) for x in v]
            else:
                out[nid] = (v.text, v.error)
        return out


def _key(node, arguments) -> str:
    return f"{node.id}{arguments.get('i', '')}"


def _gate(gated: set[str]):
    """A dispatcher whose calls named in ``gated`` (node id plus fan-out index) park on an approval; the rest return ``out-<key>``."""

    async def dispatch(node, arguments, bypass_approval=False):
        k = _key(node, arguments)
        if k in gated and not bypass_approval:
            raise YieldToWorker(
                Yielded(tool_name="_approval", event_key=f"tool_approval:sid:{k}"),
                tool_call_id=f"tc-{k}",
            )
        return ToolResultPart(id="x", output=f"out-{k}")

    return dispatch


def _failing(failing: set[str]):
    async def dispatch(node, arguments, bypass_approval=False):
        k = _key(node, arguments)
        if k in failing:
            raise RuntimeError(f"boom-{k}")
        return ToolResultPart(id="x", output=f"out-{k}")

    return dispatch


def _tc(nid: str, **kw) -> _ToolCallNode:
    return _ToolCallNode(id=nid, tool_id=f"x__{nid}", arguments={}, **kw)


def _edges(*pairs: tuple[str, str]) -> list[_StaticEdge]:
    return [_StaticEdge(from_node=a, to_node=b) for a, b in pairs]


def _fanout_graph(on_failure: str = "fail_fast") -> Graph:
    return Graph(
        id=f"g-fo-{on_failure}", description="begin -> fo -> t[0..1] -> fi -> end",
        nodes=[
            _BeginNode(id="begin"),
            _FanOutNode(id="fo", specs=[FanOutSpec(kind="broadcast", target_node_id="t", count=2, on_failure=on_failure)]),
            _ToolCallNode(id="t", tool_id="x__t", arguments={"i": "{{ fanout_index }}"}),
            _FanInNode(id="fi", aggregate_template="{{ nodes.t | map(attribute='text') | join(',') }}"),
            _EndNode(id="end", output_template="{{ nodes.fi.text }}"),
        ],
        edges=_edges(("begin", "fo"), ("t", "fi"), ("fi", "end")),
    )


def _branch_graph() -> Graph:
    return Graph(
        id="g-branch", description="begin -> {a, b}; a -> enda; b -> endb",
        nodes=[
            _BeginNode(id="begin"), _tc("a"), _tc("b"),
            _EndNode(id="enda", output_template="{{ nodes.a.text }}"),
            _EndNode(id="endb", output_template="{{ nodes.b.text }}"),
        ],
        edges=_edges(("begin", "a"), ("begin", "b"), ("a", "enda"), ("b", "endb")),
    )


def _loop_graph(on_max: str | None) -> Graph:
    return Graph(
        id="g-loop", description="begin -> a <-> b; cap lands on f -> end",
        max_iterations=4, on_max_iterations=on_max,
        nodes=[
            _BeginNode(id="begin"), _tc("a"), _tc("b"), _tc("f"),
            _EndNode(id="end", output_template="{{ nodes.f.text }}"),
        ],
        edges=_edges(("begin", "a"), ("a", "b"), ("b", "a"), ("f", "end")),
    )


# ---------------------------------------------------------------------------
# runs with no park
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_linear_run() -> None:
    run = _Run(Graph(
        id="g-lin", description="begin -> a -> b -> end",
        nodes=[_BeginNode(id="begin"), _tc("a"), _tc("b"), _EndNode(id="end", output_template="{{ nodes.a.text }}+{{ nodes.b.text }}")],
        edges=_edges(("begin", "a"), ("a", "b"), ("b", "end")),
    ))
    events, parked = await run.invoke(_gate(set()))
    assert parked is None
    assert _trace(events) == [
        "enter:begin", "exit:begin:completed",
        "enter:a", "exit:a:completed",
        "enter:b", "exit:b:completed",
        "enter:end", "end:end=out-a+out-b", "exit:end:completed",
    ]
    assert await run.ended() == ("ended", "completed", None)


@pytest.mark.asyncio
async def test_branching_run() -> None:
    run = _Run(_branch_graph())
    events, parked = await run.invoke(_gate(set()))
    assert parked is None
    assert _trace(events) == [
        "enter:begin", "exit:begin:completed",
        "enter:a", "enter:b", "exit:a:completed", "exit:b:completed",
        "enter:enda", "enter:endb", "end:enda=out-a", "exit:enda:completed", "end:endb=out-b", "exit:endb:completed",
    ]
    assert await run.ended() == ("ended", "completed", None)


@pytest.mark.asyncio
async def test_fanout_run() -> None:
    run = _Run(_fanout_graph())
    events, parked = await run.invoke(_gate(set()))
    assert parked is None
    assert _trace(events) == [
        "enter:begin", "exit:begin:completed",
        "enter:fo", "exit:fo:completed",
        "enter:t[0]", "enter:t[1]", "exit:t[0]:completed", "exit:t[1]:completed",
        "enter:fi", "exit:fi:completed",
        "enter:end", "end:end=out-t0,out-t1", "exit:end:completed",
    ]
    assert run.outputs()["t"] == [("out-t0", None), ("out-t1", None)]
    assert await run.ended() == ("ended", "completed", None)


@pytest.mark.asyncio
async def test_fanout_collect_run() -> None:
    run = _Run(_fanout_graph("collect"))
    events, parked = await run.invoke(_failing({"t1"}))
    assert parked is None
    assert _trace(events) == [
        "enter:begin", "exit:begin:completed",
        "enter:fo", "exit:fo:completed",
        "enter:t[0]", "enter:t[1]", "exit:t[0]:completed", "exit:t[1]:failed",
        "enter:fi", "exit:fi:completed",
        "enter:end", "end:end=out-t0,", "exit:end:completed",
    ]
    assert run.outputs()["t"] == [("out-t0", None), ("", "boom-t1")]
    assert await run.ended() == ("ended", "completed", None)


@pytest.mark.asyncio
async def test_fanout_drain_then_fail_run() -> None:
    run = _Run(_fanout_graph("drain_then_fail"))
    events, parked = await run.invoke(_failing({"t1"}))
    assert parked is None
    assert _trace(events) == [
        "enter:begin", "exit:begin:completed",
        "enter:fo", "exit:fo:completed",
        "enter:t[0]", "enter:t[1]", "exit:t[0]:completed", "exit:t[1]:failed",
        "error:fanin_upstream_failed@t[1]",
    ]
    assert await run.ended() == ("ended", "failed", "fanin_upstream_failed")


@pytest.mark.asyncio
async def test_fanout_fail_fast_run() -> None:
    run = _Run(_fanout_graph())
    events, parked = await run.invoke(_failing({"t1"}))
    assert parked is None
    assert _trace(events) == [
        "enter:begin", "exit:begin:completed",
        "enter:fo", "exit:fo:completed",
        "enter:t[0]", "enter:t[1]", "exit:t[0]:completed", "exit:t[1]:failed",
        "error:tool_execution_failed@t[1]",
    ]
    assert await run.ended() == ("ended", "failed", "tool_execution_failed")


@pytest.mark.asyncio
async def test_loop_capped_and_rerouted_run() -> None:
    run = _Run(_loop_graph("f"))
    events, parked = await run.invoke(_gate(set()))
    assert parked is None
    assert _trace(events) == [
        "enter:begin", "exit:begin:completed",
        "enter:a", "exit:a:completed",
        "enter:b", "exit:b:completed",
        "enter:a", "exit:a:completed",
        "enter:f", "exit:f:completed",
        "enter:end", "end:end=out-f", "exit:end:completed",
    ]
    assert await run.ended() == ("ended", "completed", None)


@pytest.mark.asyncio
async def test_loop_capped_run() -> None:
    run = _Run(_loop_graph(None))
    events, parked = await run.invoke(_gate(set()))
    assert parked is None
    assert _trace(events) == [
        "enter:begin", "exit:begin:completed",
        "enter:a", "exit:a:completed",
        "enter:b", "exit:b:completed",
        "enter:a", "exit:a:completed",
        "error:max_iterations_exceeded@None",
    ]
    assert await run.ended() == ("ended", "max_iterations_exceeded", "max_iterations_exceeded")


# ---------------------------------------------------------------------------
# parks and their answers
# ---------------------------------------------------------------------------


def _linear_gated_graph() -> Graph:
    return Graph(
        id="g-lin-gate", description="begin -> t (gated) -> end",
        nodes=[_BeginNode(id="begin"), _tc("t"), _EndNode(id="end", output_template="{{ nodes.t.text }}")],
        edges=_edges(("begin", "t"), ("t", "end")),
    )


@pytest.mark.asyncio
async def test_linear_approval_park_and_approve() -> None:
    run = _Run(_linear_gated_graph())
    dispatch = _gate({"t"})
    events, parked = await run.invoke(dispatch)
    assert isinstance(parked, YieldToWorker)
    assert _trace(events) == ["enter:begin", "exit:begin:completed", "enter:t"]
    cp = parked.graph_checkpoint
    assert cp["ready_set"] == ["t"]
    assert [p["node_id"] for p in cp["pending_toolcalls"]] == ["t"]
    assert parked.yielded.event_key == "tool_approval:sid:t"
    assert await run.ended() == ("waiting", None, None)

    events, parked = await run.resume(dispatch, cp, resumed_tcid="tc-t", resumed_event_key="tool_approval:sid:t")
    assert parked is None
    assert _trace(events) == ["exit:t:completed", "enter:end", "end:end=out-t", "exit:end:completed"]
    assert await run.ended() == ("ended", "completed", None)


@pytest.mark.asyncio
async def test_linear_approval_park_and_reject() -> None:
    run = _Run(_linear_gated_graph())
    _events, parked = await run.invoke(_gate({"t"}))
    assert isinstance(parked, YieldToWorker)

    async def rejecting(node, arguments, bypass_approval=False):
        raise _ToolApprovalRejected("no thanks", tool_call_id="tc-t", kind="rejected")

    events, parked = await run.resume(rejecting, parked.graph_checkpoint, resumed_tcid="tc-t")
    assert parked is None
    assert _trace(events) == ["error:tool_approval_rejected@t", "exit:t:failed"]
    assert await run.ended() == ("ended", "failed", "tool_approval_rejected")
    assert run.outputs()["t"] == ("", "no thanks")


@pytest.mark.asyncio
async def test_linear_approval_park_and_failed_dispatch() -> None:
    run = _Run(_linear_gated_graph())
    _events, parked = await run.invoke(_gate({"t"}))
    events, parked = await run.resume(_failing({"t"}), parked.graph_checkpoint, resumed_tcid="tc-t")
    assert parked is None
    assert _trace(events) == ["error:tool_execution_failed@t", "exit:t:failed"]
    assert await run.ended() == ("ended", "failed", "tool_execution_failed")


@pytest.mark.asyncio
async def test_ask_user_toolcall_park_and_answer() -> None:
    run = _Run(build_ask_user_graph())

    async def first(node, arguments, bypass_approval=False):
        raise YieldToWorker(
            Yielded(tool_name="ask_user", event_key="ask_user:sid:tc-ask", resume_metadata={"prompt": "Approve access?", "tool_call_id": "tc-ask"}),
            tool_call_id="tc-ask",
        )

    events, parked = await run.invoke(first)
    assert isinstance(parked, YieldToWorker)
    assert _trace(events) == ["enter:begin", "exit:begin:completed", "enter:ask"]
    assert parked.graph_checkpoint["ready_set"] == ["ask"]

    events, parked = await run.resume(first, parked.graph_checkpoint, resumed_tcid="tc-ask", toolcall_payload={"response": "yes"})
    assert parked is None
    assert _trace(events) == [
        "exit:ask:completed", "enter:exit", 'end:exit={"response": "yes"}', "exit:exit:completed",
    ]
    assert await run.ended() == ("ended", "completed", None)


@pytest.mark.asyncio
async def test_agent_node_ask_user_park_and_answer(monkeypatch) -> None:
    graph = Graph(
        id="g-agent-ask", description="begin -> A (asks) -> end",
        nodes=[_BeginNode(id="begin"), _AgentNodeRef(id="A", agent_id="agent-a"), _EndNode(id="end", output_template="done")],
        edges=_edges(("begin", "A"), ("A", "end")),
    )
    ex = await _mk_executor_for_graph(graph)
    _patch_run_agent_turn(monkeypatch, {"agent-a": _ask_user_yield("A", "tc-a")})
    events, parked = await _drive(ex.invoke([]))
    assert isinstance(parked, YieldToWorker)
    assert _trace(events) == ["enter:begin", "exit:begin:completed", "enter:A"]
    cp = parked.graph_checkpoint
    assert cp["ready_set"] == ["A"]
    assert [p["node_id"] for p in cp["pending_agent_yields"]] == ["A"]

    events, parked = await _drive(ex.resume_from_checkpoint(
        cp, resumed_tcid="tc-a",
        agent_tool_result=Message(role="tool", parts=[ToolResultPart(id="tc-a", output="blue")]),
    ))
    assert parked is None
    assert _trace(events) == ["exit:A:completed", "enter:end", "end:end=done", "exit:end:completed"]
    assert ex._last_ended_reason == "completed"


@pytest.mark.asyncio
async def test_two_parked_siblings_answered_one_at_a_time() -> None:
    run = _Run(_branch_graph())
    dispatch = _gate({"a", "b"})
    events, parked = await run.invoke(dispatch)
    assert isinstance(parked, YieldToWorker)
    assert _trace(events) == ["enter:begin", "exit:begin:completed", "enter:a", "enter:b"]
    assert parked.graph_checkpoint["ready_set"] == ["a", "b"]
    assert sorted(parked.yielded.event_keys) == ["tool_approval:sid:a", "tool_approval:sid:b"]

    events, parked = await run.resume(dispatch, parked.graph_checkpoint, resumed_tcid="tc-a", resumed_event_key="tool_approval:sid:a")
    assert isinstance(parked, YieldToWorker)
    assert _trace(events) == ["exit:a:completed"]
    assert parked.graph_checkpoint["ready_set"] == ["b", "enda"]
    assert parked.yielded.event_keys == ["tool_approval:sid:b"]

    events, parked = await run.resume(dispatch, parked.graph_checkpoint, resumed_tcid="tc-b", resumed_event_key="tool_approval:sid:b")
    assert parked is None
    assert _trace(events) == [
        "exit:b:completed",
        "enter:enda", "enter:endb", "end:enda=out-a", "exit:enda:completed", "end:endb=out-b", "exit:endb:completed",
    ]
    assert await run.ended() == ("ended", "completed", None)


@pytest.mark.asyncio
async def test_two_parked_siblings_drained_by_one_keyless_resume() -> None:
    run = _Run(_branch_graph())
    dispatch = _gate({"a", "b"})
    _events, parked = await run.invoke(dispatch)
    events, parked = await run.resume(dispatch, parked.graph_checkpoint)
    assert parked is None
    assert _trace(events) == [
        "exit:a:completed", "exit:b:completed",
        "enter:enda", "enter:endb", "end:enda=out-a", "exit:enda:completed", "end:endb=out-b", "exit:endb:completed",
    ]
    assert await run.ended() == ("ended", "completed", None)


@pytest.mark.asyncio
async def test_parked_sibling_of_a_failed_node_ends_failed_after_its_answer() -> None:
    run = _Run(_branch_graph())

    async def dispatch(node, arguments, bypass_approval=False):
        if node.id == "a":
            raise RuntimeError("boom-a")
        return await _gate({"b"})(node, arguments, bypass_approval)

    events, parked = await run.invoke(dispatch)
    assert isinstance(parked, YieldToWorker)
    assert _trace(events) == ["enter:begin", "exit:begin:completed", "enter:a", "enter:b", "exit:a:failed"]
    cp = parked.graph_checkpoint
    assert cp["ready_set"] == ["b"]
    assert cp["pending_ended_reason"] == "failed"
    assert cp["pending_ended_detail"] == "tool_execution_failed"

    events, parked = await run.resume(dispatch, cp, resumed_tcid="tc-b")
    assert parked is None
    assert _trace(events) == ["exit:b:completed"]
    assert await run.ended() == ("ended", "failed", "tool_execution_failed")


@pytest.mark.asyncio
async def test_loop_node_parks_and_resumes_to_the_cap() -> None:
    run = _Run(_loop_graph(None))
    calls = {"a": 0}

    async def dispatch(node, arguments, bypass_approval=False):
        if node.id == "a" and not bypass_approval:
            calls["a"] += 1
            if calls["a"] == 2:
                raise YieldToWorker(Yielded(tool_name="_approval", event_key="tool_approval:sid:a"), tool_call_id="tc-a")
        return ToolResultPart(id="x", output=f"out-{node.id}")

    events, parked = await run.invoke(dispatch)
    assert isinstance(parked, YieldToWorker)
    assert _trace(events) == [
        "enter:begin", "exit:begin:completed", "enter:a", "exit:a:completed", "enter:b", "exit:b:completed", "enter:a",
    ]
    assert parked.graph_checkpoint["ready_set"] == ["a"]

    events, parked = await run.resume(dispatch, parked.graph_checkpoint, resumed_tcid="tc-a")
    assert parked is None
    assert _trace(events) == ["exit:a:completed", "error:max_iterations_exceeded@None"]
    assert await run.ended() == ("ended", "max_iterations_exceeded", "max_iterations_exceeded")
    assert calls["a"] == 2


@pytest.mark.asyncio
async def test_a_checkpoint_without_the_cap_flag_restores_it_false() -> None:
    run = _Run(_linear_gated_graph())
    _events, parked = await run.invoke(_gate({"t"}))
    cp = dict(parked.graph_checkpoint)
    cp.pop("max_iter_routed", None)
    ex = await run._executor(_gate(set()))
    ex.restore_state(cp)
    assert ex._max_iter_routed is False


# A park checkpoint exactly as the code before Task 47 wrote it (no ``max_iter_routed``): it must still resume.
_PRE_T47_LINEAR_CHECKPOINT: dict[str, Any] = {
    "context": {
        "ctx": {"artifact_dir": None, "graph_id": None, "identity": None, "now": "2026-10-31T20:16:08.955174+00:00", "principal": None,
                "session_id": None, "surface": "memory", "workspace_id": None},
        "initial_input": [], "iteration": 1,
        "nodes": {"begin": {"ended_detail": None, "error": None, "history": [], "iteration": 0, "parsed": None, "text": ""}},
    },
    "fanout_drain_state": {}, "fanout_instances": {}, "fanout_target_expected_count": {}, "instance_to_spec": {},
    "node_states": {
        "begin": {"error": None, "last_run_at": "2026-10-31T20:16:08.955352Z", "last_run_iteration": 0, "status": "ended"},
        "end": {"error": None, "last_run_at": None, "last_run_iteration": None, "status": "pending"},
        "t": {"error": None, "last_run_at": "2026-10-31T20:16:08.955375Z", "last_run_iteration": 1, "status": "running"},
    },
    "pending_agent_yields": [],
    "pending_dispatch": [{"kind": "_approval", "node_id": "t", "resume_metadata": {"original_call": {"arguments": {}, "id": "tc-t", "name": "x__t"}},
                          "tool_call_id": "tc-t"}],
    "pending_ended_detail": None, "pending_ended_reason": None, "pending_tool_waits": [],
    "pending_toolcalls": [{"arguments": {}, "node_id": "t", "parked_event_key": "tool_approval:sid:t", "resume_metadata": {},
                           "row_call_id": "7a864f29-f8c9-4afc-abdc-56422ed11837", "scoped_tool_call_id": None, "tool_call_id": "tc-t",
                           "tool_name": "_approval"}],
    "ready_set": ["t"],
}


@pytest.mark.asyncio
async def test_a_checkpoint_written_before_task_47_resumes() -> None:
    run = _Run(_linear_gated_graph())
    events, parked = await run.resume(_gate(set()), _PRE_T47_LINEAR_CHECKPOINT, resumed_tcid="tc-t", resumed_event_key="tool_approval:sid:t")
    assert parked is None
    assert _trace(events) == ["exit:t:completed", "enter:end", "end:end=out-t", "exit:end:completed"]
    assert await run.ended() == ("ended", "completed", None)
    assert run.ex is not None and run.ex._max_iter_routed is False
