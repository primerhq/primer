"""A decision resumes the sibling it answers, not every sibling that shares its raw tool_call_id (console review C-033 round 2, ticket 01a11fc6-0cce).

The provider's ``tool_call_id`` is not unique: two fan-out siblings of one graph superstep can both park on ``call_0``. The API edge already tells them
apart by ``gate_id`` and publishes to the answered gate's own event key, but the worker then cut the FIRED key down to its last segment (the raw id) and
the engine selected every pending entry that carried it: B's approval also resumed A, with B's decision. The fired event key is now threaded down
(``replies`` -> ``resume_graph_from_checkpoint(resumed_event_key=)`` -> ``resume_from_checkpoint``), the entries are selected by it (the raw id only for
a key-less drain, or when the fired key names no pending entry), an ``ask_user`` inside a graph node is scoped by the node as an approval is, and the
resume-time audit record resolves the gate by the fired key.
"""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from primer.model.chat import Message, ToolResultPart
from primer.model.storage import OffsetPage
from primer.model.tool_approval import ToolApprovalRecord
from primer.model.yield_ import Yielded, YieldToWorker
from primer.session.pending_gates import resolve_pending_gate
from primer.worker import graph_resume_coordinator
from primer.worker.graph_resume_coordinator import write_approval_record_for_graph
from primer.worker.yield_runtime import ParkedState
from tests._resume_hook_fakes import (
    EngineFakePool as _FakePool,
    EngineStorageProvider as _StorageProvider,
    NullWorkspaceIO as _WorkspaceIO,
    waiting_graph_session as _session,
)
from tests.conftest import _FakeStorageProvider
from tests.graph.test_tool_wait_graph_park import _mk_parallel_executor, _patch_run_agent_turn

GA, GB = "a" * 32, "b" * 32
RAW = "call_0"
ASK_A, ASK_B = f"ask_user:gs-1:A:{RAW}", f"ask_user:gs-1:B:{RAW}"
APPROVAL_A, APPROVAL_B = f"tool_approval:gs-1:A:{RAW}", f"tool_approval:gs-1:B:{RAW}"


def _ask_user(node: str, gate_id: str) -> YieldToWorker:
    return YieldToWorker(
        Yielded(
            tool_name="ask_user", event_key=f"ask_user:gs-1:{node}:{RAW}",
            resume_metadata={"prompt": f"question of {node}", "gate_id": gate_id},
        ),
        tool_call_id=RAW,
        llm_messages=[{"role": "assistant", "parts": [{"type": "text", "text": f"{node} asking"}]}],
    )


def _approval(node: str, gate_id: str) -> YieldToWorker:
    return YieldToWorker(
        Yielded(
            tool_name="_approval", event_key=f"tool_approval:gs-1:{node}:{RAW}",
            resume_metadata={
                "policy_id": "pol", "approval_type": "required", "gate_reason": "m", "approvers": None, "gate_id": gate_id,
                "original_call": {"id": RAW, "name": "delete_workspace", "arguments": {"id": node}},
            },
        ),
        tool_call_id=RAW,
        llm_messages=[{"role": "assistant", "parts": [{"type": "text", "text": f"{node} asking"}]}],
    )


async def _two_parked_siblings(monkeypatch, make) -> tuple[dict, YieldToWorker]:
    ex = await _mk_parallel_executor()
    _patch_run_agent_turn(monkeypatch, {"agent-a": make("A", GA), "agent-b": make("B", GB)})
    with pytest.raises(YieldToWorker) as first:
        async for _ev in ex.invoke([]):
            pass
    return first.value.graph_checkpoint, first.value


def _pending_nodes(repark) -> list[str]:
    return [e["node_id"] for e in repark.graph_checkpoint["pending_agent_yields"]]


async def _resume_engine(monkeypatch, make, fired_key: str, payload: dict):
    checkpoint, first = await _two_parked_siblings(monkeypatch, make)
    pool = _FakePool(storage=_StorageProvider(), workspace_io=_WorkspaceIO(), executor_factory=_mk_parallel_executor)
    session = _session()
    session.parked_state = {"resume_event_payloads": {"k": {"event_key": fired_key, "payload": payload}}}
    parked = ParkedState(
        yielded=first.yielded, llm_messages=[], turn_no=0, started_at=datetime.now(UTC), tool_call_id=first.tool_call_id,
        resume_event_payload=payload, graph_checkpoint=checkpoint,
    )
    outcome = await graph_resume_coordinator.resume_graph_engine(pool, session, parked)
    return pool, outcome


# ---- the engine ----------------------------------------------------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_answering_sibling_b_leaves_sibling_a_waiting_for_its_own_answer(monkeypatch) -> None:
    """The reviewer's probe, through the real ``resume_graph_engine``: B's reply must not resume A with B's answer."""
    pool, outcome = await _resume_engine(monkeypatch, _ask_user, ASK_B, {"response": "blue"})

    assert pool.end_session_calls == [], "A was resumed with B's answer and the graph ran to its end"
    assert len(pool.repark_calls) == 1 and outcome == "REPARKED"
    assert _pending_nodes(pool.repark_calls[0]) == ["A"]


@pytest.mark.asyncio
async def test_the_fired_event_key_is_what_the_engine_delivers_the_answer_for(monkeypatch) -> None:
    pool, _outcome = await _resume_engine(monkeypatch, _ask_user, ASK_B, {"response": "blue"})

    assert pool.agent_tool_result_event_keys == [ASK_B]
    assert pool.agent_tool_result_tcids == [RAW]


@pytest.mark.asyncio
async def test_a_reply_for_the_first_sibling_leaves_the_second_waiting(monkeypatch) -> None:
    pool, _outcome = await _resume_engine(monkeypatch, _ask_user, ASK_A, {"response": "blue"})

    assert _pending_nodes(pool.repark_calls[0]) == ["B"]


@pytest.mark.asyncio
async def test_both_siblings_answered_in_one_cycle_are_both_resumed_each_by_its_own_key(monkeypatch) -> None:
    checkpoint, first = await _two_parked_siblings(monkeypatch, _ask_user)
    pool = _FakePool(storage=_StorageProvider(), workspace_io=_WorkspaceIO(), executor_factory=_mk_parallel_executor)
    session = _session()
    session.parked_state = {"resume_event_payloads": {
        "ka": {"event_key": ASK_A, "payload": {"response": "red"}},
        "kb": {"event_key": ASK_B, "payload": {"response": "blue"}},
    }}
    parked = ParkedState(
        yielded=first.yielded, llm_messages=[], turn_no=0, started_at=datetime.now(UTC), tool_call_id=first.tool_call_id,
        resume_event_payload={"response": "red"}, graph_checkpoint=checkpoint,
    )

    outcome = await graph_resume_coordinator.resume_graph_engine(pool, session, parked)

    assert outcome == "ENDED:completed"
    assert sorted(pool.agent_tool_result_event_keys) == sorted([ASK_A, ASK_B])


@pytest.mark.asyncio
async def test_a_key_less_drain_still_resumes_by_the_raw_id(monkeypatch) -> None:
    """The legacy single-event drain (no accumulated reply) carries no fired key: it keeps selecting by the raw id, as before."""
    checkpoint, first = await _two_parked_siblings(monkeypatch, _ask_user)
    pool = _FakePool(storage=_StorageProvider(), workspace_io=_WorkspaceIO(), executor_factory=_mk_parallel_executor)
    session = _session()
    session.parked_state = {"resume_event_key": ASK_B}
    parked = ParkedState(
        yielded=first.yielded, llm_messages=[], turn_no=0, started_at=datetime.now(UTC), tool_call_id=first.tool_call_id,
        resume_event_payload={"response": "blue"}, graph_checkpoint=checkpoint,
    )

    await graph_resume_coordinator.resume_graph_engine(pool, session, parked)

    assert pool.agent_tool_result_event_keys == [ASK_B], "the singular path also carries the fired key now"


# ---- the executor ------------------------------------------------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_executor_resumes_only_the_entry_whose_event_key_fired(monkeypatch) -> None:
    checkpoint, _first = await _two_parked_siblings(monkeypatch, _approval)
    ex = await _mk_parallel_executor()
    answer = Message(role="tool", parts=[ToolResultPart(id=RAW, output="approved")])

    with pytest.raises(YieldToWorker) as repark:
        async for _ev in ex.resume_from_checkpoint(checkpoint, resumed_tcid=RAW, resumed_event_key=APPROVAL_B, agent_tool_result=answer):
            pass

    assert _pending_nodes(repark.value) == ["A"]


@pytest.mark.asyncio
async def test_the_executor_falls_back_to_the_raw_id_when_the_fired_key_names_no_entry(monkeypatch) -> None:
    """A key from a park written before event keys were node-scoped may match nothing: the raw id decides, as it always did (every entry that has it)."""
    checkpoint, _first = await _two_parked_siblings(monkeypatch, _approval)
    ex = await _mk_parallel_executor()
    answer = Message(role="tool", parts=[ToolResultPart(id=RAW, output="approved")])

    async for _ev in ex.resume_from_checkpoint(checkpoint, resumed_tcid=RAW, resumed_event_key="tool_approval:gs-1:legacy", agent_tool_result=answer):
        pass          # both resumed, the graph ran to its end: no YieldToWorker


@pytest.mark.asyncio
async def test_the_executor_without_a_fired_key_resumes_only_the_first_entry_with_the_raw_id(monkeypatch) -> None:
    """A raw id with no key cannot say which sibling it answers: only the first entry that carries it (the primary the park projects) is resumed, the other
    stays pending. It used to resume every entry with the raw id, so one reply drained both (#683 review, S-B)."""
    checkpoint, _first = await _two_parked_siblings(monkeypatch, _approval)
    ex = await _mk_parallel_executor()
    answer = Message(role="tool", parts=[ToolResultPart(id=RAW, output="approved")])

    with pytest.raises(YieldToWorker) as repark:
        async for _ev in ex.resume_from_checkpoint(checkpoint, resumed_tcid=RAW, agent_tool_result=answer):
            pass

    assert _pending_nodes(repark.value) == ["B"]


# ---- the gate and the audit record ------------------------------------------------------------------------------------------------------


def _entry(node: str, gid: str) -> dict:
    return {
        "node_id": node, "tool_call_id": RAW, "event_key": f"tool_approval:gs-1:{node}:{RAW}", "tool_name": "_approval",
        "resume_metadata": {"policy_id": "pol", "approval_type": "required", "gate_reason": "m", "approvers": None, "gate_id": gid,
                            "original_call": {"id": RAW, "name": "delete_workspace", "arguments": {"id": node}}},
        "llm_messages": [], "iteration": 0, "frames": [], "leaf": None,
    }


def _checkpoint() -> dict:
    return {"pending_toolcalls": [], "pending_agent_yields": [_entry("A", GA), _entry("B", GB)], "pending_dispatch": []}


def test_a_gate_is_resolved_by_the_event_key_that_fired() -> None:
    gate = resolve_pending_gate({"graph_checkpoint": _checkpoint()}, tool_call_id=RAW, kind="_approval", event_key=APPROVAL_B)

    assert gate is not None and gate["node_id"] == "B"


def test_an_event_key_that_names_no_gate_falls_back_to_the_raw_id() -> None:
    gate = resolve_pending_gate({"graph_checkpoint": _checkpoint()}, tool_call_id=RAW, kind="_approval", event_key="tool_approval:gs-1:elsewhere")

    assert gate is not None and gate["node_id"] == "A", "the first match, as before"


@pytest.mark.asyncio
async def test_the_resume_time_audit_record_is_written_for_the_gate_that_was_decided() -> None:
    """The reviewer's record-fallback probe: B was decided, and the record must be written under B's gate key (it was written under A's)."""
    sp = _FakeStorageProvider()
    pool = SimpleNamespace(_storage=sp)
    session = SimpleNamespace(id="gs-1", binding=SimpleNamespace(agent_id="agt"), parked_at=None)

    await write_approval_record_for_graph(
        pool, session=session, checkpoint=_checkpoint(), tcid=RAW,
        payload={"decision": "approved", "reason": None, "decided_by": "x"}, event_key=APPROVAL_B,
    )

    keys = [r.gate_event_key for r in (await sp.get_storage(ToolApprovalRecord).list(OffsetPage(offset=0, length=10))).items]
    assert keys == [APPROVAL_B], keys      # the event key of the decided gate (the record key carries the gate id once the record key change lands)


# ---- ask_user inside a graph node is scoped by the node, as an approval is --------------------------------------------------------------------


@pytest.mark.asyncio
async def test_an_ask_user_inside_a_graph_node_carries_the_node_in_its_event_key() -> None:
    from primer.graph._node_identity import reset_current_graph_node_id, set_current_graph_node_id
    from primer.model.yield_ import ToolContext
    from primer.toolset._system_tools import _ask_user_handler

    ctx = ToolContext(tool_call_id=RAW, session_id="gs-1", workspace_id="ws-1")
    token = set_current_graph_node_id("worker[1]")
    try:
        yielded = await _ask_user_handler({"prompt": "q"}, ctx=ctx)
    finally:
        reset_current_graph_node_id(token)

    assert yielded.event_key == f"ask_user:gs-1:worker[1]:{RAW}"


@pytest.mark.asyncio
async def test_an_ask_user_outside_a_graph_node_keeps_its_event_key() -> None:
    from primer.model.yield_ import ToolContext
    from primer.toolset._system_tools import _ask_user_handler

    yielded = await _ask_user_handler({"prompt": "q"}, ctx=ToolContext(tool_call_id=RAW, session_id="gs-1", workspace_id="ws-1"))

    assert yielded.event_key == f"ask_user:gs-1:{RAW}"
