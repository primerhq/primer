"""A ToolCall node that parked and is resumed answers the call row it wrote (ticket 01a11faa, the resume half).

The node writes its call as a ``tool_call`` row before the tool runs (``tests/graph/test_tool_call_node_call_row.py``). When the tool parks (an approval gate, or a value-yielding tool such as ``ask_user``) the
row stays open until the operator answers, and the resume drain must close it with a ``tool_result`` row under the SAME scoped id, whichever way the node ends: the approved tool's answer, a rejection, or the
operator's reply for a value-yielding tool. Before this the resumed node wrote no row at all, so the call row (and, before the call row existed, a result written for nothing) never paired.

These cases drive the real ``resume_graph_from_checkpoint`` (the worker adapter both resume coordinators call) with the drain tap writing to a recording workspace, after a live first run whose records go through the
real ``translate_stream_event`` and whose park is stashed with ``stash_graph_scoped_ids`` the way ``dispatch.py`` does it.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from primer.graph._node_identity import current_toolcall_id
from primer.graph.executor import GraphExecutor
from primer.model.chat import ToolCallResult, ToolResultPart
from primer.model.graph import Graph, GraphNodeMessage, GraphThread, _BeginNode, _EndNode, _StaticEdge, _ToolCallNode
from primer.model.yield_ import Yielded, YieldToWorker
from primer.session.persistence import _CoalesceState, stash_graph_scoped_ids, translate_stream_event
from primer.worker.graph_resume import resume_graph_from_checkpoint
from primer.worker.yield_resume_registry import register_resume_hook
from tests._resume_hook_fakes import (
    DrainTapPool,
    FakeSessionRow,
    FakeSessionStorage,
    FakeStorage,
    RecordingWorkspaceIO,
    make_toolcall_executor,
)
from tests.graph.test_toolcall_dispatch import _InMemoryStorage


def _call_id() -> str:
    """The id the dispatch is called with: what the manager would be handed (``WorkspaceGraphExecutor._dispatch_toolcall`` reads the same value)."""
    return current_toolcall_id() or "tc-unpublished"


def _graph() -> Graph:
    return Graph(
        id="g-park", description="begin -> tool -> end",
        nodes=[_BeginNode(id="begin"), _ToolCallNode(id="t", tool_id="dangerous__tool", arguments={"q": "x"}), _EndNode(id="exit", output_template="{{ nodes.t.text }}")],
        edges=[_StaticEdge(from_node="begin", to_node="t"), _StaticEdge(from_node="t", to_node="exit")],
    )


class _World:
    """A graph parked at its ToolCall node: the checkpoint the park left, the records the live run wrote, and a builder of the executors that resume it."""

    def __init__(self, graph: Graph, thread: Any, ts: Any, ms: Any, checkpoint: dict, live: list[dict]) -> None:
        self.graph, self.thread, self.ts, self.ms, self.checkpoint, self.live = graph, thread, ts, ms, checkpoint, live

    def executor(self, dispatcher: Any) -> Any:
        return make_toolcall_executor(self.graph, self.thread, self.ts, self.ms, dispatcher)


async def _park(first: Any) -> _World:
    """Run the node to its park (records through ``translate_stream_event``, the park stashed)."""
    graph = _graph()
    ts: _InMemoryStorage[GraphThread] = _InMemoryStorage(GraphThread)
    ms: _InMemoryStorage[GraphNodeMessage] = _InMemoryStorage(GraphNodeMessage)
    thread = await GraphExecutor.open_thread(graph=graph, thread_storage=ts, title="t")  # type: ignore[arg-type]
    parker = make_toolcall_executor(graph, thread, ts, ms, first)
    state = _CoalesceState()
    parker.bind_coalesce_state(state)
    live: list[dict] = []
    with pytest.raises(YieldToWorker):
        async for ev in parker.invoke([]):
            rec = translate_stream_event(ev, state, turn_no=1)
            live.extend(r.model_dump(mode="json") for r in ([] if rec is None else rec if isinstance(rec, list) else [rec]))
    checkpoint = parker.snapshot_state()
    stash_graph_scoped_ids(checkpoint, state)
    return _World(graph, thread, ts, ms, checkpoint, live)


async def _parked(first: Any, resumed: Any) -> tuple[dict, Any, list[dict]]:
    """``_park``, and the one executor that resumes it."""
    world = await _park(first)
    return world.checkpoint, world.executor(resumed), world.live


async def _resume_full(checkpoint: dict, executor: Any, payload: Any, tcid: str, event_key: str | None = None) -> tuple[list[dict], Any]:
    """The records the resume drain tap wrote, and the re-park (``None`` when the graph drained)."""
    io = RecordingWorkspaceIO()
    row = FakeSessionRow(sid="gs-park", workspace_id="ws-1", turn_no=1, last_seq=10)
    pool = DrainTapPool(workspace_io=io, storage=FakeStorage(FakeSessionStorage(row)))
    _decision, repark, _seq = await resume_graph_from_checkpoint(
        executor=executor, checkpoint=checkpoint, payload=payload, resumed_tcid=tcid, resumed_event_key=event_key, pool=pool, session=row,  # type: ignore[arg-type]
    )
    return [json.loads(one) for _sid, line in io.lines for one in line.decode().splitlines() if one.strip()], repark   # a write can carry several records


async def _resume(checkpoint: dict, executor: Any, payload: Any, tcid: str) -> list[dict]:
    """The records the resume drain tap wrote, for a resume that drains the graph."""
    written, repark = await _resume_full(checkpoint, executor, payload, tcid)
    assert repark is None
    return written


def _approval_park(call_id_out: list[str]):
    async def first(node: Any, arguments: Any) -> ToolResultPart:
        call_id_out.append(_call_id())
        raise YieldToWorker(Yielded(tool_name="_approval", event_key=f"tool_approval:s:{call_id_out[0]}"), tool_call_id=call_id_out[0])

    return first


def _live_call(live: list[dict]) -> dict:
    (call,) = [r for r in live if r["kind"] == "tool_call"]
    return call


def _results(written: list[dict]) -> list[dict]:
    return [r for r in written if r.get("kind") == "tool_result"]


@pytest.mark.asyncio
async def test_an_approved_call_is_answered_under_the_scoped_id_of_its_row() -> None:
    ids: list[str] = []

    async def approved(node: Any, arguments: Any, bypass_approval: bool = False) -> ToolResultPart:
        return ToolResultPart(id=_call_id(), output="it ran")

    checkpoint, resumer, live = await _parked(_approval_park(ids), approved)
    written = await _resume(checkpoint, resumer, {"decision": "approved"}, ids[0])
    call = _live_call(live)
    assert [(r["payload"]["call_id"], r["payload"]["output"], r["payload"]["error"], r["node_id"]) for r in _results(written)] == [(call["payload"]["id"], "it ran", False, "t")]


@pytest.mark.asyncio
async def test_a_rejected_call_is_answered_with_an_error_under_the_scoped_id_of_its_row() -> None:
    ids: list[str] = []

    async def never(node: Any, arguments: Any, bypass_approval: bool = False) -> ToolResultPart:  # pragma: no cover - the rejection replaces the dispatch
        raise AssertionError("a rejected call is not dispatched")

    checkpoint, resumer, live = await _parked(_approval_park(ids), never)
    written = await _resume(checkpoint, resumer, {"decision": "rejected", "reason": "no"}, ids[0])
    call = _live_call(live)
    assert [(r["payload"]["call_id"], r["payload"]["error"], r["node_id"]) for r in _results(written)] == [(call["payload"]["id"], True, "t")]


@pytest.mark.asyncio
async def test_a_tool_that_fails_after_the_approval_is_answered_with_an_error() -> None:
    ids: list[str] = []

    async def broken(node: Any, arguments: Any, bypass_approval: bool = False) -> ToolResultPart:
        raise RuntimeError("the tool broke")

    checkpoint, resumer, live = await _parked(_approval_park(ids), broken)
    written = await _resume(checkpoint, resumer, {"decision": "approved"}, ids[0])
    call = _live_call(live)
    assert [(r["payload"]["call_id"], r["payload"]["error"]) for r in _results(written)] == [(call["payload"]["id"], True)]


@pytest.mark.asyncio
async def test_a_value_yielding_call_is_answered_with_the_operators_reply() -> None:
    ids: list[str] = []

    def hook(meta: Any, payload: Any, ctx: Any) -> ToolCallResult:
        return ToolCallResult(output=json.dumps({"response": payload["response"]}), is_error=False)

    register_resume_hook("test_call_row_hook", hook)

    async def first(node: Any, arguments: Any) -> ToolResultPart:
        ids.append(_call_id())
        raise YieldToWorker(Yielded(tool_name="test_call_row_hook", event_key=f"test_call_row_hook:s:{ids[0]}", resume_metadata={"q": "?"}), tool_call_id=ids[0])

    async def never(node: Any, arguments: Any, bypass_approval: bool = False) -> ToolResultPart:  # pragma: no cover - the hook answers, the tool is not run again
        raise AssertionError("a value-yielding call is not dispatched again")

    checkpoint, resumer, live = await _parked(first, never)
    written = await _resume(checkpoint, resumer, {"response": "blue"}, ids[0])
    call = _live_call(live)
    assert [(r["payload"]["call_id"], json.loads(r["payload"]["output"]), r["payload"]["error"]) for r in _results(written)] == [(call["payload"]["id"], {"response": "blue"}, False)]


def _gate(ids: list[str]):
    """A dispatcher that parks on an approval gate whose key is built from the id it is called with, as ``ToolExecutionManager`` builds it (``tool_approval:<session>:<call id>``)."""
    async def gate(node: Any, arguments: Any, bypass_approval: bool = False) -> ToolResultPart:
        ids.append(_call_id())
        raise YieldToWorker(Yielded(tool_name="_approval", event_key=f"tool_approval:s:{ids[-1]}"), tool_call_id=ids[-1])

    return gate


@pytest.mark.asyncio
async def test_a_second_gate_inside_the_approved_dispatch_has_its_own_id_and_key_and_the_first_gates_id_does_not_answer_for_it() -> None:
    """B2 of round 1 (C-033 class): the approved re-dispatch used the PARKED call id, so a second gate raised inside it had the first gate's event key, and a token-less respond for the first gate (accepted
    this release) resolved by ``tool_call_id`` to the second and decided it. The bypassed dispatch gets a fresh id; the id of the call ROW is carried separately (``row_call_id``) so the answer still pairs."""
    from primer.session.pending_gates import pending_entries, resolve_pending_gate

    ids: list[str] = []
    world = await _park(_gate(ids))
    written, repark = await _resume_full(world.checkpoint, world.executor(_gate(ids)), {"decision": "approved"}, ids[0])
    assert repark is not None and len(ids) == 2, "the approved dispatch hit a second gate"
    assert ids[0] != ids[1], "the second gate was raised under the first gate's call id"
    first_key, second_key = (f"tool_approval:s:{i}" for i in ids)
    (pending,) = repark.graph_checkpoint["pending_toolcalls"]
    assert (pending["tool_call_id"], pending["parked_event_key"]) == (ids[1], second_key) and first_key != second_key
    assert _results(written) == [], "the second park leaves the row open"
    # a token-less respond for the FIRST gate names its tool_call_id and no key: it must not select the second gate
    assert pending_entries(repark.graph_checkpoint, "pending_toolcalls", tool_call_id=ids[0]) == []
    assert resolve_pending_gate({"graph_checkpoint": repark.graph_checkpoint}, tool_call_id=ids[0]) is None
    assert len(pending_entries(repark.graph_checkpoint, "pending_toolcalls", tool_call_id=ids[1])) == 1


@pytest.mark.asyncio
async def test_a_two_phase_park_is_answered_under_the_scoped_id_of_the_row_the_node_wrote() -> None:
    """N1: approval, a second gate inside the approved dispatch, approval again: one call row, answered once, under the id the live run stashed (it survives the re-park and the second resume)."""
    ids: list[str] = []

    async def finishes(node: Any, arguments: Any, bypass_approval: bool = False) -> ToolResultPart:
        ids.append(_call_id())
        return ToolResultPart(id=ids[-1], output="it ran at last")

    world = await _park(_gate(ids))
    _written, repark = await _resume_full(world.checkpoint, world.executor(_gate(ids)), {"decision": "approved"}, ids[0])
    assert repark is not None
    (pending,) = repark.graph_checkpoint["pending_toolcalls"]
    written, again = await _resume_full(repark.graph_checkpoint, world.executor(finishes), {"decision": "approved"}, ids[1], event_key=pending["parked_event_key"])
    assert again is None
    call = _live_call(world.live)
    assert [(r["payload"]["call_id"], r["payload"]["output"], r["payload"]["error"]) for r in _results(written)] == [(call["payload"]["id"], "it ran at last", False)]


@pytest.mark.asyncio
async def test_a_park_written_before_the_call_row_existed_is_not_answered_with_a_result_that_has_no_call() -> None:
    """N3: a checkpoint from before this change has no row id and no scoped id; its node never wrote a row, and a raw-id result with no call is a row the transcript renders on its own."""
    ids: list[str] = []

    async def approved(node: Any, arguments: Any, bypass_approval: bool = False) -> ToolResultPart:
        return ToolResultPart(id=_call_id(), output="it ran")

    checkpoint, resumer, _live = await _parked(_approval_park(ids), approved)
    for entry in checkpoint["pending_toolcalls"]:
        entry.pop("row_call_id", None)
        entry["scoped_tool_call_id"] = None
    written = await _resume(checkpoint, resumer, {"decision": "approved"}, ids[0])
    assert _results(written) == []


@pytest.mark.asyncio
async def test_a_value_yielding_call_whose_hook_raises_is_answered_with_an_error() -> None:
    """N1: the hook of a value-yielding tool (``ask_user``) raising on the operator's reply fails the node, and the row it wrote is closed with an error answer."""
    ids: list[str] = []

    def hook(meta: Any, payload: Any, ctx: Any) -> ToolCallResult:
        raise RuntimeError("the hook broke")

    register_resume_hook("test_call_row_hook_raises", hook)

    async def first(node: Any, arguments: Any) -> ToolResultPart:
        ids.append(_call_id())
        raise YieldToWorker(Yielded(tool_name="test_call_row_hook_raises", event_key=f"test_call_row_hook_raises:s:{ids[0]}", resume_metadata={"q": "?"}), tool_call_id=ids[0])

    async def never(node: Any, arguments: Any, bypass_approval: bool = False) -> ToolResultPart:  # pragma: no cover - the hook answers
        raise AssertionError("a value-yielding call is not dispatched again")

    checkpoint, resumer, live = await _parked(first, never)
    written = await _resume(checkpoint, resumer, {"response": "blue"}, ids[0])
    call = _live_call(live)
    assert [(r["payload"]["call_id"], r["payload"]["error"]) for r in _results(written)] == [(call["payload"]["id"], True)]
