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

from primer.graph import _node_identity
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
    """The id the node called its tool under: what the manager would be handed (a fixed one before the node published it)."""
    return getattr(_node_identity, "current_toolcall_id", lambda: None)() or "tc-fixed"


def _graph() -> Graph:
    return Graph(
        id="g-park", description="begin -> tool -> end",
        nodes=[_BeginNode(id="begin"), _ToolCallNode(id="t", tool_id="dangerous__tool", arguments={"q": "x"}), _EndNode(id="exit", output_template="{{ nodes.t.text }}")],
        edges=[_StaticEdge(from_node="begin", to_node="t"), _StaticEdge(from_node="t", to_node="exit")],
    )


async def _parked(first: Any, resumed: Any) -> tuple[dict, Any, list[dict]]:
    """Run the node to its park (records through ``translate_stream_event``, the park stashed), and build the executor that resumes it."""
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
    return checkpoint, make_toolcall_executor(graph, thread, ts, ms, resumed), live


async def _resume(checkpoint: dict, executor: Any, payload: Any, tcid: str) -> list[dict]:
    """The records the resume drain tap wrote."""
    io = RecordingWorkspaceIO()
    row = FakeSessionRow(sid="gs-park", workspace_id="ws-1", turn_no=1, last_seq=10)
    pool = DrainTapPool(workspace_io=io, storage=FakeStorage(FakeSessionStorage(row)))
    _decision, repark, _seq = await resume_graph_from_checkpoint(
        executor=executor, checkpoint=checkpoint, payload=payload, resumed_tcid=tcid, pool=pool, session=row,  # type: ignore[arg-type]
    )
    assert repark is None
    return [json.loads(one) for _sid, line in io.lines for one in line.decode().splitlines() if one.strip()]   # a write can carry several records


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
