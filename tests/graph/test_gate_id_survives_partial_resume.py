"""Deciding one gate of a multi-gate graph park leaves every other pending gate's ``gate_id`` alone (C-033, Lead ruling 1).

The fence token of a gate cannot be the session's ``parked_at``: a graph superstep can park on several gates at once, and answering one of them
re-parks the graph on the remaining ones with a FRESH ``parked_at`` (``repark_graph_outcome`` stamps ``now``). A token of ``parked_at`` would make
the card of every sibling still pending read as stale the moment its neighbour was decided. The token is a ``gate_id`` stored in the gate's own
pending entry, which the executor carries across the re-park verbatim.

This pins both halves with the real executor and the real re-park builder: the sibling's ``gate_id`` is identical after the re-park, and the
park's ``parked_at`` is not (which is why it is not the token).
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from primer.model.chat import Message, ToolResultPart
from primer.model.yield_ import Yielded, YieldToWorker
from primer.worker.graph_resume_coordinator import repark_graph_outcome

from tests.graph.test_tool_wait_graph_park import _mk_parallel_executor, _patch_run_agent_turn

GATE_A = "a" * 32
GATE_B = "b" * 32


def _approval_yield(node_id: str, tcid: str, gate_id: str) -> YieldToWorker:
    return YieldToWorker(
        Yielded(
            tool_name="_approval", event_key=f"tool_approval:s:{node_id}:{tcid}",
            resume_metadata={
                "policy_id": "pol", "approval_type": "required", "gate_reason": "matched policy", "approvers": None, "gate_id": gate_id,
                "original_call": {"id": tcid, "name": "delete_workspace", "arguments": {}},
            },
        ),
        tool_call_id=tcid,
        llm_messages=[{"role": "assistant", "parts": [{"type": "text", "text": f"{node_id} asking"}]}],
    )


def _gate_ids(checkpoint: dict) -> dict[str, str]:
    return {e["tool_call_id"]: e["resume_metadata"]["gate_id"] for e in checkpoint["pending_agent_yields"]}


@pytest.mark.asyncio
async def test_a_sibling_gate_keeps_its_gate_id_while_the_park_is_restamped(monkeypatch) -> None:
    ex = await _mk_parallel_executor()
    _patch_run_agent_turn(monkeypatch, {
        "agent-a": _approval_yield("A", "tc-a", GATE_A),
        "agent-b": _approval_yield("B", "tc-b", GATE_B),
    })
    with pytest.raises(YieldToWorker) as first:
        async for _ev in ex.invoke([]):
            pass
    assert _gate_ids(first.value.graph_checkpoint) == {"tc-a": GATE_A, "tc-b": GATE_B}

    # the operator decides A; B is still pending, so the graph re-parks on B alone
    answer = Message(role="tool", parts=[ToolResultPart(id="tc-a", output="approved")])
    with pytest.raises(YieldToWorker) as second:
        async for _ev in ex.resume_from_checkpoint(first.value.graph_checkpoint, resumed_tcid="tc-a", agent_tool_result=answer):
            pass

    assert _gate_ids(second.value.graph_checkpoint) == {"tc-b": GATE_B}          # B's card is still the current one

    session = SimpleNamespace(turn_no=1)
    before = repark_graph_outcome(None, session, first.value).park
    after = repark_graph_outcome(None, session, second.value).park
    assert after.parked_at != before.parked_at                                   # why parked_at cannot be the token
    assert _gate_ids(after.parked_state["graph_checkpoint"]) == {"tc-b": GATE_B}
