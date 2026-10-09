"""An AGENT session parked on a child graph with two sibling gates resumes the sibling that was ANSWERED, not the child's primary (console review C-033, #683 review round 2, B1; ticket 01a11fc6-0cce).

An agent session that invoked a child graph (``system__invoke_graph``) parks on the child's PRIMARY projection (``parked_state['yielded']`` is the child's first
pending entry), with a ``GraphFrame`` on the frame stack and every pending key in ``parked_event_keys``. The pending list, the respond routes and the channel
prompts offer EVERY sibling, so a reply naming B's gate flips the row on B's own key and stamps ``resume_event_key`` / ``resume_event_payload`` with B's. The
resume used to key the child's resume on the leaf, A, so B's approval ran A's gated call (A's approvers were never asked), B's ask_user answer reached A, and
the resume-time audit record named A. The FIRED key from the row now rides ``resume_continuation(fired_key=)`` -> ``GraphFrame.resume_leaf(fired_key=)`` and
selects the entry that was answered: its tool_call_id, the approval-gate check and the record all come from that entry.

The shape is the one production parks: the REAL ``run_invoke_graph`` produces the park, the REAL ``resume_engine_session`` and continuation walk resume it,
over a real child executor.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

import primer.toolset.misc  # noqa: F401  (registers the ask_user resume hook)
from primer.graph.invoke_graph import GraphInvocationServices, run_invoke_graph
from primer.model.storage import OffsetPage
from primer.model.tool_approval import ToolApprovalRecord
from primer.model.workspace_session import AgentSessionBinding, SessionStatus, WorkspaceSession
from primer.model.yield_ import Yielded, YieldToWorker
from primer.session.approvers import may_decide
from primer.session.pending_gates import enumerate_pending_gates, resolve_pending_gate
from primer.session.yields import durably_mark_session_resumable
from primer.worker import graph_resume_coordinator as grc
from primer.worker import session_resume_coordinator as src
from primer.worker.frames import GraphFrame
from primer.worker.yield_runtime import ParkedState
from tests.conftest import _FakeStorageProvider
from tests.graph.test_tool_wait_graph_park import _mk_parallel_executor, _patch_run_agent_turn

SID = "s-agent"
GA, GB = "a" * 32, "b" * 32


def _yield(kind: str, node: str, gid: str, tcid: str, approvers=None) -> YieldToWorker:
    if kind == "approval":
        y = Yielded(
            tool_name="_approval", event_key=f"tool_approval:{SID}:{node}:{tcid}",
            resume_metadata={"policy_id": "pol", "approval_type": "required", "gate_reason": "m", "approvers": approvers, "gate_id": gid,
                             "original_call": {"id": tcid, "name": "delete_workspace", "arguments": {"id": node}}},
        )
    else:
        y = Yielded(tool_name="ask_user", event_key=f"ask_user:{SID}:{node}:{tcid}", resume_metadata={"prompt": f"question of {node}", "gate_id": gid})
    return YieldToWorker(y, tool_call_id=tcid, llm_messages=[{"role": "assistant", "parts": [{"type": "text", "text": f"{node} asking"}]}])


class _Bus:
    async def publish(self, key, payload):
        return None


class _Pool:
    """The slice of WorkerPool resume_engine_session uses; the invocation services are the REAL build_invocation_services."""

    def __init__(self, sp) -> None:
        self._storage = sp
        self._event_bus = _Bus()
        self._provider_registry = None
        self._approval_resolver = None
        self.reparked = None
        self.ended: list[str] = []

    async def _end_session(self, session, *, reason):
        self.ended.append(reason)
        return f"ENDED:{reason}"

    async def _load_workspace_for_persist(self, workspace_id):
        return object()

    async def _build_agent_executor(self, session, workspace):
        async def resolve_graph(_gid):
            return None

        async def build_child_executor(*, graph, gsid):
            return await _mk_parallel_executor()

        return SimpleNamespace(_tool_manager=SimpleNamespace(_graph_services=SimpleNamespace(
            resolve_graph=resolve_graph, build_child_executor=build_child_executor)))

    def _build_invocation_services(self, session, workspace, executor, tool_manager):
        return src.build_invocation_services(self, session, workspace, executor, tool_manager)

    async def _graph_agent_tool_result(self, checkpoint, tcid, payload, *, session_id, event_key=None):
        return await grc.graph_agent_tool_result(self, checkpoint, tcid, payload, session_id=session_id, event_key=event_key)

    async def _write_approval_record_for_session(self, *, session, blob, payload):
        return await src.write_approval_record_for_session(self, session=session, blob=blob, payload=payload)

    def _repark_continuation(self, session, parked, outcome):
        self.reparked = outcome
        return src.repark_continuation(self, session, parked, outcome)

    async def _inject_resume_and_continue(self, session, executor, parked, tool_result_part):
        return "CONTINUED"


def _key_prefix(kind: str) -> str:
    return "tool_approval" if kind == "approval" else "ask_user"


async def _answer(
    monkeypatch, kind: str, payload: dict, *, kind_b: str | None = None, answered: str = "B", tcid_a="call_0", tcid_b="call_0", approvers_a=None, approvers_b=None,
    fired: bool = True,
):
    """``kind`` is A's gate kind (``approval`` or ``ask_user``) and, unless ``kind_b`` says otherwise, B's."""
    kind_b = kind_b or kind
    _patch_run_agent_turn(monkeypatch, {"agent-a": _yield(kind, "A", GA, tcid_a, approvers_a), "agent-b": _yield(kind_b, "B", GB, tcid_b, approvers_b)})

    async def _resolve_graph(_gid):
        return None

    async def _build_child(*, graph, gsid):
        return await _mk_parallel_executor()

    # The REAL run_invoke_graph (the agent's system__invoke_graph): the child parks, the frame is pushed, the child's yield is re-raised.
    services = GraphInvocationServices(resolve_graph=_resolve_graph, build_child_executor=_build_child, session_id=SID, workspace_id="ws-1",
                                       graph_session_id=SID)
    with pytest.raises(YieldToWorker) as first:
        await run_invoke_graph(graph_id="child", graph_input="go", services=services, tool_call_id="outer")
    park = first.value
    a_key, b_key = f"{_key_prefix(kind)}:{SID}:A:{tcid_a}", f"{_key_prefix(kind_b)}:{SID}:B:{tcid_b}"
    # dispatch.py's except-YieldToWorker park, field for field: yielded (stamped), graph_checkpoint, frames, every key.
    now = datetime.now(timezone.utc) - timedelta(seconds=5)
    yielded = Yielded(tool_name=park.yielded.tool_name, event_key=park.yielded.event_key, timeout=park.yielded.timeout,
                      resume_metadata={**park.yielded.resume_metadata, "parked_at_iso": now.isoformat()}, event_keys=park.yielded.event_keys)
    assert yielded.event_key == a_key and sorted(yielded.event_keys) == sorted([a_key, b_key]), (yielded.event_key, yielded.event_keys)
    assert isinstance(park.frames[-1], GraphFrame) and park.graph_checkpoint is not None
    blob = ParkedState(yielded=yielded, llm_messages=[], turn_no=0, started_at=now, tool_call_id=park.tool_call_id,
                       graph_checkpoint=getattr(park, "graph_checkpoint", None), frames=list(park.frames)).to_jsonable()
    # The pending list and the respond routes see BOTH siblings; B's gate id resolves B's own key.
    assert [g["node_id"] for g in enumerate_pending_gates(blob)] == ["A", "B"]
    fired_key, fired_tcid, fired_gid = (b_key, tcid_b, GB) if answered == "B" else (a_key, tcid_a, GA)
    fired_kind = kind if answered == "A" else kind_b
    gate = resolve_pending_gate(blob, tool_call_id=fired_tcid, kind="_approval" if fired_kind == "approval" else "ask_user", gate_id=fired_gid)
    assert gate is not None and gate["event_key"] == fired_key, gate
    sp = _FakeStorageProvider()
    store = sp.get_storage(WorkspaceSession)
    row = WorkspaceSession(
        id=SID, workspace_id="ws-1", binding=AgentSessionBinding(kind="agent", agent_id="ag-1"), status=SessionStatus.RUNNING,
        created_at=now, turn_no=0, parked_status="parked", parked_event_key=a_key, parked_event_keys=[a_key, b_key],
        parked_until=now + timedelta(seconds=600), parked_at=now, parked_state=blob,
    )
    await store.create(row)
    assert await durably_mark_session_resumable(row, event_key=fired_key, payload=payload, session_storage=store, engine=None)
    if not fired:                                                                      # a row written before the fired key was stamped
        flipped = await store.get(SID)
        state = dict(flipped.parked_state)
        state.pop("resume_event_key", None)
        await store.update(flipped.model_copy(update={"parked_state": state}))
    pool = _Pool(sp)
    out = await src.resume_engine_session(pool, None, await store.get(SID))  # type: ignore[arg-type]
    assert pool.reparked is not None, f"no re-park: {out!r} ended={pool.ended}"
    still = [e["node_id"] for e in pool.reparked.frames[-1].checkpoint["pending_agent_yields"]]
    records = [(r.gate_event_key, r.arguments, r.decision) for r in
               (await sp.get_storage(ToolApprovalRecord).list(OffsetPage(offset=0, length=10))).items]
    return still, records, a_key, b_key, gate


APPROVED = {"decision": "approved", "reason": None, "decided_by": "op"}


@pytest.mark.asyncio
async def test_an_approval_of_the_non_primary_child_sibling_decides_that_sibling(monkeypatch) -> None:
    still, _records, _a, _b, _g = await _answer(monkeypatch, "approval", APPROVED)
    assert still == ["A"], f"B's approval resumed A (A's gated call ran on B's approval) and B is asked again: pending {still}"


@pytest.mark.asyncio
async def test_the_resume_time_record_of_a_non_primary_child_sibling_names_that_sibling(monkeypatch) -> None:
    _still, records, a_key, b_key, _g = await _answer(monkeypatch, "approval", APPROVED)
    assert all(not key.startswith(a_key) for key, _args, _d in records), f"an audit record says A was approved: {records}"
    assert [key for key, _args, _d in records] == [f"{b_key}@{GB}"], records      # the gate's own event key, suffixed with its gate id (C-033 PR 2)
    assert [args for _key, args, _d in records] == [{"id": "B"}], "the record describes B's gated call, not A's"


@pytest.mark.asyncio
async def test_an_answer_to_the_non_primary_child_ask_user_reaches_that_sibling(monkeypatch) -> None:
    still, _records, _a, _b, _g = await _answer(monkeypatch, "ask_user", {"response": "B's answer"})
    assert still == ["A"], f"B's answer was delivered to A and B is asked again: pending {still}"


@pytest.mark.asyncio
async def test_an_ask_user_primary_beside_an_answered_approval_sibling_writes_one_record_under_that_siblings_key(monkeypatch) -> None:
    """The leaf (A) is an ``ask_user``, whose answer is not a decision, and the reply decides B's approval: judging the reply by the leaf wrote no record at all
    for the approval that ran. The record is built from, and the gate check is judged on, the entry that was answered."""
    still, records, a_key, b_key, _g = await _answer(monkeypatch, "ask_user", APPROVED, kind_b="approval")

    assert still == ["A"], f"B's approval resumed A: pending {still}"
    assert [key for key, _args, _d in records] == [f"{b_key}@{GB}"], records
    assert [args for _key, args, _d in records] == [{"id": "B"}] and [d for _key, _args, d in records] == ["approved"]


@pytest.mark.asyncio
async def test_an_approval_primary_beside_an_answered_ask_user_sibling_writes_no_record(monkeypatch) -> None:
    """The mirror: the leaf (A) is an approval gate and the reply is B's ``ask_user`` answer. It is not a decision, so no approval record is written for it
    (judged on the leaf it would have been recorded as a REJECTED approval of A)."""
    still, records, _a, _b, _g = await _answer(monkeypatch, "approval", {"response": "B's answer"}, kind_b="ask_user")

    assert still == ["A"], f"B's answer resumed A: pending {still}"
    assert records == [], f"an operator's answer was recorded as an approval decision: {records}"


@pytest.mark.asyncio
async def test_distinct_raw_ids_too(monkeypatch) -> None:
    """Not a raw-id collision: with call_a / call_b the agent-session GraphFrame path used to ignore the fired key as well."""
    still, _records, _a, _b, _g = await _answer(monkeypatch, "approval", APPROVED, tcid_a="call_a", tcid_b="call_b")
    assert still == ["A"], f"B's approval resumed A although the ids differ: pending {still}"


@pytest.mark.asyncio
async def test_a_sibling_answer_does_not_cross_approver_routing(monkeypatch) -> None:
    """A is routed to alice only, B is open. bob may decide B (the respond route judges the RESOLVED gate, B); the resume must not run A."""
    still, _records, _a, _b, gate = await _answer(monkeypatch, "approval", APPROVED, approvers_a={"kind": "users", "users": ["alice"]})
    a_meta = {"approvers": {"kind": "users", "users": ["alice"]}}
    assert may_decide(gate["resume_metadata"], username="bob", role="user") is True
    assert may_decide(a_meta, username="bob", role="user") is False
    assert still == ["A"], f"bob's approval of the open gate B ran A, which only alice may approve: pending {still}"


# ---- the controls: the primary, and a row with no fired key -----------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_an_approval_of_the_primary_child_sibling_still_decides_the_primary(monkeypatch) -> None:
    still, records, a_key, _b, _g = await _answer(monkeypatch, "approval", APPROVED, answered="A")

    assert still == ["B"], f"A's approval left {still} pending"
    assert [key for key, _args, _d in records] == [f"{a_key}@{GA}"] and [args for _key, args, _d in records] == [{"id": "A"}]


@pytest.mark.asyncio
async def test_a_row_without_a_fired_key_resumes_the_leaf_as_it_always_did(monkeypatch) -> None:
    """A row flipped before ``resume_event_key`` was stamped (an older build): the leaf, the child's primary, is what is resumed."""
    still, _records, _a, _b, _g = await _answer(monkeypatch, "approval", APPROVED, answered="A", fired=False)

    assert still == ["B"]
