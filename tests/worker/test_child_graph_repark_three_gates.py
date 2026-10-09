"""An agent session parked on a child graph with THREE gates keeps all of them after ONE is answered (C-033 round 4, from the #683 round 3 review).

Child graph: begin -> {T1, T2, A}. T1 and T2 are graph tool_call nodes gated by approval (their key is the unscoped ``tool_approval:{session}:{uuid}`` the tool
manager builds for a tool_call node, whose ctx.session_id is the agent session's: ``build_child_executor`` passes the agent session's workspace_session); A is
an agent node with a node-scoped approval key. The park projects the first tool_call (T1 or T2) as the primary.

Step 1: A (non-primary) is answered on its own key (what the REST respond does). The real resume resumes A and the child re-parks on [T1, T2] through
``repark_continuation``. That re-park used to write NO top-level ``graph_checkpoint`` and to carry the first park's ``tool_call_id``, so the routes saw only the
PROJECTION of the primary (which carries no ``approvers``) and the other gate was invisible: bob decided a gate routed to alice only, and an unidentified channel
click ran it. Step 2: a click on the OTHER tool_call gate (non-primary, Tnp), through the real ``ChannelInbox.handle_response`` and a bus that flips like the
listener. Step 3: the real resume again.

Checked: (a) with Tnp routed to alice only an unidentified channel click is refused and nothing flips; (b) with Tnp open, the resume-time record names the
gate that RAN; (c) the shape the re-park leaves is the first park's: the child's advanced checkpoint on the blob and the new primary's tool_call_id.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

import primer.toolset.misc  # noqa: F401
from primer.channel.adapter import ResponseEnvelope
from primer.channel.inbox import ChannelInbox
from primer.graph.executor import GraphExecutor
from primer.graph.invoke_graph import GraphInvocationServices, run_invoke_graph
from primer.model.agent import Agent, AgentModel
from primer.model.chat import ToolResultPart
from primer.model.graph import Graph, GraphNodeMessage, GraphThread, _AgentNodeRef, _BeginNode, _EndNode, _StaticEdge, _ToolCallNode
from primer.model.storage import OffsetPage
from primer.model.tool_approval import ToolApprovalRecord
from primer.model.workspace_session import AgentSessionBinding, SessionStatus, WorkspaceSession
from primer.model.yield_ import Yielded, YieldToWorker
from primer.session.approvers import may_decide
from primer.session.pending_gates import enumerate_pending_gates, resolve_pending_gate
from primer.session.yields import durably_mark_session_resumable, flip_sessions_parked_on
from primer.worker import graph_resume_coordinator as grc
from primer.worker import session_resume_coordinator as src
from primer.worker.yield_runtime import ParkedState
from tests.conftest import _FakeStorageProvider
from tests.graph.test_tool_wait_graph_park import _model, _patch_run_agent_turn, _UnusedLLM
from tests.graph.test_toolcall_dispatch import _InMemoryStorage

SID = "s-agent"
G = {"T1": "1" * 32, "T2": "2" * 32, "A": "a" * 32}
A_KEY = f"tool_approval:{SID}:A:call_a"


def _graph() -> Graph:
    return Graph(
        id="child", description="begin -> {T1, T2, A}",
        nodes=[
            _BeginNode(id="begin"),
            _ToolCallNode(id="T1", tool_id="workspace__delete_file", arguments={"path": "t1"}),
            _ToolCallNode(id="T2", tool_id="workspace__delete_file", arguments={"path": "t2"}),
            _AgentNodeRef(id="A", agent_id="agent-a"),
            _EndNode(id="E1"), _EndNode(id="E2"), _EndNode(id="E3"),
        ],
        edges=[
            _StaticEdge(from_node="begin", to_node="T1"), _StaticEdge(from_node="begin", to_node="T2"), _StaticEdge(from_node="begin", to_node="A"),
            _StaticEdge(from_node="T1", to_node="E1"), _StaticEdge(from_node="T2", to_node="E2"), _StaticEdge(from_node="A", to_node="E3"),
        ],
    )


class _World:
    def __init__(self, approvers: dict) -> None:
        self.approvers = approvers
        self.ran: list[str] = []

    async def dispatcher(self, node, arguments, bypass_approval=False):
        tcid = f"u-{node.id}"
        if not bypass_approval:
            raise YieldToWorker(Yielded(
                tool_name="_approval", event_key=f"tool_approval:{SID}:{tcid}",
                resume_metadata={"policy_id": "pol", "approval_type": "required", "gate_reason": "m", "approvers": self.approvers.get(node.id),
                                 "gate_id": G[node.id], "original_call": {"id": tcid, "name": node.tool_id, "arguments": dict(arguments)}},
            ), tool_call_id=tcid)
        self.ran.append(node.id)
        return ToolResultPart(id=tcid, output=f"ran {node.id}")

    async def build_child(self, *, graph=None, gsid=None):
        g = _graph()

        async def agent_resolver(agent_id):
            return Agent(id=agent_id, description=agent_id, model=AgentModel(profile_id="p--m"))

        async def llm_resolver(_agent):
            return (_UnusedLLM(), _model())

        ts = _InMemoryStorage(GraphThread)
        ms = _InMemoryStorage(GraphNodeMessage)
        thread = await GraphExecutor.open_thread(graph=g, thread_storage=ts)
        ex = GraphExecutor(graph=g, agent_resolver=agent_resolver, llm_resolver=llm_resolver, thread_storage=ts, message_storage=ms,
                           graph_thread_id=thread.id, tool_dispatcher=self.dispatcher)
        ex._tool_calls_as_claims_enabled = True
        return ex


class _Bus:
    async def publish(self, key, payload):
        return None


class _FlippingBus:
    def __init__(self, store) -> None:
        self.store, self.published, self.flipped = store, [], 0

    async def publish(self, event_key, payload=None):
        self.published.append(event_key)
        self.flipped += await flip_sessions_parked_on(event_key, payload or {}, session_storage=self.store, engine=None)


class _Pool:
    def __init__(self, sp, world) -> None:
        self._storage = sp
        self._event_bus = _Bus()
        self._provider_registry = None
        self._approval_resolver = None
        self.reparked = None
        self.release = None
        self.ended: list[str] = []
        self.world = world

    async def _end_session(self, session, *, reason):
        self.ended.append(reason)
        return f"ENDED:{reason}"

    async def _load_workspace_for_persist(self, workspace_id):
        return object()

    async def _build_agent_executor(self, session, workspace):
        async def resolve_graph(_gid):
            return None

        return SimpleNamespace(_tool_manager=SimpleNamespace(_graph_services=SimpleNamespace(
            resolve_graph=resolve_graph, build_child_executor=self.world.build_child)))

    def _build_invocation_services(self, session, workspace, executor, tool_manager):
        return src.build_invocation_services(self, session, workspace, executor, tool_manager)

    async def _graph_agent_tool_result(self, checkpoint, tcid, payload, *, session_id, event_key=None):
        import inspect
        kw = {"event_key": event_key} if "event_key" in inspect.signature(grc.graph_agent_tool_result).parameters else {}
        return await grc.graph_agent_tool_result(self, checkpoint, tcid, payload, session_id=session_id, **kw)

    async def _write_approval_record_for_session(self, *, session, blob, payload):
        return await src.write_approval_record_for_session(self, session=session, blob=blob, payload=payload)

    def _repark_continuation(self, session, parked, outcome):
        self.reparked = outcome
        self.release = src.repark_continuation(self, session, parked, outcome)
        return self.release

    async def _inject_resume_and_continue(self, session, executor, parked, tool_result_part):
        return "CONTINUED"


APPROVED = {"decision": "approved", "reason": None, "decided_by": "op"}


async def _scenario(monkeypatch, approvers: dict, answer_first: str = "A"):
    world = _World(approvers)
    _patch_run_agent_turn(monkeypatch, {"agent-a": YieldToWorker(Yielded(
        tool_name="_approval", event_key=A_KEY,
        resume_metadata={"policy_id": "pol", "approval_type": "required", "gate_reason": "m", "approvers": None, "gate_id": G["A"],
                         "original_call": {"id": "call_a", "name": "delete_workspace", "arguments": {"id": "A"}}},
    ), tool_call_id="call_a", llm_messages=[{"role": "assistant", "parts": [{"type": "text", "text": "A asking"}]}])})

    async def _resolve_graph(_gid):
        return None

    services = GraphInvocationServices(resolve_graph=_resolve_graph, build_child_executor=world.build_child, session_id=SID, workspace_id="ws-1",
                                       graph_session_id=SID)
    with pytest.raises(YieldToWorker) as first:
        await run_invoke_graph(graph_id="child", graph_input="go", services=services, tool_call_id="outer")
    park = first.value
    now = datetime.now(timezone.utc) - timedelta(seconds=5)
    keys = list(park.yielded.event_keys)
    primary_key = park.yielded.event_key
    primary = primary_key.rsplit(":", 1)[-1].removeprefix("u-")
    tnp = "T2" if primary == "T1" else "T1"
    yielded = Yielded(tool_name=park.yielded.tool_name, event_key=primary_key, timeout=park.yielded.timeout,
                      resume_metadata={**park.yielded.resume_metadata, "parked_at_iso": now.isoformat()}, event_keys=keys)
    blob = ParkedState(yielded=yielded, llm_messages=[], turn_no=0, started_at=now, tool_call_id=park.tool_call_id,
                       graph_checkpoint=park.graph_checkpoint, frames=list(park.frames)).to_jsonable()
    trace = {"primary": primary, "clicked": tnp, "first_park_listed": [g["node_id"] for g in enumerate_pending_gates(blob)],
             "first_park_tool_call_id": park.tool_call_id}
    sp = _FakeStorageProvider()
    store = sp.get_storage(WorkspaceSession)
    row = WorkspaceSession(
        id=SID, workspace_id="ws-1", binding=AgentSessionBinding(kind="agent", agent_id="ag-1"), status=SessionStatus.RUNNING,
        created_at=now, turn_no=0, parked_status="parked", parked_event_key=primary_key, parked_event_keys=keys,
        parked_until=now + timedelta(seconds=600), parked_at=now, parked_state=blob,
    )
    await store.create(row)
    # Step 1: A (non-primary, open) is answered on its own key, or (answer_first="primary") the primary is.
    first_key = A_KEY if answer_first == "A" else primary_key
    if answer_first != "A":
        tnp = "T2" if primary == "T1" else "T1"            # after the primary is answered the OTHER tool_call becomes the new primary
        trace["clicked"] = tnp
    assert await durably_mark_session_resumable(row, event_key=first_key, payload=APPROVED, session_storage=store, engine=None)
    pool = _Pool(sp, world)
    await src.resume_engine_session(pool, None, await store.get(SID))  # type: ignore[arg-type]
    assert pool.release is not None and pool.release.park is not None, f"no re-park after A: ended={pool.ended}"
    trace["ran_after_A"] = list(world.ran)
    p = pool.release.park
    cur = await store.get(SID)
    await store.update(cur.model_copy(update={
        "parked_status": "parked", "parked_event_key": p.parked_event_key, "parked_event_keys": p.parked_event_keys,
        "parked_until": p.parked_until, "parked_at": p.parked_at, "parked_state": p.parked_state,
    }))
    reparked = await store.get(SID)
    trace["repark_has_top_level_checkpoint"] = bool((reparked.parked_state or {}).get("graph_checkpoint"))
    trace["repark_top_tool_call_id"] = (reparked.parked_state or {}).get("tool_call_id")
    trace["repark_listed"] = [(g["tool_call_id"], g["event_key"]) for g in enumerate_pending_gates(reparked.parked_state)]
    trace["repark_keys"] = reparked.parked_event_keys
    # What the REST respond route would do now (tool_approval.py: resolve_pending_gate, then enforce_approvers on THAT entry's metadata).
    rest = {}
    for node in ("T1", "T2"):
        g = resolve_pending_gate(reparked.parked_state, tool_call_id=f"u-{node}", kind="_approval", gate_id=G[node])
        rest[node] = None if g is None else {"event_key": g["event_key"], "has_approvers_key": "approvers" in (g.get("resume_metadata") or {}),
                                              "bob_may_decide": may_decide(g.get("resume_metadata"), username="bob", role="user")}
    trace["rest_after_repark"] = rest
    trace["_reparked_row"] = reparked
    # Step 2: a channel click on the OTHER tool_call gate (Tnp), through the real inbox.
    bus = _FlippingBus(store)
    inbox = ChannelInbox(event_bus=bus, storage_provider=sp)
    refused = None
    try:
        await inbox.handle_response(ResponseEnvelope(kind="tool_approval", workspace_id="ws-1", session_id=SID, tool_call_id=f"u-{tnp}",
                                                     response=None, decision="approved", reason=None, platform_metadata={"slack_user_id": "U1"},
                                                     gate_id=G[tnp]))
    except Exception as exc:  # noqa: BLE001
        refused = type(exc).__name__
    trace["click_refused"] = refused
    trace["click_published"] = bus.published
    trace["click_flipped"] = bus.flipped
    ran_before = len(world.ran)
    if bus.flipped:
        flipped_row = await store.get(SID)
        trace["fired_key"] = (flipped_row.parked_state or {}).get("resume_event_key")
        pool2 = _Pool(sp, world)
        await src.resume_engine_session(pool2, None, flipped_row)  # type: ignore[arg-type]
    trace["ran_after_click"] = world.ran[ran_before:]
    recs = (await sp.get_storage(ToolApprovalRecord).list(OffsetPage(offset=0, length=20))).items
    trace["records"] = [(r.gate_event_key, r.arguments, r.decision, r.decided_by) for r in recs]
    return trace


@pytest.mark.asyncio
async def test_a_click_on_a_restricted_child_gate_after_a_sibling_answer_is_refused(monkeypatch) -> None:
    trace = await _scenario(monkeypatch, {"T1": {"kind": "users", "users": ["alice"]}, "T2": {"kind": "users", "users": ["alice"]}})
    assert trace["click_refused"] == "ApproverRefusedError" and trace["click_flipped"] == 0 and trace["ran_after_click"] == [], (
        f"an unidentified channel click decided a gate routed to alice only: {trace}")


@pytest.mark.asyncio
async def test_the_record_of_a_click_after_a_sibling_answer_names_the_gate_that_ran(monkeypatch) -> None:
    trace = await _scenario(monkeypatch, {})
    if not trace["click_flipped"]:
        pytest.skip(f"the click woke nothing: {trace}")
    ran = trace["ran_after_click"]
    clicked_key = f"tool_approval:{SID}:u-{trace['clicked']}"
    later = [r for r in trace["records"] if r[0] != A_KEY]
    assert ran == [trace["clicked"]], f"the click on {trace['clicked']} ran {ran}: {trace}"
    # The click's own respond-time record and the resume-time record are ONE record on the unique key (the fake store has no unique index, so the set is compared).
    assert {r[0] for r in later} == {clicked_key}, f"the gate that ran is {ran} but the resume-time record names {[r[0] for r in later]}: {trace}"


@pytest.mark.asyncio
async def test_pre_existing_shape_primary_answered_first_then_a_click_on_the_new_primary(monkeypatch) -> None:
    """Main-compatible shape (no non-primary answer needed): the primary is answered, the other tool_call becomes the new primary, and the click on it
    still skips the approver check, because the re-park carries the FIRST park's tool_call_id and no top-level checkpoint."""
    trace = await _scenario(monkeypatch, {"T1": {"kind": "users", "users": ["alice"]}, "T2": {"kind": "users", "users": ["alice"]}}, answer_first="primary")
    assert trace["click_refused"] == "ApproverRefusedError" and trace["click_flipped"] == 0 and trace["ran_after_click"] == [], (
        f"an unidentified channel click decided a gate routed to alice only: {trace}")


@pytest.mark.asyncio
async def test_rest_after_a_non_primary_answer_still_judges_the_primarys_real_approvers(monkeypatch) -> None:
    """A (non-primary) answered first; T1 stays the primary. The REST respond for T1 resolves the top-level PROJECTION (no checkpoint after
    repark_continuation), whose resume_metadata for a ToolCall primary carries no `approvers` key: bob passes a gate routed to alice only."""
    trace = await _scenario(monkeypatch, {"T1": {"kind": "users", "users": ["alice"]}, "T2": {"kind": "users", "users": ["alice"]}})
    primary = trace["primary"]
    r = trace["rest_after_repark"][primary]
    assert r is not None, trace
    assert r["bob_may_decide"] is False, f"after a sibling's answer the console lets bob decide {primary}, routed to alice only: {trace}"


@pytest.mark.asyncio
async def test_the_reparked_session_lists_every_gate_the_child_still_waits_on(monkeypatch) -> None:
    """The first park's shape: the child's ADVANCED checkpoint on the blob (so every pending gate is listed with its own metadata) and the new primary's id."""
    trace = await _scenario(monkeypatch, {})

    assert trace["repark_has_top_level_checkpoint"] is True
    assert sorted(tcid for tcid, _key in trace["repark_listed"]) == ["u-T1", "u-T2"]
    assert trace["repark_top_tool_call_id"] == f"u-{trace['primary']}"


@pytest.mark.asyncio
async def test_when_the_primary_is_answered_the_next_gate_becomes_the_reparked_primary(monkeypatch) -> None:
    trace = await _scenario(monkeypatch, {}, answer_first="primary")

    assert trace["repark_top_tool_call_id"] == f"u-{trace['clicked']}", "the re-park carries the NEW primary's tool_call_id, not the answered one's"
    assert [tcid for tcid, _key in trace["repark_listed"] if tcid.startswith("u-T")] == [f"u-{trace['clicked']}"]
