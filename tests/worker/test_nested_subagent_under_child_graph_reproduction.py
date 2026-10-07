"""A nested subagent that parks under an invoked CHILD graph is never continued when its answer arrives (ticket 01a10b54-6264).

Reproduction only: the fix is held for the independent verifiers (it shares one nested-walk helper with 01a11057).

Shape: agent A calls ``invoke_graph(G1)``; G1's agent node calls ``invoke_agent(B)``; B parks on a leaf (an ``ask_user`` or an
``_approval`` gate). The session parks with ONE frame, the ``GraphFrame`` of G1, and G1's checkpoint carries the node's pending
entry WITH its nested subagent frame and leaf (``pending_agent_yields[0].frames == [agent]``). When the operator answers, the
continuation walk gives the leaf's answer to ``GraphFrame.resume_leaf``, which never walks that entry's frames:

* B's turn is never continued (``services.resume_subagent`` is not called, so B never produces its answer);
* the node's resumed turn is handed the leaf's answer under the LEAF id (``leaf-tc``) instead of the ``invoke_agent`` call's id
  (``invoke-tc``): a ``tool_use`` with no matching ``tool_result``, an invalid history for the provider;
* an ``_approval`` leaf is resolved as an empty success, so the call the operator approved never runs.

Driven through the REAL ``resume_continuation`` walk, the real ``GraphFrame`` and a real child executor; only
``services.resume_subagent`` is a spy (it answers "B's final answer"), so a continued B is observable.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import pytest

import primer.toolset.misc  # noqa: F401  registers the ask_user resume hook
from primer.graph.invoke_graph import GraphInvocationServices, run_invoke_graph
from primer.model.chat import Done, Message, TextDelta, ToolCallPart, ToolResultPart
from primer.model.yield_ import Yielded, YieldToWorker
from primer.worker.continuation import resume_continuation
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

xfail_the_walk = pytest.mark.xfail(
    strict=True, raises=AssertionError,
    reason="01a10b54-6264: GraphFrame.resume_leaf never walks the nested subagent frames of the node's pending entry, so "
    "B's turn is not continued and the node's resumed turn gets the leaf's answer under the wrong id",
)


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
    llm: _NodeLLM


async def _park_then_answer(leaf_kind: str) -> _Run:
    leaf, answer = LEAVES[leaf_kind]
    graph = _graph()
    llm = _NodeLLM(leaf)
    executors = [await _mk_executor(graph, llm), await _mk_executor(graph, llm)]

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

    async def spy_resume_subagent(**kw: Any) -> str:
        continued.append(kw["invoke_tool_call_id"])
        return B_ANSWER

    services.resume_subagent = spy_resume_subagent  # type: ignore[method-assign]
    outcome = await resume_continuation(parked.frames, parked.yielded, answer, services)
    pending = (parked.frames[0].checkpoint.get("pending_agent_yields") or [{}])[0]
    return _Run(parked=parked, pending=pending, outcome=outcome, continued=continued, llm=llm)


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


@xfail_the_walk
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
    assert not problems, "\n".join(problems)
