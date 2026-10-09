"""An agent session parked on a child graph with two approval gates: bob decides the NON-primary gate (C-033 PR 2, #673 review round 2, P5; #683 round 3).

Gate A (the child's primary projection) is routed to alice only; gate B is open to anyone. The console, the Inbox and the channels offer both, and the REST respond
resolves either. bob is refused on A and accepted on B; the session resume then resumes B (not A: the fired key selects the entry), and the resume-time fallback
writes B's record only, under ``<event key>@<gate id>``. Real REST, real resume, once with a shared raw id and once with distinct ids.
"""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from primer.graph.invoke_graph import GraphInvocationServices, run_invoke_graph
from primer.model.chat import Message, ToolCallPart
from primer.model.storage import OffsetPage
from primer.model.tool_approval import ToolApprovalRecord
from primer.model.workspace_session import AgentSessionBinding, SessionStatus, WorkspaceSession
from primer.model.yield_ import Yielded, YieldToWorker
from primer.worker.session_resume_coordinator import resume_engine_session, write_approval_record_for_session
from primer.worker.yield_runtime import ParkedState
from tests.api.conftest import raw_client as client, app, fake_provider_registry  # noqa: F401
from tests.api.test_approver_routing import _login_user, _register_admin
from tests.graph.test_tool_wait_graph_park import _mk_parallel_executor, _patch_run_agent_turn

SID = "ag-child-2"
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


async def _agent_park(monkeypatch, raw_a: str, raw_b: str) -> WorkspaceSession:
    """The agent session row exactly as dispatch.py builds it from the YieldToWorker that run_invoke_graph re-raises."""
    child = await _mk_parallel_executor()
    _patch_run_agent_turn(monkeypatch, {"agent-a": _gate("A", raw_a, GA, ALICE_ONLY), "agent-b": _gate("B", raw_b, GB, None)})

    async def _resolve(gid):
        return {"id": gid}

    async def _build(*, graph, gsid):
        return child

    services = GraphInvocationServices(resolve_graph=_resolve, build_child_executor=_build, session_id=SID, workspace_id="ws", graph_session_id="gs")
    with pytest.raises(YieldToWorker) as caught:
        await run_invoke_graph(graph_id="child", graph_input="go", services=services, tool_call_id="outer-tc")
    park = caught.value
    assert park.graph_checkpoint is not None, "the re-raised child yield lost its checkpoint"
    parked_at = datetime.now(UTC)
    y = park.yielded
    metadata = dict(y.resume_metadata)
    metadata["parked_at_iso"] = parked_at.isoformat()
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


class _Services:
    def __init__(self, child) -> None:
        self._child = child
        self.session_id = SID
        self.resolve_provider = None

    async def resolve_graph(self, _graph_id):
        return None

    async def build_child_graph_executor(self, _graph, _gsid):
        return self._child

    async def graph_agent_tool_result(self, _checkpoint, _tcid, _payload, *, event_key=None):
        return None


class _Pool:
    """What resume_engine_session asks of the pool on the continuation path; the record write is the REAL one."""

    def __init__(self, storage, child) -> None:
        self._storage, self._child = storage, child
        self._event_bus = SimpleNamespace(publish=None)
        self.reparked = None

    async def _end_session(self, session, *, reason):
        return f"ENDED:{reason}"

    async def _load_workspace_for_persist(self, _ws):
        return object()

    async def _build_agent_executor(self, _s, _w):
        return SimpleNamespace(_tool_manager=None)

    def _build_invocation_services(self, *_a):
        return _Services(self._child)

    async def _write_approval_record_for_session(self, *, session, blob, payload):
        await write_approval_record_for_session(self, session=session, blob=blob, payload=payload)

    def _repark_continuation(self, session, parked, outcome):
        self.reparked = outcome
        return "REPARKED"

    async def _inject_resume_and_continue(self, *_a):
        return "CONTINUED"


@pytest.mark.asyncio
@pytest.mark.parametrize("raws", [("call_0", "call_0"), ("call_a", "call_b")], ids=["shared-raw-id", "distinct-raw-ids"])
async def test_bob_decides_the_non_primary_child_gate(monkeypatch, client, app, raws) -> None:
    raw_a, raw_b = raws
    sessions = app.state.storage_provider.get_storage(WorkspaceSession)
    await sessions.create(await _agent_park(monkeypatch, raw_a, raw_b))
    await _register_admin(client)
    await _login_user(client, app, "bob")

    on_a = await client.post(f"/v1/sessions/{SID}/tool_approval/respond", json={"tool_call_id": raw_a, "gate_id": GA, "decision": "approved"})
    on_b = await client.post(f"/v1/sessions/{SID}/tool_approval/respond", json={"tool_call_id": raw_b, "gate_id": GB, "decision": "approved"})
    row = await sessions.get(SID)
    records = app.state.storage_provider.get_storage(ToolApprovalRecord)
    after_respond = sorted((r.gate_event_key, r.decided_by) for r in (await records.list(OffsetPage(offset=0, length=50))).items)
    print("\nrespond A (bob, alice-only):", on_a.status_code, "| respond B (bob, anyone):", on_b.status_code)
    print("row.resume_event_key:", row.parked_state.get("resume_event_key"))
    print("records after respond:", after_respond)

    pool = _Pool(app.state.storage_provider, await _mk_parallel_executor())
    out = await resume_engine_session(pool, None, row)  # type: ignore[arg-type]
    after_resume = sorted((r.gate_event_key, r.decision, r.decided_by) for r in (await records.list(OffsetPage(offset=0, length=50))).items)
    still_pending = pool.reparked.leaf.event_key if pool.reparked is not None else None
    print("resume outcome:", out, "| gate still pending after the resume:", still_pending)
    print("records after resume:", after_resume)

    assert on_a.status_code == 403, "bob may not decide A"
    assert on_b.status_code == 202
    # A SET: the API test double enforces no unique index, so the respond-time row and the resume-time row of ONE gate are two rows here (they collapse on
    # a real index: tests/worker/test_approval_record_collapse_sqlite.py). What is pinned is WHICH gates have a record, and who decided them.
    assert set(after_resume) == {(f"tool_approval:{SID}:B:{raw_b}@{GB}", "approved", "bob")}, "the resume wrote a record for a gate nobody decided"
    assert still_pending == f"tool_approval:{SID}:A:{raw_a}", "B's decision resumed A (the alice-only gate), and B is still pending"
