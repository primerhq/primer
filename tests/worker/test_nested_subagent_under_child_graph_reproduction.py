"""A nested subagent parked under an invoked child graph continues before its node resumes (ticket 01a10b54-6264).

Shape: agent A calls ``invoke_graph(G1)``; G1's agent node calls ``invoke_agent(B)``; B parks on a leaf (an ``ask_user`` or an
``_approval`` gate). The session parks with ONE frame, the ``GraphFrame`` of G1, and G1's checkpoint carries the node's pending
entry WITH its nested subagent frame and leaf (``pending_agent_yields[0].frames == [agent]``). When the operator answers, the
continuation walk gives the leaf's answer to ``GraphFrame.resume_leaf``.

The continuation walk first resolves B's leaf through the existing leaf
semantics, continues B exactly once, and only then delivers B's completed
``invoke_agent`` result into the child graph node.

Driven through the REAL ``resume_continuation`` walk, the real ``GraphFrame`` and a real child executor; only
``services.resume_subagent`` is a spy (it answers "B's final answer"), so a continued B is observable.
"""

from __future__ import annotations

import json
from copy import deepcopy
from types import SimpleNamespace
from typing import Any

import pytest

import primer.toolset.misc  # noqa: F401  registers the ask_user resume hook
from primer.graph.invoke_graph import GraphInvocationServices, run_invoke_graph
from primer.graph._node_identity import current_graph_node_id
from primer.model.chat import Done, Message, TextDelta, ToolCallPart, ToolResultPart
from primer.model.graph import Graph, _AgentNodeRef, _BeginNode, _EndNode, _StaticEdge
from primer.model.yield_ import Yielded, YieldToWorker
from primer.worker.continuation import resume_continuation
from primer.worker.frames import AgentFrame, AgentResumeContext
from primer.worker.session_resume_coordinator import build_invocation_services
from tests._resume_hook_fakes import AgentNodeHookPool as _Pool, IdentityToolsetRegistry as _Registry
from tests.graph.test_graph_node_subagent_yield import _graph, _INVOKE_TCID, _LEAF_TCID, _mk_executor, _subagent_frame

SESSION_ID = "sess-agent"
B_ANSWER = "B's final answer"

LEAVES = {
    "ask_user": (
        Yielded(tool_name="ask_user", event_key=f"ask_user:s:{_LEAF_TCID}", resume_metadata={"prompt": "color?"}),
        {"response": "blue"},
    ),
    "approval": (
        Yielded(
            tool_name="_approval", event_key=f"tool_approval:s:{_LEAF_TCID}",
            resume_metadata={"original_call": {"id": _LEAF_TCID, "name": "danger__wipe", "arguments": {}}},
        ),
        {"decision": "approved"},
    ),
}

class _NodeLLM:
    """The child graph's agent node. First stream: its turn parks (it called ``invoke_agent(B)`` and B yielded ``leaf``).
    Later streams: it finishes, and the messages it was handed are kept so what the resumed turn saw can be read."""

    def __init__(self, leaf: Yielded) -> None:
        self.leaf = leaf
        self.calls = 0
        self.prompts: list[list[Message]] = []

    async def list_models(self):
        return ["m"]

    def stream(self, **kw: Any):
        self.calls += 1
        n = self.calls
        self.prompts.append(list(kw.get("messages") or []))

        async def g():
            if n == 1:
                parked = YieldToWorker(
                    self.leaf, tool_call_id=_LEAF_TCID,
                    llm_messages=[Message(role="assistant", parts=[
                        ToolCallPart(id=_INVOKE_TCID, name="system__invoke_agent", arguments={"agent_id": "sub", "input": "q"}),
                    ])],
                )
                parked.frames = [_subagent_frame()]
                raise parked
            yield TextDelta(text="node finished", index=0)
            yield Done(stop_reason="stop", raw_reason="stop")

        return g()


class _Run(SimpleNamespace):
    parked: YieldToWorker
    pending: dict
    outcome: Any
    continued: list[str]
    approved: list[str]
    llm: _NodeLLM
    services: Any


async def _park_then_answer(leaf_kind: str, *, reyield_once: bool = False) -> _Run:
    leaf, answer = LEAVES[leaf_kind]
    graph = _graph()
    llm = _NodeLLM(leaf)
    executors = [await _mk_executor(graph, llm) for _ in range(3)]

    async def resolve_graph(_gid):
        return graph

    async def build_child_executor(*, graph, gsid):
        return executors.pop(0)

    graph_services = GraphInvocationServices(
        resolve_graph=resolve_graph, build_child_executor=build_child_executor,
        session_id=SESSION_ID, workspace_id="ws-1", graph_session_id="gs-agent",
    )
    with pytest.raises(YieldToWorker) as raised:
        await run_invoke_graph(graph_id="child", graph_input="go", services=graph_services, tool_call_id="agent-tc")
    parked = raised.value

    pool = _Pool(_provider_registry=_Registry(), _storage=None, _approval_resolver=None)
    services = build_invocation_services(
        pool, SimpleNamespace(id=SESSION_ID), None, None, SimpleNamespace(_graph_services=graph_services),
    )
    continued: list[str] = []
    approved: list[str] = []

    class _Manager:
        async def execute(self, call, *, bypass_approval=False):
            assert bypass_approval is True
            approved.append(call.id)
            return ToolResultPart(id=call.id, output="approved", error=False)

    async def build_manager(_context, *, agent_id=None):
        assert agent_id == "sub"
        return _Manager()

    async def spy_resume_subagent(**kw: Any) -> str:
        continued.append(kw["invoke_tool_call_id"])
        if reyield_once and len(continued) == 1:
            again = YieldToWorker(
                Yielded(
                    tool_name="ask_user",
                    event_key="ask_user:s:leaf-again",
                    resume_metadata={"prompt": "again?"},
                ),
                tool_call_id="leaf-again",
            )
            again.frames = [_subagent_frame()]
            raise again
        return B_ANSWER

    services.resume_subagent = spy_resume_subagent  # type: ignore[method-assign]
    services.build_subagent_toolmanager = build_manager  # type: ignore[method-assign]
    outcome = await resume_continuation(parked.frames, parked.yielded, answer, services)
    pending = (parked.frames[0].checkpoint.get("pending_agent_yields") or [{}])[0]
    return _Run(
        parked=parked, pending=pending, outcome=outcome,
        continued=continued, approved=approved, llm=llm, services=services,
    )


def _tool_results_the_resumed_node_saw(run: _Run) -> list[ToolResultPart]:
    assert run.llm.calls >= 2, "the node's turn was never resumed"
    return [p for m in run.llm.prompts[-1] for p in m.parts if isinstance(p, ToolResultPart)]


@pytest.mark.asyncio
@pytest.mark.parametrize("leaf_kind", list(LEAVES))
async def test_scenario_a_nested_subagent_parked_under_a_child_graph_node(leaf_kind: str) -> None:
    run = await _park_then_answer(leaf_kind)

    assert [type(f).__name__ for f in run.parked.frames] == ["GraphFrame"], "the session parks on G1's frame alone"
    assert run.pending["tool_name"] == LEAVES[leaf_kind][0].tool_name, "the checkpoint keeps B's leaf as the node's pending yield"
    assert run.pending["tool_call_id"] == _LEAF_TCID and run.pending["leaf"], "the checkpoint keeps the node's leaf"
    assert [f.get("kind") for f in run.pending["frames"]] == ["agent"], "the checkpoint keeps B's frame under the node"
    assert type(run.outcome).__name__ == "Deliver", "the walk ran to its end and delivered something to the invoking agent"


@pytest.mark.asyncio
@pytest.mark.parametrize("leaf_kind", list(LEAVES))
async def test_the_answer_continues_the_nested_subagent_and_pairs_with_the_call_that_invoked_it(leaf_kind: str) -> None:
    run = await _park_then_answer(leaf_kind)

    results = _tool_results_the_resumed_node_saw(run)
    problems = []
    if run.continued != [_INVOKE_TCID]:
        problems.append(f"B's turn was continued {len(run.continued)} times ({run.continued}), expected once for {_INVOKE_TCID!r}")
    if _LEAF_TCID in [r.id for r in results]:
        problems.append(f"the node's resumed turn was handed a tool_result under the LEAF id {_LEAF_TCID!r} (no tool_use has it)")
    paired = [r for r in results if r.id == _INVOKE_TCID]
    if len(paired) != 1:
        problems.append(f"the invoke_agent call {_INVOKE_TCID!r} got {len(paired)} tool_results, expected 1")
    elif B_ANSWER not in json.dumps(paired[0].output):
        problems.append(f"the invoke_agent result does not carry B's answer: {paired[0].output!r}")
    expected_approved = [_LEAF_TCID] if leaf_kind == "approval" else []
    if run.approved != expected_approved:
        problems.append(f"approved leaf executions were {run.approved}, expected {expected_approved}")
    assert not problems, "\n".join(problems)


@pytest.mark.asyncio
async def test_nested_subagent_reyield_advances_the_child_checkpoint_without_double_continuation() -> None:
    run = await _park_then_answer("ask_user", reyield_once=True)

    assert type(run.outcome).__name__ == "Repark"
    assert run.continued == [_INVOKE_TCID]
    assert run.llm.calls == 1, "the child graph node must not resume before B completes"
    assert [type(frame).__name__ for frame in run.outcome.frames] == ["GraphFrame"]
    advanced = run.outcome.frames[0]
    entry = advanced.checkpoint["pending_agent_yields"][0]
    assert entry["event_key"] == "ask_user:s:leaf-again"
    assert entry["tool_call_id"] == "leaf-again"
    assert [frame["kind"] for frame in entry["frames"]] == ["agent"]

    completed = await resume_continuation(
        run.outcome.frames,
        run.outcome.leaf,
        {"response": "green"},
        run.services,
    )
    assert type(completed).__name__ == "Deliver"
    assert run.continued == [_INVOKE_TCID, _INVOKE_TCID]
    results = _tool_results_the_resumed_node_saw(run)
    assert [result.id for result in results] == [_INVOKE_TCID]


_SHARED_TCID = "shared-leaf"
_SHARED_KEY = f"ask_user:s:{_SHARED_TCID}"
_GATE_A = "a" * 32
_GATE_B = "b" * 32


def _sibling_frame(node_id: str) -> AgentFrame:
    return AgentFrame(
        agent_id=f"sub-{node_id}",
        llm_messages=[{"role": "assistant", "parts": []}],
        tool_call_id=f"invoke-{node_id}",
        depth=0,
        context=AgentResumeContext(
            session_id="s",
            workspace_id="w",
            chat_id=None,
            principal="p",
            tools=["misc__ask_user"],
        ),
    )


def _sibling_graph() -> Graph:
    return Graph(
        id="g-siblings",
        description="two colliding nested agent yields",
        nodes=[
            _BeginNode(id="begin"),
            _AgentNodeRef(id="A", agent_id="x"),
            _AgentNodeRef(id="B", agent_id="x"),
            _EndNode(id="end-A"),
            _EndNode(id="end-B"),
        ],
        edges=[
            _StaticEdge(from_node="begin", to_node="A"),
            _StaticEdge(from_node="begin", to_node="B"),
            _StaticEdge(from_node="A", to_node="end-A"),
            _StaticEdge(from_node="B", to_node="end-B"),
        ],
    )


class _SiblingLLM:
    """Two real child nodes that collide on raw leaf id/key."""

    def __init__(self) -> None:
        self.calls = {"A": 0, "B": 0}
        self.prompts: dict[str, list[list[Message]]] = {"A": [], "B": []}

    async def list_models(self):
        return ["m"]

    def stream(self, **kw: Any):
        node_id = current_graph_node_id()
        assert node_id in ("A", "B")
        self.calls[node_id] += 1
        call_no = self.calls[node_id]
        self.prompts[node_id].append(list(kw.get("messages") or []))

        async def g():
            if call_no == 1:
                gate_id = _GATE_A if node_id == "A" else _GATE_B
                parked = YieldToWorker(
                    Yielded(
                        tool_name="ask_user",
                        event_key=_SHARED_KEY,
                        resume_metadata={
                            "prompt": f"question-{node_id}",
                            "gate_id": gate_id,
                        },
                    ),
                    tool_call_id=_SHARED_TCID,
                    llm_messages=[
                        Message(
                            role="assistant",
                            parts=[
                                ToolCallPart(
                                    id=f"invoke-{node_id}",
                                    name="system__invoke_agent",
                                    arguments={"agent_id": f"sub-{node_id}"},
                                )
                            ],
                        )
                    ],
                )
                parked.frames = [_sibling_frame(node_id)]
                raise parked
            yield TextDelta(text=f"node {node_id} finished", index=0)
            yield Done(stop_reason="stop", raw_reason="stop")

        return g()


async def _answer_colliding_sibling(*, reyield_b: bool) -> SimpleNamespace:
    graph = _sibling_graph()
    llm = _SiblingLLM()
    executors = [await _mk_executor(graph, llm) for _ in range(2)]

    async def resolve_graph(_gid):
        return graph

    async def build_child_executor(*, graph, gsid):
        return executors.pop(0)

    graph_services = GraphInvocationServices(
        resolve_graph=resolve_graph,
        build_child_executor=build_child_executor,
        session_id=SESSION_ID,
        workspace_id="ws-1",
        graph_session_id="gs-siblings",
    )
    with pytest.raises(YieldToWorker) as raised:
        await run_invoke_graph(
            graph_id="child",
            graph_input="go",
            services=graph_services,
            tool_call_id="agent-tc",
        )
    parked = raised.value
    before = deepcopy(parked.frames[0].checkpoint["pending_agent_yields"])
    assert [
        (entry["node_id"], entry["resume_metadata"]["gate_id"])
        for entry in before
    ] == [
        ("A", _GATE_A),
        ("B", _GATE_B),
    ]

    pool = _Pool(
        _provider_registry=_Registry(),
        _storage=None,
        _approval_resolver=None,
    )
    services = build_invocation_services(
        pool,
        SimpleNamespace(id=SESSION_ID),
        None,
        None,
        SimpleNamespace(_graph_services=graph_services),
    )
    continued: list[str] = []

    async def spy_resume_subagent(**kw: Any) -> str:
        continued.append(kw["invoke_tool_call_id"])
        assert kw["agent_id"] == "sub-B"
        if reyield_b:
            again = YieldToWorker(
                Yielded(
                    tool_name="ask_user",
                    event_key="ask_user:s:B-again",
                    resume_metadata={"prompt": "B again?", "gate_id": "c" * 32},
                ),
                tool_call_id="B-again",
            )
            again.frames = [_sibling_frame("B")]
            raise again
        return "B completed"

    services.resume_subagent = spy_resume_subagent  # type: ignore[method-assign]
    outcome = await resume_continuation(
        parked.frames,
        parked.yielded,
        {"response": "answer B"},
        services,
        fired_key=_SHARED_KEY,
        gate_id=_GATE_B,
    )
    return SimpleNamespace(
        before=before,
        outcome=outcome,
        continued=continued,
        llm=llm,
    )


def _entry_by_node(checkpoint: dict[str, Any], node_id: str) -> dict[str, Any]:
    return next(
        entry
        for entry in checkpoint["pending_agent_yields"]
        if entry["node_id"] == node_id
    )


@pytest.mark.asyncio
async def test_colliding_sibling_completion_resumes_only_the_exact_nested_entry() -> None:
    run = await _answer_colliding_sibling(reyield_b=False)

    assert type(run.outcome).__name__ == "Repark"
    assert run.continued == ["invoke-B"]
    assert run.llm.calls == {"A": 1, "B": 2}
    checkpoint = run.outcome.frames[0].checkpoint
    assert _entry_by_node(checkpoint, "A") == _entry_by_node(
        {"pending_agent_yields": run.before}, "A"
    )
    assert [entry["node_id"] for entry in checkpoint["pending_agent_yields"]] == ["A"]
    results = [
        part
        for message in run.llm.prompts["B"][-1]
        for part in message.parts
        if isinstance(part, ToolResultPart)
    ]
    assert [part.id for part in results] == ["invoke-B"]


@pytest.mark.asyncio
async def test_colliding_sibling_reyield_advances_only_the_exact_nested_entry() -> None:
    run = await _answer_colliding_sibling(reyield_b=True)

    assert type(run.outcome).__name__ == "Repark"
    assert run.continued == ["invoke-B"]
    assert run.llm.calls == {"A": 1, "B": 1}
    checkpoint = run.outcome.frames[0].checkpoint
    before_a = _entry_by_node({"pending_agent_yields": run.before}, "A")
    before_b = _entry_by_node({"pending_agent_yields": run.before}, "B")
    assert _entry_by_node(checkpoint, "A") == before_a
    after_b = _entry_by_node(checkpoint, "B")
    assert after_b != before_b
    assert after_b["event_key"] == "ask_user:s:B-again"
    assert after_b["tool_call_id"] == "B-again"
    assert after_b["leaf"]["event_key"] == "ask_user:s:B-again"
