"""Graph third-list / mixed-park order tests (Phase 3 stage 7a, 01a0518b
boundary d) - the leader-required proof that ``resume_from_checkpoint``
drains a mixed (human-gate + tool_wait) or partial-wake (two independent
fan-out tool_wait batches) pending set correctly, in EITHER arrival order.

Three orders, per the leader's own ruling (per-node wake keys make the
pure graph tool_wait park inherently multi-event, so "two order tests"
became three):

1. ``test_tools_finish_first_gate_survives_the_reprk`` - mixed superstep,
   the tool_wait batch goes terminal BEFORE the human gate is answered.
2. ``test_gate_answers_first_tool_wait_survives_the_reprk`` - same mixed
   superstep, the human gate is answered BEFORE the tool_wait batch goes
   terminal.
3. ``test_node_a_before_node_b_partial_wake`` - PURE park (no human gate
   at all), two independent fan-out-sibling nodes each raise their own
   ``ToolWaitPark`` in the same superstep; node A's batch goes terminal
   while node B's is still mid-flight (partial wake).

Drives ``run_agent_turn`` via a direct monkeypatch keyed on the
dispatched node's ``agent.id`` rather than standing up a full
``_dispatch_as_claims``/tool_manager simulation - the scope under test is
the EXECUTOR's own pending-list bookkeeping (``_pending_tool_waits`` /
``_pending_agent_yields`` / drain-until-empty / re-park choice), not
``_dispatch_as_claims`` itself (already covered at the agent-loop level).
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest

from primer.graph.base import _PendingAgentYield, _PendingToolWait
from primer.graph.executor import GraphExecutor
from primer.model.agent import Agent, AgentModel
from primer.model.chat import Message, StreamEvent, ToolResultPart
from primer.model.graph import (
    Graph, GraphNodeMessage, GraphThread, NodeRuntimeStatus,
    _AgentNodeRef, _BeginNode, _EndNode, _StaticEdge,
)
from primer.model.model_profile import ModelProfileConfig
from primer.model.yield_ import ToolWaitPark, Yielded, YieldToWorker
from primer.model_profile import ResolvedModel

from tests.graph.test_toolcall_dispatch import _InMemoryStorage


def _model() -> ResolvedModel:
    return ResolvedModel(
        profile_id="test-profile", provider_id="test-provider", model_name="m",
        context_length=128_000, config=ModelProfileConfig(),
    )


class _UnusedLLM:
    """Never actually invoked - run_agent_turn itself is monkeypatched for
    every node in these tests, so the real LLM/tool loop never runs."""

    async def list_models(self):
        return ["m"]

    def stream(self, **kw) -> AsyncIterator[StreamEvent]:
        raise AssertionError("real LLM.stream() should never be reached")


def _parallel_graph() -> Graph:
    """begin -> {A, B}, A -> C, B -> D: both agent nodes become ready in
    the SAME superstep (no edge between them), so their respective parks
    land in one drain cycle - the mixed/partial-wake shape every test
    here needs. DIVERGENT downstreams (7a gate review, verdict item 2) -
    a prior shared-End topology (A -> exit, B -> exit) MASKED the
    successor-drop bug: B's own edge to the SAME node as A's meant the
    graph still reached "exit" even when A's own edge-walk was silently
    skipped, since firing a plain (non-FanIn) node doesn't care which
    upstream triggered it. Separate terminals make A's own successor
    observable independently of B's.
    """
    return Graph(
        id="g", description="begin -> {A,B}; A -> C; B -> D",
        nodes=[
            _BeginNode(id="begin"),
            _AgentNodeRef(id="A", agent_id="agent-a"),
            _AgentNodeRef(id="B", agent_id="agent-b"),
            _EndNode(id="C"),
            _EndNode(id="D"),
        ],
        edges=[
            _StaticEdge(from_node="begin", to_node="A"),
            _StaticEdge(from_node="begin", to_node="B"),
            _StaticEdge(from_node="A", to_node="C"),
            _StaticEdge(from_node="B", to_node="D"),
        ],
    )


async def _mk_parallel_executor() -> GraphExecutor:
    async def agent_resolver(agent_id: str) -> Agent:
        return Agent(
            id=agent_id, description=agent_id, model=AgentModel(profile_id="p--m"),
        )

    async def llm_resolver(_agent):
        return (_UnusedLLM(), _model())

    graph = _parallel_graph()
    ts: _InMemoryStorage[GraphThread] = _InMemoryStorage(GraphThread)
    ms: _InMemoryStorage[GraphNodeMessage] = _InMemoryStorage(GraphNodeMessage)
    thread = await GraphExecutor.open_thread(graph=graph, thread_storage=ts)  # type: ignore[arg-type]
    ex = GraphExecutor(
        graph=graph, agent_resolver=agent_resolver,
        llm_resolver=llm_resolver,  # type: ignore[arg-type]
        thread_storage=ts, message_storage=ms,  # type: ignore[arg-type]
        graph_thread_id=thread.id,
    )
    ex._tool_calls_as_claims_enabled = True
    return ex


def _tool_wait_park(node_id: str, seq: str) -> ToolWaitPark:
    scoped_id = f"{node_id}:tool:0:{seq}"
    return ToolWaitPark(
        outstanding_task_ids=[scoped_id],
        event_key=f"tool_wait:{scoped_id}",
        llm_messages=[{
            "role": "assistant",
            "parts": [{"type": "text", "text": f"{node_id} dispatching"}],
        }],
    )


def _ask_user_yield(node_id: str, tcid: str) -> YieldToWorker:
    return YieldToWorker(
        Yielded(
            tool_name="ask_user", event_key=f"ask_user:{node_id}:{tcid}",
            resume_metadata={"prompt": "color?"},
        ),
        tool_call_id=tcid,
        llm_messages=[{
            "role": "assistant",
            "parts": [{"type": "text", "text": f"{node_id} asking"}],
        }],
    )


def _patch_run_agent_turn(monkeypatch, behavior: dict) -> None:
    """Monkeypatch primer.graph._agent_node.run_agent_turn so a call
    dispatched for agent id ``k`` raises ``behavior[k]`` (a ToolWaitPark
    or YieldToWorker instance) instead of running a real LLM/tool loop.
    Consumed exactly once per key (pops it) so a SECOND dispatch for the
    same agent (a resumed node's continuation) falls through to a plain
    completion instead of looping the same park forever.
    """
    import primer.graph._agent_node as agent_node_mod

    async def _spy(**kwargs):
        agent = kwargs["agent"]
        exc = behavior.get(agent.id)
        if exc is not None:
            del behavior[agent.id]
            raise exc
            yield  # pragma: no cover - unreachable, keeps this a generator
        return
        yield  # pragma: no cover - unreachable, keeps this a generator

    monkeypatch.setattr(agent_node_mod, "run_agent_turn", _spy)


@pytest.mark.asyncio
async def test_tools_finish_first_gate_survives_the_reprk(monkeypatch) -> None:
    """Mixed superstep (A -> tool_wait, B -> ask_user). Resolve A's batch
    FIRST (resolved_tool_wait={"A": [...]}); B's gate must survive the
    re-park - the drain must not silently drop or complete it."""
    ex = await _mk_parallel_executor()
    _patch_run_agent_turn(monkeypatch, {
        "agent-a": _tool_wait_park("A", "1"),
        "agent-b": _ask_user_yield("B", "tc-b"),
    })

    with pytest.raises(YieldToWorker) as excinfo:
        async for _ev in ex.invoke([]):
            pass
    first_park = excinfo.value
    assert first_park.graph_checkpoint is not None
    assert len(ex._pending_tool_waits) == 1
    assert ex._pending_tool_waits[0].node_id == "A"
    assert len(ex._pending_agent_yields) == 1
    assert ex._pending_agent_yields[0].node_id == "B"

    # A's batch goes terminal; B's gate has not been answered yet.
    # resumed_tcid must NOT match B's real tcid ("tc-b") - passing the
    # legacy None would blanket-drain EVERY pending toolcall/agent_yield
    # (see resume_from_checkpoint's own docstring), which would spuriously
    # resume B too and defeat the point of this test.
    result_a = ToolResultPart(id="A:tool:0:1", output="result A", error=False)
    with pytest.raises(YieldToWorker) as excinfo2:
        async for _ev in ex.resume_from_checkpoint(
            first_park.graph_checkpoint,
            resumed_tcid="__no_gate_reply_yet__",
            resolved_tool_wait={"A": [result_a]},
        ):
            pass
    repark = excinfo2.value

    # Node A finished and left the pending set entirely.
    assert ex._pending_tool_waits == []
    # Node B's gate SURVIVES the re-park untouched - drain-until-empty,
    # not silently dropped by resolving the unrelated tool_wait batch.
    assert len(ex._pending_agent_yields) == 1
    assert ex._pending_agent_yields[0].node_id == "B"
    assert ex._pending_agent_yields[0].tool_call_id == "tc-b"
    # A co-pending human gate always wins the re-park choice - ToolWaitPark
    # only fires when NEITHER toolcalls nor agent_yields remain pending.
    assert repark.graph_checkpoint is not None
    ck_pending_ay = repark.graph_checkpoint["pending_agent_yields"]
    assert len(ck_pending_ay) == 1
    assert ck_pending_ay[0]["node_id"] == "B"
    assert repark.graph_checkpoint.get("pending_tool_waits") in (None, [])


@pytest.mark.asyncio
async def test_gate_answers_first_tool_wait_survives_the_reprk(monkeypatch) -> None:
    """Same mixed superstep, opposite order: the human gate is answered
    FIRST while A's tool_wait batch is still mid-flight. The re-park must
    now be a ToolWaitPark (no human gate remains) carrying node A's
    still-pending batch, not a silent drop of it."""
    ex = await _mk_parallel_executor()
    _patch_run_agent_turn(monkeypatch, {
        "agent-a": _tool_wait_park("A", "1"),
        "agent-b": _ask_user_yield("B", "tc-b"),
    })

    with pytest.raises(YieldToWorker) as excinfo:
        async for _ev in ex.invoke([]):
            pass
    first_park = excinfo.value

    # Answer B's gate; A's tool_wait batch has NOT resolved yet
    # (resolved_tool_wait omitted / empty).
    with pytest.raises(ToolWaitPark) as excinfo2:
        async for _ev in ex.resume_from_checkpoint(
            first_park.graph_checkpoint,
            resumed_tcid="tc-b",
            agent_tool_result=Message(
                role="tool", parts=[ToolResultPart(id="tc-b", output="blue")],
            ),
        ):
            pass
    repark = excinfo2.value

    # Node B answered and left the pending set entirely.
    assert ex._pending_agent_yields == []
    # Node A's batch SURVIVES the re-park untouched.
    assert len(ex._pending_tool_waits) == 1
    assert ex._pending_tool_waits[0].node_id == "A"
    assert ex._pending_tool_waits[0].outstanding_task_ids == ["A:tool:0:1"]
    # No co-pending human gate remains -> the flattened ToolWaitPark path,
    # not YieldToWorker.
    assert repark.graph_checkpoint is not None
    assert repark.graph_checkpoint.get("pending_toolcalls") in (None, [])
    assert repark.graph_checkpoint.get("pending_agent_yields") in (None, [])
    ck_pending_tw = repark.graph_checkpoint["pending_tool_waits"]
    assert len(ck_pending_tw) == 1
    assert ck_pending_tw[0]["node_id"] == "A"
    assert "A:tool:0:1" in repark.outstanding_task_ids


@pytest.mark.asyncio
async def test_node_a_before_node_b_partial_wake(monkeypatch) -> None:
    """PURE park (no human gate at all): both A and B raise their own
    ToolWaitPark in the same superstep. Node A's batch goes terminal
    while node B's is still mid-flight (partial wake) - the resume must
    drain ONLY A and re-park on B alone, proving the per-node wake-key
    design (each fan-out sibling's batch is independently addressable)
    all the way through the executor's own resume plumbing."""
    ex = await _mk_parallel_executor()
    _patch_run_agent_turn(monkeypatch, {
        "agent-a": _tool_wait_park("A", "1"),
        "agent-b": _tool_wait_park("B", "1"),
    })

    with pytest.raises(ToolWaitPark) as excinfo:
        async for _ev in ex.invoke([]):
            pass
    first_park = excinfo.value
    assert first_park.graph_checkpoint is not None
    assert sorted(first_park.outstanding_task_ids) == ["A:tool:0:1", "B:tool:0:1"]
    pending_by_node = {
        pw["node_id"]: pw
        for pw in first_park.graph_checkpoint["pending_tool_waits"]
    }
    assert set(pending_by_node) == {"A", "B"}
    assert len(ex._pending_tool_waits) == 2

    # Only A's batch is ready; B's is still mid-flight.
    result_a = ToolResultPart(id="A:tool:0:1", output="result A", error=False)
    with pytest.raises(ToolWaitPark) as excinfo2:
        async for _ev in ex.resume_from_checkpoint(
            first_park.graph_checkpoint,
            resolved_tool_wait={"A": [result_a]},
        ):
            pass
    repark = excinfo2.value

    # A drained; B alone remains, correctly isolated from A's now-gone
    # batch (proves per-node scoping, not a flattened shared list).
    assert len(ex._pending_tool_waits) == 1
    assert ex._pending_tool_waits[0].node_id == "B"
    assert repark.outstanding_task_ids == ["B:tool:0:1"]
    assert repark.graph_checkpoint is not None
    ck_pending_tw = repark.graph_checkpoint["pending_tool_waits"]
    assert [pw["node_id"] for pw in ck_pending_tw] == ["B"]

    # Finish B too - the graph should now drain to completion (no more
    # pending, no more re-park) rather than hanging on an empty repark.
    result_b = ToolResultPart(id="B:tool:0:1", output="result B", error=False)
    drained = False
    try:
        async for _ev in ex.resume_from_checkpoint(
            repark.graph_checkpoint,
            resolved_tool_wait={"B": [result_b]},
        ):
            pass
        drained = True
    except (ToolWaitPark, YieldToWorker):
        drained = False
    assert drained is True
    assert ex._pending_tool_waits == []

    # 7a gate review (verdict item 2): A's own successor "C" must have
    # actually RUN, not just been silently dropped by the re-park firing
    # before its edge was ever walked. Divergent downstreams (A -> C,
    # B -> D, no shared node) make this observable independently of B's
    # own completion - the prior shared-End topology masked exactly this
    # by letting B's own edge to the SAME node paper over A's missing one.
    assert ex._node_states["C"].status == NodeRuntimeStatus.ENDED
    assert ex._node_states["D"].status == NodeRuntimeStatus.ENDED
