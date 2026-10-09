"""The SESSION resume fallback keys its record by the gate the decision fired, not by the top-level projection (C-033 PR 2, #673 review round 2, P6).

An agent session parked inside ``invoke_graph`` on a child superstep with two approval gates (A primary, alice only; B open): the park's top-level ``yielded`` is the
child's primary (A) but the pending list offers both. bob decides B (``_publish_decision``, the respond route's own helper); ``write_approval_record_for_session`` is
what ``resume_engine_session`` calls on this park. The record must be B's only, and when alice later decides A (still pending) her respond-time record must not be
dropped by the unique index as a duplicate of a record that never described her decision. Real SQLite unique index.
"""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from primer.api.routers.tool_approval import ToolApprovalRespondBody, _publish_decision
from primer.bus.in_memory import InMemoryEventBus
from primer.graph.invoke_graph import GraphInvocationServices, run_invoke_graph
from primer.model.chat import Message, ToolCallPart
from primer.model.provider import SqliteConfig
from primer.model.storage import OffsetPage
from primer.model.tool_approval import ToolApprovalRecord
from primer.model.workspace_session import AgentSessionBinding, SessionStatus, WorkspaceSession
from primer.model.yield_ import Yielded, YieldToWorker
from primer.session.approvers import may_decide
from primer.session.pending_gates import resolve_pending_gate
from primer.storage.sqlite import SqliteStorageProvider
from primer.worker.session_resume_coordinator import write_approval_record_for_session
from tests.graph.test_tool_wait_graph_park import _mk_parallel_executor, _patch_run_agent_turn

SID = "ag-child-fallback"
GA, GB = "a" * 32, "b" * 32
ALICE_ONLY = {"kind": "users", "roles": [], "users": ["alice"]}


def _gate(node: str, raw: str, gid: str, approvers) -> YieldToWorker:
    return YieldToWorker(
        Yielded(
            tool_name="_approval", event_key=f"tool_approval:{SID}:{node}:{raw}",
            resume_metadata={"policy_id": "pol", "approval_type": "required", "gate_reason": "m", "approvers": approvers, "gate_id": gid,
                             "original_call": {"id": raw, "name": "delete_workspace", "arguments": {"id": f"ws-{node}"}}},
        ),
        tool_call_id=raw,
        llm_messages=[{"role": "assistant", "parts": [{"type": "text", "text": f"{node} asking"}]}],
    )


async def _agent_park(monkeypatch) -> WorkspaceSession:
    from primer.worker.yield_runtime import ParkedState

    child = await _mk_parallel_executor()
    _patch_run_agent_turn(monkeypatch, {"agent-a": _gate("A", "call_0", GA, ALICE_ONLY), "agent-b": _gate("B", "call_0", GB, None)})

    async def _resolve(gid):
        return {"id": gid}

    async def _build(*, graph, gsid):
        return child

    services = GraphInvocationServices(resolve_graph=_resolve, build_child_executor=_build, session_id=SID, workspace_id="ws", graph_session_id="gs")
    with pytest.raises(YieldToWorker) as caught:
        await run_invoke_graph(graph_id="child", graph_input="go", services=services, tool_call_id="outer-tc")
    park = caught.value
    parked_at = datetime.now(UTC)
    y = park.yielded
    metadata = {**y.resume_metadata, "parked_at_iso": parked_at.isoformat()}
    stamped = type(y)(tool_name=y.tool_name, event_key=y.event_key, timeout=y.timeout, resume_metadata=metadata, event_keys=getattr(y, "event_keys", None))
    tool_use = Message(role="assistant", parts=[ToolCallPart(id="outer-tc", name="system__invoke_graph", arguments={"graph_id": "child"})])
    state = ParkedState(
        yielded=stamped, llm_messages=[tool_use.model_dump(mode="json")], turn_no=0, started_at=parked_at,
        tool_call_id=park.tool_call_id, graph_checkpoint=park.graph_checkpoint, frames=list(park.frames),
    )
    return WorkspaceSession(
        id=SID, workspace_id="ws", binding=AgentSessionBinding(kind="agent", agent_id="agt"), status=SessionStatus.RUNNING,
        created_at=parked_at, turn_no=0, parked_status="parked", parked_at=parked_at, parked_event_key=y.event_key,
        parked_event_keys=list(y.event_keys or []) or None, parked_state=state.to_jsonable(),
    )


async def _decide(sp, bus, *, gate_id: str, by: str, role: str) -> None:
    ss = sp.get_storage(WorkspaceSession)
    row = await ss.get(SID)
    gate = resolve_pending_gate(row.parked_state, tool_call_id="call_0", kind="_approval", gate_id=gate_id)
    assert gate is not None and may_decide(gate["resume_metadata"], username=by, role=role), f"{by} may not decide {gate_id[:4]}"
    await _publish_decision(
        sess=row, id_str=SID, body=ToolApprovalRespondBody(tool_call_id="call_0", gate_id=gate_id, decision="approved"), gate=gate,
        event_bus=bus, session_storage=ss, engine=None, storage_provider=sp, decided_by=by,
    )


@pytest.mark.asyncio
async def test_the_session_fallback_records_the_gate_the_decision_fired(monkeypatch, tmp_path):
    sp = SqliteStorageProvider(SqliteConfig(path=str(tmp_path / "fallback.sqlite")))
    await sp.initialize()
    bus = InMemoryEventBus()
    await bus.initialize()
    try:
        ss = sp.get_storage(WorkspaceSession)
        park = await _agent_park(monkeypatch)
        await ss.create(park)

        await _decide(sp, bus, gate_id=GB, by="bob", role="user")
        row = await ss.get(SID)
        assert row.parked_state["resume_event_key"] == f"tool_approval:{SID}:B:call_0"
        await write_approval_record_for_session(
            SimpleNamespace(_storage=sp), session=row, blob=row.parked_state, payload=row.parked_state["resume_event_payload"],
        )
        records = sp.get_storage(ToolApprovalRecord)
        after_b = sorted((r.gate_event_key, r.decided_by) for r in (await records.list(OffsetPage(offset=0, length=50))).items)
        print("\nafter bob decides B (respond + session fallback):", after_b)

        # A is still pending (as a resume that selected B leaves it); alice, its only approver, decides it.
        await ss.update(park)
        await _decide(sp, bus, gate_id=GA, by="alice", role="user")
        after_a = sorted((r.gate_event_key, r.decided_by) for r in (await records.list(OffsetPage(offset=0, length=50))).items)
        print("after alice decides A (respond):", after_a)

        assert after_b == [(f"tool_approval:{SID}:B:call_0@{GB}", "bob")], "one decision of B wrote a record for A too"
        assert (f"tool_approval:{SID}:A:call_0@{GA}", "alice") in after_a, "alice's decision of A was dropped by the unique index"
    finally:
        await bus.aclose()
        await sp.aclose()
