"""The gate id reaches every projection of a graph park a client reads (console review C-033, ticket 01a11f52-9d98).

A graph park holds several gates at once, and only the first is projected onto the top-level ``yielded`` blob (the Inbox row reads nothing else). The
per-gate lists the REST listers and the channel prompts read are ``pending_toolcalls`` / ``pending_agent_yields`` (the entries' own ``resume_metadata``)
and ``pending_dispatch`` (the denormalised entry a channel prompt is built from). Each must carry the ``gate_id`` the gate was minted with, or a
decision made from that projection could not name the gate it answers. These pin the three, on the real executor: the primary projection for an
approval and for an agent-node ``ask_user``, the ``pending_dispatch`` entry for an approval and for a value-yielding tool_call (``ask_user``), and
the snapshot that is checkpointed.
"""

from __future__ import annotations

import pytest

from primer.graph._node_refs import _PendingToolCall
from primer.model.yield_ import Yielded, YieldToWorker

from tests.graph.test_tool_wait_graph_park import _mk_parallel_executor, _patch_run_agent_turn

GATE_A = "a" * 32
GATE_B = "b" * 32


def _approval_pending(node_id: str, tcid: str, gate_id: str) -> _PendingToolCall:
    return _PendingToolCall(
        node_id=node_id, tool_call_id=tcid, parked_event_key=f"tool_approval:s:{tcid}", arguments={"id": "ws-1"}, tool_name="_approval",
        resume_metadata={
            "policy_id": "pol", "approval_type": "required", "gate_reason": "matched policy", "approvers": None, "gate_id": gate_id,
            "original_call": {"id": tcid, "name": "delete_workspace", "arguments": {"id": "ws-1"}},
        },
    )


def _ask_user_pending(node_id: str, tcid: str, gate_id: str) -> _PendingToolCall:
    return _PendingToolCall(
        node_id=node_id, tool_call_id=tcid, parked_event_key=f"ask_user:s:{tcid}", arguments={}, tool_name="ask_user",
        resume_metadata={"prompt": "which?", "response_schema": None, "gate_id": gate_id},
    )


@pytest.mark.asyncio
async def test_the_primary_projection_of_an_approval_carries_its_gate_id() -> None:
    ex = await _mk_parallel_executor()
    ex._pending_toolcalls = [_approval_pending("agent-a", "tc-a", GATE_A), _approval_pending("agent-b", "tc-b", GATE_B)]

    yld = ex._build_pending_park_yield()

    assert yld.yielded.resume_metadata["gate_id"] == GATE_A, "the Inbox row reads only the top-level yield"
    assert yld.tool_call_id == "tc-a"


@pytest.mark.asyncio
async def test_the_pending_dispatch_entry_of_an_approval_carries_its_gate_id() -> None:
    ex = await _mk_parallel_executor()
    p = _approval_pending("agent-a", "tc-a", GATE_A)

    entry = ex._toolcall_dispatch_entry(p)

    assert entry["kind"] == "_approval" and entry["resume_metadata"]["gate_id"] == GATE_A
    assert "original_call" in entry["resume_metadata"], "the channel prompt is still built from the denormalised call"


@pytest.mark.asyncio
async def test_the_pending_dispatch_entry_of_a_value_yielding_tool_call_carries_its_gate_id() -> None:
    ex = await _mk_parallel_executor()
    p = _ask_user_pending("agent-a", "tc-q", GATE_A)

    entry = ex._toolcall_dispatch_entry(p)

    assert entry["kind"] == "ask_user" and entry["resume_metadata"]["gate_id"] == GATE_A
    assert entry["resume_metadata"]["prompt"] == "which?"


@pytest.mark.asyncio
async def test_the_checkpointed_snapshot_carries_each_gates_id_in_every_list() -> None:
    ex = await _mk_parallel_executor()
    ex._pending_toolcalls = [_approval_pending("agent-a", "tc-a", GATE_A), _ask_user_pending("agent-b", "tc-q", GATE_B)]

    snap = ex.snapshot_state()

    assert [e["resume_metadata"]["gate_id"] for e in snap["pending_toolcalls"]] == [GATE_A, GATE_B]
    assert [e["resume_metadata"]["gate_id"] for e in snap["pending_dispatch"]] == [GATE_A, GATE_B]


@pytest.mark.asyncio
async def test_the_primary_projection_of_an_agent_node_ask_user_carries_its_gate_id(monkeypatch) -> None:
    ex = await _mk_parallel_executor()
    _patch_run_agent_turn(monkeypatch, {
        "agent-a": YieldToWorker(
            Yielded(tool_name="ask_user", event_key="ask_user:s:tc-a", resume_metadata={"prompt": "q", "gate_id": GATE_A}),
            tool_call_id="tc-a", llm_messages=[{"role": "assistant", "parts": [{"type": "text", "text": "asking"}]}],
        ),
        "agent-b": YieldToWorker(
            Yielded(tool_name="ask_user", event_key="ask_user:s:tc-b", resume_metadata={"prompt": "q", "gate_id": GATE_B}),
            tool_call_id="tc-b", llm_messages=[{"role": "assistant", "parts": [{"type": "text", "text": "asking"}]}],
        ),
    })

    with pytest.raises(YieldToWorker) as parked:
        async for _ev in ex.invoke([]):
            pass

    assert parked.value.yielded.resume_metadata["gate_id"] == GATE_A
    ay = parked.value.graph_checkpoint["pending_agent_yields"]
    assert {e["tool_call_id"]: e["resume_metadata"]["gate_id"] for e in ay} == {"tc-a": GATE_A, "tc-b": GATE_B}
