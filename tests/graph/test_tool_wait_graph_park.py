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


async def _mk_executor_for_graph(graph: Graph) -> GraphExecutor:
    async def agent_resolver(agent_id: str) -> Agent:
        return Agent(
            id=agent_id, description=agent_id, model=AgentModel(profile_id="p--m"),
        )

    async def llm_resolver(_agent):
        return (_UnusedLLM(), _model())

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


async def _mk_parallel_executor() -> GraphExecutor:
    return await _mk_executor_for_graph(_parallel_graph())


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


@pytest.mark.xfail(
    reason=(
        "round-4 gate ruling: reverted resume_from_checkpoint's ready-set "
        "line (rounds 1-3: reorder, union, already_ended, settled_ids) back "
        "to merge-base `ready = next_ready` after four consecutive fix "
        "rounds each introduced a new bug - two of them flag-off "
        "regressions, on code not needed for merge (flag off means "
        "tw_pending is always empty). This test's final assertions pin "
        "item 2's original successor-drop finding, which merge-base "
        "semantics reintroduce for a MULTI-entry partial wake (each "
        "resume's own edge-walk only sees its own completed_ids, and a "
        "PRIOR resume's fold is a bare-assignment casualty of the NEXT "
        "resume). Kept as specification, not deleted - see task 01a0812c."
    ),
    strict=True,
)
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


@pytest.mark.asyncio
async def test_iteration_bumps_once_per_drained_superstep_not_per_partial_resume(
    monkeypatch,
) -> None:
    """7a gate review (verdict R2-4, optional test while in the file):
    context.iteration must bump once PER DRAINED SUPERSTEP, not once per
    partial resume - a two-batch partial wake (A, B) must NOT bump on the
    first (still-pending) resume, and must bump exactly once for itself
    once fully drained (a second, unrelated bump follows for the NEXT
    superstep it hands off to). Bumping on the first partial resume too
    would prematurely consume a flag-off multi-gate graph's
    max_iterations budget."""
    ex = await _mk_parallel_executor()
    _patch_run_agent_turn(monkeypatch, {
        "agent-a": _tool_wait_park("A", "1"),
        "agent-b": _tool_wait_park("B", "1"),
    })

    with pytest.raises(ToolWaitPark) as excinfo:
        async for _ev in ex.invoke([]):
            pass
    first_park = excinfo.value
    iteration_at_park = ex._context.iteration

    # Partial resume: only A's batch is ready - iteration must NOT bump
    # yet, since B's batch is still pending (the superstep isn't drained).
    result_a = ToolResultPart(id="A:tool:0:1", output="result A", error=False)
    with pytest.raises(ToolWaitPark) as excinfo2:
        async for _ev in ex.resume_from_checkpoint(
            first_park.graph_checkpoint,
            resolved_tool_wait={"A": [result_a]},
        ):
            pass
    repark = excinfo2.value
    assert ex._context.iteration == iteration_at_park

    # Final resume: B's batch resolves too, the superstep fully drains -
    # TWO bumps land here, not three: one for THIS resumed superstep
    # (A+B) finishing (the R2-4 fix, once - not once per partial resume),
    # and one more for the NEXT superstep (C+D, both End nodes) that
    # resume_from_checkpoint hands off to via _run_superstep_loop's own
    # pre-existing, unrelated per-superstep bump. A regression that fires
    # the R2-4 bump per-partial-resume would land on +3 instead.
    result_b = ToolResultPart(id="B:tool:0:1", output="result B", error=False)
    async for _ev in ex.resume_from_checkpoint(
        repark.graph_checkpoint,
        resolved_tool_wait={"B": [result_b]},
    ):
        pass
    assert ex._context.iteration == iteration_at_park + 2


@pytest.mark.asyncio
async def test_completed_sibling_is_not_re_executed_on_final_resume(
    monkeypatch,
) -> None:
    """7a gate review (verdict R2-2, FLAG-OFF REGRESSION): a superstep
    where node A parks (tool_wait) while sibling node B completes
    NORMALLY in the SAME original dispatch ({A parks, N completes}).
    f705bc5c's ready-union fix (item 2) kept ``ready`` as the FULL
    originally-dispatched set across every resume - before the R2-2
    exclusion fix, B's id would still be sitting in that set once the
    drain finally completes, and the final dispatch call would re-run
    B's WHOLE turn a second time: duplicate LLM call, duplicate tool
    side effects, duplicate gate prompts.

    B's own successor ("D") is deliberately NOT asserted here - B's
    successor edge never getting walked when a sibling parks in the
    SAME superstep is the separate, PRE-EXISTING 01a08016 bug, not
    fixed in this arc.
    """
    call_counts: dict[str, int] = {}
    parked_once: set[str] = set()
    ex = await _mk_parallel_executor()

    import primer.graph._agent_node as agent_node_mod

    async def _spy(**kwargs):
        agent = kwargs["agent"]
        call_counts[agent.id] = call_counts.get(agent.id, 0) + 1
        # Single-shot, like _patch_run_agent_turn's own consume-once
        # behavior: A's RESUMED continuation must complete normally,
        # not re-park forever - only its FIRST dispatch parks.
        if agent.id == "agent-a" and agent.id not in parked_once:
            parked_once.add(agent.id)
            raise _tool_wait_park("A", "1")
            yield  # pragma: no cover - unreachable, keeps this a generator
        return
        yield  # pragma: no cover - unreachable, keeps this a generator

    monkeypatch.setattr(agent_node_mod, "run_agent_turn", _spy)

    with pytest.raises(ToolWaitPark) as excinfo:
        async for _ev in ex.invoke([]):
            pass
    first_park = excinfo.value
    assert first_park.graph_checkpoint is not None
    # Both dispatched once in the original superstep: A parks, B
    # completes normally.
    assert call_counts == {"agent-a": 1, "agent-b": 1}

    result_a = ToolResultPart(id="A:tool:0:1", output="result A", error=False)
    drained = False
    try:
        async for _ev in ex.resume_from_checkpoint(
            first_park.graph_checkpoint,
            resolved_tool_wait={"A": [result_a]},
        ):
            pass
        drained = True
    except (ToolWaitPark, YieldToWorker):
        drained = False
    assert drained is True

    # The whole point: B must NOT have been dispatched a second time by
    # the final resume's own next-superstep call. A legitimately runs
    # AGAIN here - once for its original park, once for its resumed
    # continuation (which now completes normally) - that second call is
    # the resume actually working, not the regression.
    assert call_counts == {"agent-a": 2, "agent-b": 1}


@pytest.mark.asyncio
async def test_failed_sibling_is_not_re_executed_and_stays_failed(
    monkeypatch,
) -> None:
    """Test matrix item 2 (verdict R3-1/R3-2): {A parks, F fails} - a
    sibling that FAILED (not just completed) in the original superstep
    must ALSO be excluded from re-dispatch on the final resume. R2-2's
    ``already_ended`` filter matched ENDED only (R3-2), so a FAILED
    sibling re-dispatched on the final resume and its original failure
    was silently swallowed by whatever the re-run produced. settled_ids
    tracks BOTH terminal kinds identically, no status-kind check needed.
    """
    call_counts: dict[str, int] = {}
    parked_once: set[str] = set()
    ex = await _mk_parallel_executor()

    import primer.graph._agent_node as agent_node_mod

    async def _spy(**kwargs):
        agent = kwargs["agent"]
        call_counts[agent.id] = call_counts.get(agent.id, 0) + 1
        if agent.id == "agent-a" and agent.id not in parked_once:
            parked_once.add(agent.id)
            raise _tool_wait_park("A", "1")
            yield  # pragma: no cover - unreachable, keeps this a generator
        if agent.id == "agent-b":
            raise RuntimeError("agent-b boom")
            yield  # pragma: no cover - unreachable, keeps this a generator
        return
        yield  # pragma: no cover - unreachable, keeps this a generator

    monkeypatch.setattr(agent_node_mod, "run_agent_turn", _spy)

    with pytest.raises(ToolWaitPark) as excinfo:
        async for _ev in ex.invoke([]):
            pass
    first_park = excinfo.value
    assert ex._node_states["B"].status == NodeRuntimeStatus.FAILED
    b_error_at_park = ex._node_states["B"].error
    assert b_error_at_park is not None

    result_a = ToolResultPart(id="A:tool:0:1", output="result A", error=False)
    drained = False
    try:
        async for _ev in ex.resume_from_checkpoint(
            first_park.graph_checkpoint,
            resolved_tool_wait={"A": [result_a]},
        ):
            pass
        drained = True
    except (ToolWaitPark, YieldToWorker):
        drained = False
    assert drained is True

    # B was NOT re-dispatched on the final resume (call count stays at
    # the original 1), and its FAILED status/error survive untouched -
    # not silently overwritten by a re-run's own (successful) outcome.
    assert call_counts["agent-b"] == 1
    assert ex._node_states["B"].status == NodeRuntimeStatus.FAILED
    assert ex._node_states["B"].error == b_error_at_park


def _cyclic_collision_graph() -> Graph:
    """begin -> X -> {A, B}; A -> X (the cycle); B -> D.

    X dispatches ALONE first and ends normally - a REAL, early
    completion (superstep 2). Its own edges then fan out to {A, B}
    together (superstep 3): A parks, and A's OWN resolution routes BACK
    to X - the same node_id that already carries an ENDED status from
    its earlier, now-unrelated visit. B parks too, independently, and
    resolves in a SEPARATE, LATER partial resume of the SAME superstep.
    That two-resume structure is essential: a bug in the exclusion
    formula only manifests on the SECOND resume, checking a ``ready``
    set that carries the FIRST resume's own accumulated fold (see the
    test's own docstring for the exact mechanism).
    """
    return Graph(
        id="g-cyclic-collision", description="begin -> X -> {A,B}; A -> X; B -> D",
        nodes=[
            _BeginNode(id="begin"),
            _AgentNodeRef(id="X", agent_id="agent-x"),
            _AgentNodeRef(id="A", agent_id="agent-a"),
            _AgentNodeRef(id="B", agent_id="agent-b"),
            _EndNode(id="D"),
        ],
        edges=[
            _StaticEdge(from_node="begin", to_node="X"),
            _StaticEdge(from_node="X", to_node="A"),
            _StaticEdge(from_node="X", to_node="B"),
            _StaticEdge(from_node="A", to_node="X"),
            _StaticEdge(from_node="B", to_node="D"),
        ],
    )


@pytest.mark.xfail(
    reason=(
        "round-4 gate ruling: this test pins R3-1's own fix (settled_ids), "
        "which was itself reverted after the gate found a BLOCKER in it - "
        "subtracting settled_ids from the ACCUMULATED ready deletes a "
        "loop-back target a PRIOR resume legitimately folded in, in a "
        "same-superstep topology this test doesn't cover (X completes "
        "ALONE in its own superstep here). resume_from_checkpoint's "
        "ready-set line is back to merge-base `ready = next_ready` after "
        "four rounds each introduced a new bug. Kept as specification, not "
        "deleted - the cyclic loop-back scenario is real and becomes part "
        "of task 01a0812c's mandatory scenario matrix."
    ),
    strict=True,
)
@pytest.mark.asyncio
async def test_cyclic_loop_back_across_two_partial_resumes_still_dispatches(
    monkeypatch,
) -> None:
    """Test matrix item 3 (verdict R3-1, the headline case): a node_id
    (X) that legitimately ENDED in an earlier superstep of THIS SAME run
    must still dispatch for real when it becomes ready again later - via
    A's own resolution (A -> X, the cycle), while B's own batch is still
    mid-flight in the SAME superstep as A.

    Exact mechanism: superstep 3 dispatches {A, B}, both park. Resume 1
    (A resolves) folds X into the accumulated ``ready`` via A's own
    ``next_ready`` (A -> X) - ``ready`` becomes ``{B, X}``, persisted in
    the repark's own checkpoint. Resume 2 (B resolves): method-entry
    ``ready`` is this ``{B, X}``. R2-2's ``already_ended`` filter (and
    R2-4's identical inheritance of it) computed "already ran" from
    ``node_states[nid].status == ENDED`` alone - X's status IS ENDED at
    this point (a REAL completion, from superstep 2, before the cycle
    ever brought it back) - so the OLD filter wrongly deletes X, and
    round 2's OWN ``next_ready`` (from B's edges alone) does not contain
    X to add it back via the union. ``settled_ids`` cannot make this
    mistake: reset at the start of EVERY superstep, X's superstep-2
    completion is not a member of superstep 3's settled set, regardless
    of what ``node_states`` still shows.
    """
    call_counts: dict[str, int] = {}
    ex = await _mk_executor_for_graph(_cyclic_collision_graph())

    import primer.graph._agent_node as agent_node_mod

    async def _spy(**kwargs):
        agent = kwargs["agent"]
        call_counts[agent.id] = call_counts.get(agent.id, 0) + 1
        if agent.id == "agent-x":
            if call_counts["agent-x"] == 1:
                # X's FIRST visit (superstep 2): completes normally.
                return
                yield  # pragma: no cover - unreachable, keeps this a generator
            # X's SECOND visit (the cycle, via A -> X): park again, so
            # the test can observe the dispatch happened for real
            # without having to chase the cycle any further.
            raise _tool_wait_park("X", "2")
            yield  # pragma: no cover - unreachable, keeps this a generator
        if agent.id == "agent-a" and call_counts["agent-a"] == 1:
            raise _tool_wait_park("A", "1")
            yield  # pragma: no cover - unreachable, keeps this a generator
        if agent.id == "agent-b" and call_counts["agent-b"] == 1:
            raise _tool_wait_park("B", "1")
            yield  # pragma: no cover - unreachable, keeps this a generator
        return
        yield  # pragma: no cover - unreachable, keeps this a generator

    monkeypatch.setattr(agent_node_mod, "run_agent_turn", _spy)

    # invoke() drives superstep 1 (begin, not agent-backed), superstep 2
    # (X alone, completes), and superstep 3 (A, B - both park) in one call.
    with pytest.raises(ToolWaitPark) as excinfo:
        async for _ev in ex.invoke([]):
            pass
    first_park = excinfo.value
    assert ex._node_states["X"].status == NodeRuntimeStatus.ENDED
    assert call_counts == {"agent-x": 1, "agent-a": 1, "agent-b": 1}

    # Resume 1: A resolves. Its own edge (A -> X) folds X back into the
    # accumulated ready set - X now carries a STALE ENDED status from
    # its unrelated superstep-2 completion.
    result_a = ToolResultPart(id="A:tool:0:1", output="result A", error=False)
    with pytest.raises(ToolWaitPark) as excinfo2:
        async for _ev in ex.resume_from_checkpoint(
            first_park.graph_checkpoint,
            resolved_tool_wait={"A": [result_a]},
        ):
            pass
    repark = excinfo2.value

    # Resume 2: B resolves too - THIS is the resume where the bug fires
    # (see the test's own docstring for the exact mechanism).
    result_b = ToolResultPart(id="B:tool:0:1", output="result B", error=False)
    with pytest.raises(ToolWaitPark) as excinfo3:
        async for _ev in ex.resume_from_checkpoint(
            repark.graph_checkpoint,
            resolved_tool_wait={"B": [result_b]},
        ):
            pass
    second_repark = excinfo3.value

    # X was actually DISPATCHED a second time - not silently excluded by
    # its stale superstep-2 status - proven by its own mock firing again
    # and re-parking for real, on the SAME node_id.
    assert call_counts["agent-x"] == 2
    assert second_repark.outstanding_task_ids == ["X:tool:0:2"]

