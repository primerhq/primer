"""A respond for a gate that is no longer pending says so with a code: 404 ``gate_not_pending`` (ticket 01a125ec-68d6, from the #730 review).

The approval and ask_user respond routes answer 404 for several reasons, and the console read every one of them as "this gate moved on, reload".
Only a 404 whose session is there and readable but whose gate is no longer pending carries ``extensions.code = "gate_not_pending"``; a missing
session, or a park that cannot be read, carries none, and the console shows those as the errors they are.

The approval cases drive the REAL respond route against a REAL two-phase re-park (lifted from the #730 reviewer's probe): the graph park, the
approve-resume and the re-park are the real executor and ``resume_graph_from_checkpoint`` (the #712 harness), each park is persisted through the
real ``_repark_graph_yield_outcome`` (the ParkRequest production writes), and the card's ids come from the real pending routes. The ask_user case
does the same with a graph whose two ToolCall nodes each ask a question: answering the first resumes the graph into the second, which re-parks.
"""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pytest

import primer.toolset.system  # noqa: F401  (registers the ask_user resume hook the value-yield resume runs)
from primer.graph.executor import GraphExecutor
from primer.model.graph import Graph, GraphNodeMessage, GraphThread, _BeginNode, _EndNode, _StaticEdge, _ToolCallNode
from primer.model.workspace_session import AgentSessionBinding, GraphSessionBinding, SessionStatus, WorkspaceSession
from primer.model.yield_ import Yielded, YieldToWorker, new_gate_id
from primer.session.persistence import _CoalesceState, stash_graph_scoped_ids, translate_stream_event
from primer.worker.graph_resume_coordinator import _repark_graph_yield_outcome
from tests._resume_hook_fakes import make_toolcall_executor
from tests.graph.test_toolcall_dispatch import _InMemoryStorage
from tests.worker.test_tool_call_node_resume_answers_its_call import _call_id, _graph, _resume_full

WID = "ws-gnp"
CODE = "gate_not_pending"


def _code(answer: Any) -> str | None:
    """The problem's ``extensions.code`` (``None`` when the envelope carries none)."""
    return (answer.json().get("extensions") or {}).get("code")


# ---- the real parks -------------------------------------------------------------------------------------------------------------------------------


def _approval_gate(sid: str, ids: list[str], gates: list[str]):
    """Parks on an approval whose metadata has the shape approval_resume_metadata stamps (gate_id minted per park, original_call under the call id)."""
    async def gate(node: Any, arguments: Any, bypass_approval: bool = False):
        ids.append(_call_id())
        gid = new_gate_id()
        gates.append(gid)
        meta = {"policy_id": "pol", "approval_type": "required", "gate_reason": "matched", "approvers": None,
                "original_call": {"id": ids[-1], "name": "dangerous__tool", "arguments": {"q": "x"}}, "gate_id": gid}
        raise YieldToWorker(Yielded(tool_name="_approval", event_key=f"tool_approval:{sid}:{ids[-1]}", resume_metadata=meta), tool_call_id=ids[-1])
    return gate


def _sleeping(sid: str, ids: list[str]):
    """Phase 2 of an approval on a YIELDING tool: the approved re-dispatch runs the tool, which parks on its own timer (not an approval)."""
    async def sleep(node: Any, arguments: Any, bypass_approval: bool = False):
        ids.append(_call_id())
        raise YieldToWorker(Yielded(tool_name="sleep", event_key=f"timer:{sid}:{ids[-1]}", resume_metadata={"duration_s": 30}), tool_call_id=ids[-1])
    return sleep


def _asking(sid: str, ids: list[str], gates: list[str]):
    """A ToolCall node on ``ask_user``: the yield has the shape the ask_user handler builds (prompt, schema, call id, a gate id minted per question)."""
    async def ask(node: Any, arguments: Any, bypass_approval: bool = False):
        ids.append(_call_id())
        gid = new_gate_id()
        gates.append(gid)
        meta = {"prompt": f"Question {len(ids)}?", "response_schema": None, "tool_call_id": ids[-1], "files": None, "gate_id": gid}
        raise YieldToWorker(Yielded(tool_name="ask_user", event_key=f"ask_user:{sid}:{ids[-1]}", resume_metadata=meta), tool_call_id=ids[-1])
    return ask


def _two_questions() -> Graph:
    return Graph(
        id="g-ask", description="begin -> ask -> ask again -> end",
        nodes=[_BeginNode(id="begin"), _ToolCallNode(id="q1", tool_id="system__ask_user", arguments={"prompt": "one?"}),
               _ToolCallNode(id="q2", tool_id="system__ask_user", arguments={"prompt": "two?"}), _EndNode(id="exit", output_template="done")],
        edges=[_StaticEdge(from_node="begin", to_node="q1"), _StaticEdge(from_node="q1", to_node="q2"), _StaticEdge(from_node="q2", to_node="exit")],
    )


async def _first_park(graph: Graph, dispatcher):
    """Run the graph to its first park; the park carries the snapshot with the scoped ids stashed, as dispatch.py does."""
    ts: _InMemoryStorage[GraphThread] = _InMemoryStorage(GraphThread)
    ms: _InMemoryStorage[GraphNodeMessage] = _InMemoryStorage(GraphNodeMessage)
    thread = await GraphExecutor.open_thread(graph=graph, thread_storage=ts, title="t")  # type: ignore[arg-type]
    parker = make_toolcall_executor(graph, thread, ts, ms, dispatcher)
    state = _CoalesceState()
    parker.bind_coalesce_state(state)
    with pytest.raises(YieldToWorker) as ei:
        async for ev in parker.invoke([]):
            translate_stream_event(ev, state, turn_no=1)
    checkpoint = parker.snapshot_state()
    stash_graph_scoped_ids(checkpoint, state)
    yld = ei.value
    assert getattr(yld, "graph_checkpoint", None) is not None, "the outer park carries the snapshot"
    yld.graph_checkpoint = checkpoint
    return graph, thread, ts, ms, yld


def _graph_row(sid: str, yld: Any) -> WorkspaceSession:
    """The row a park writes, through the production ParkRequest builder."""
    park = _repark_graph_yield_outcome(SimpleNamespace(turn_no=1), yld).park
    return WorkspaceSession(
        id=sid, workspace_id=WID, binding=GraphSessionBinding(graph_id="g-park"), status=SessionStatus.RUNNING, created_at=datetime.now(UTC),
        parked_status="parked", parked_at=park.parked_at, parked_event_key=park.parked_event_key, parked_event_keys=park.parked_event_keys,
        parked_state=park.parked_state,
    )


async def _replace(storage: Any, row: WorkspaceSession) -> None:
    await storage.delete(row.id)
    await storage.create(row)


def _running(row: WorkspaceSession) -> WorkspaceSession:
    """The same session after the park was answered and the worker took it back: running, nothing parked."""
    return row.model_copy(update={"parked_status": None, "parked_state": None, "parked_event_key": None, "parked_event_keys": None, "parked_at": None})


async def _approve(client, sid: str, tcid: str, gate_id: str | None):
    body: dict = {"tool_call_id": tcid, "decision": "approved"}
    if gate_id is not None:
        body["gate_id"] = gate_id
    return await client.post(f"/v1/sessions/{sid}/tool_approval/respond", json=body)


async def _answer(client, sid: str, tcid: str, gate_id: str | None):
    body: dict = {"tool_call_id": tcid, "response": "EUR"}
    if gate_id is not None:
        body["gate_id"] = gate_id
    return await client.post(f"/v1/sessions/{sid}/ask_user/respond", json=body)


def _assert_not_pending(answer: Any) -> None:
    assert answer.status_code == 404, answer.text
    assert answer.json()["type"] == "/errors/not-found"
    assert _code(answer) == CODE, answer.json()


def _assert_plain_404(answer: Any) -> None:
    assert answer.status_code == 404, answer.text
    assert _code(answer) is None, answer.json()


# ---- tool_approval/respond ------------------------------------------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_first_gate_of_a_two_phase_re_park_is_not_pending_and_says_so_with_the_code(app, client) -> None:
    """Approved elsewhere, the approved re-dispatch hit a second gate under a fresh id: the first gate's ids (the card's, the banner's, and a
    tokenless respond) are pending under no approval gate. The controls: the second gate's id under the first gate's token is the 409, its own
    token is the 202."""
    sid = "s-gnp-a"
    ids: list[str] = []
    gates: list[str] = []
    graph, thread, ts, ms, yld = await _first_park(_graph(), _approval_gate(sid, ids, gates))
    storage = app.state.storage_provider.get_storage(WorkspaceSession)
    await storage.create(_graph_row(sid, yld))
    card = [i for i in (await client.get(f"/v1/workspaces/{WID}/sessions/{sid}/yields/pending")).json()["items"] if i["kind"] == "approval"][0]
    banner = (await client.get(f"/v1/sessions/{sid}/tool_approval/pending")).json()
    assert (card["tool_call_id"], card["gate_id"]) == (banner["tool_call_id"], banner["gate_id"]) == (ids[0], gates[0])

    resumer = make_toolcall_executor(graph, thread, ts, ms, _approval_gate(sid, ids, gates))
    _written, repark = await _resume_full(yld.graph_checkpoint, resumer, {"decision": "approved"}, ids[0], event_key=f"tool_approval:{sid}:{ids[0]}")
    assert repark is not None and len(ids) == 2 and ids[0] != ids[1], "the approved dispatch re-parked on a second gate under a fresh id"
    await _replace(storage, _graph_row(sid, repark))
    card2 = [i for i in (await client.get(f"/v1/workspaces/{WID}/sessions/{sid}/yields/pending")).json()["items"] if i["kind"] == "approval"][0]
    assert (card2["tool_call_id"], card2["gate_id"]) == (ids[1], gates[1])

    for answer in (await _approve(client, sid, card["tool_call_id"], card["gate_id"]),
                   await _approve(client, sid, banner["tool_call_id"], banner["gate_id"]),
                   await _approve(client, sid, card["tool_call_id"], None)):
        _assert_not_pending(answer)
        assert answer.json()["detail"] == f"No pending tool_approval with tool_call_id {ids[0]!r} on {sid!r}"

    stale = await _approve(client, sid, card2["tool_call_id"], card["gate_id"])
    assert stale.status_code == 409 and _code(stale) == "approval_stale", stale.text
    accepted = await _approve(client, sid, card2["tool_call_id"], card2["gate_id"])
    assert accepted.status_code == 202, accepted.text


@pytest.mark.asyncio
async def test_a_gate_whose_node_re_parked_on_the_tools_own_yield_is_not_pending_and_says_so_with_the_code(app, client) -> None:
    """Approved elsewhere, the approved tool ran and parked on its own timer: no approval gate is pending on the session any more."""
    sid = "s-gnp-b"
    ids: list[str] = []
    gates: list[str] = []
    graph, thread, ts, ms, yld = await _first_park(_graph(), _approval_gate(sid, ids, gates))
    storage = app.state.storage_provider.get_storage(WorkspaceSession)
    await storage.create(_graph_row(sid, yld))
    card = [i for i in (await client.get(f"/v1/workspaces/{WID}/sessions/{sid}/yields/pending")).json()["items"] if i["kind"] == "approval"][0]
    resumer = make_toolcall_executor(graph, thread, ts, ms, _sleeping(sid, ids))
    _written, repark = await _resume_full(yld.graph_checkpoint, resumer, {"decision": "approved"}, ids[0], event_key=f"tool_approval:{sid}:{ids[0]}")
    assert repark is not None
    await _replace(storage, _graph_row(sid, repark))
    pending = (await client.get(f"/v1/workspaces/{WID}/sessions/{sid}/yields/pending")).json()["items"]
    assert [i["kind"] for i in pending] == ["sleep"]

    answer = await _approve(client, sid, card["tool_call_id"], card["gate_id"])
    _assert_not_pending(answer)
    assert answer.json()["detail"] == f"{sid!r} is parked on a different tool"


@pytest.mark.asyncio
async def test_only_a_gate_that_moved_on_carries_the_code_not_a_deleted_session_or_a_park_that_cannot_be_read(app, client) -> None:
    """Decided elsewhere and the session running again: the gate moved on (the code). A park whose gate has no event key, a parked row that names
    nothing, and a deleted session are not a gate that moved on: a plain 404 each, shown by the console as the error it is."""
    sid = "s-gnp-c"
    ids: list[str] = []
    gates: list[str] = []
    _g, _t, _ts, _ms, yld = await _first_park(_graph(), _approval_gate(sid, ids, gates))
    storage = app.state.storage_provider.get_storage(WorkspaceSession)
    row = _graph_row(sid, yld)
    await storage.create(row)
    card = [i for i in (await client.get(f"/v1/workspaces/{WID}/sessions/{sid}/yields/pending")).json()["items"] if i["kind"] == "approval"][0]

    await _replace(storage, _running(row))
    running = await _approve(client, sid, card["tool_call_id"], card["gate_id"])
    _assert_not_pending(running)
    assert running.json()["detail"] == f"{sid!r} has no pending tool_approval"

    checkpoint = dict(row.parked_state["graph_checkpoint"])
    checkpoint["pending_toolcalls"] = [{**e, "parked_event_key": None} for e in checkpoint["pending_toolcalls"]]
    await _replace(storage, row.model_copy(update={"parked_state": {**row.parked_state, "graph_checkpoint": checkpoint}}))
    keyless = await _approve(client, sid, card["tool_call_id"], card["gate_id"])
    _assert_plain_404(keyless)
    assert keyless.json()["detail"] == f"{sid!r} park is missing event_key"

    await _replace(storage, row.model_copy(update={"parked_state": {}}))
    empty = await _approve(client, sid, card["tool_call_id"], card["gate_id"])
    _assert_plain_404(empty)
    assert empty.json()["detail"] == f"{sid!r} park names nothing it waits on"

    await storage.delete(sid)
    gone = await _approve(client, sid, card["tool_call_id"], card["gate_id"])
    _assert_plain_404(gone)
    assert gone.json()["detail"] == f"{sid!r} does not exist"


# ---- ask_user/respond -----------------------------------------------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_first_question_of_a_graph_that_asked_again_is_not_pending_and_says_so_with_the_code(app, client) -> None:
    """Answered elsewhere, the graph resumed into its next ask_user node and re-parked under a fresh id: the first question's ids are pending
    nowhere. The controls: the second question's id under the first question's token is the 409, its own token is the 202."""
    sid = "s-gnp-q"
    ids: list[str] = []
    gates: list[str] = []
    graph, thread, ts, ms, yld = await _first_park(_two_questions(), _asking(sid, ids, gates))
    storage = app.state.storage_provider.get_storage(WorkspaceSession)
    await storage.create(_graph_row(sid, yld))
    first = (await client.get(f"/v1/sessions/{sid}/ask_user/pending")).json()
    assert (first["tool_call_id"], first["gate_id"]) == (ids[0], gates[0])

    resumer = make_toolcall_executor(graph, thread, ts, ms, _asking(sid, ids, gates))
    _written, repark = await _resume_full(yld.graph_checkpoint, resumer, {"response": "EUR"}, ids[0], event_key=f"ask_user:{sid}:{ids[0]}")
    assert repark is not None and len(ids) == 2 and ids[0] != ids[1], "the answered graph re-parked on its second question"
    await _replace(storage, _graph_row(sid, repark))
    second = (await client.get(f"/v1/sessions/{sid}/ask_user/pending")).json()
    assert (second["tool_call_id"], second["gate_id"]) == (ids[1], gates[1])

    for answer in (await _answer(client, sid, first["tool_call_id"], first["gate_id"]), await _answer(client, sid, first["tool_call_id"], None)):
        _assert_not_pending(answer)
        assert answer.json()["detail"] == f"No pending ask_user prompt with tool_call_id {ids[0]!r} on session {sid!r}"

    stale = await _answer(client, sid, second["tool_call_id"], first["gate_id"])
    assert stale.status_code == 409 and _code(stale) == "approval_stale", stale.text
    accepted = await _answer(client, sid, second["tool_call_id"], second["gate_id"])
    assert accepted.status_code == 202, accepted.text


def _agent_row(sid: str, tcid: str, *, tool_name: str = "ask_user", state: dict | None = None) -> WorkspaceSession:
    """An agent session parked on one yield, in the shape the agent loop's park writes (the call id at the top level and in the metadata)."""
    if state is None:
        state = {"schema_version": 1, "tool_call_id": tcid, "yielded": {
            "tool_name": tool_name, "event_key": f"{tool_name}:{sid}:{tcid}", "timeout": 600.0,
            "resume_metadata": {"prompt": "Which currency?", "response_schema": None, "tool_call_id": tcid, "gate_id": new_gate_id()},
        }}
    return WorkspaceSession(
        id=sid, workspace_id=WID, binding=AgentSessionBinding(kind="agent", agent_id="ag-x"), status=SessionStatus.RUNNING,
        created_at=datetime.now(UTC), parked_status="parked", parked_at=datetime.now(UTC), parked_event_key=f"{tool_name}:{sid}:{tcid}",
        parked_state=state,
    )


@pytest.mark.asyncio
async def test_only_a_question_that_moved_on_carries_the_code_not_a_deleted_session_or_a_park_that_cannot_be_read(app, client) -> None:
    """An agent session's question: asked again under a fresh id, parked on another tool, or answered and running again, it moved on (the code).
    A park with no call id, a parked row that names nothing, and a deleted session are a plain 404 each."""
    sid = "s-gnp-r"
    storage = app.state.storage_provider.get_storage(WorkspaceSession)
    asked = _agent_row(sid, "tc-1")
    await storage.create(asked)
    first = (await client.get(f"/v1/sessions/{sid}/ask_user/pending")).json()
    assert first["tool_call_id"] == "tc-1"

    await _replace(storage, _agent_row(sid, "tc-2"))
    again = await _answer(client, sid, "tc-1", first["gate_id"])
    _assert_not_pending(again)
    assert again.json()["detail"] == f"No pending ask_user prompt with tool_call_id 'tc-1' on session {sid!r}"

    await _replace(storage, _agent_row(sid, "tc-1", tool_name="sleep"))
    other = await _answer(client, sid, "tc-1", first["gate_id"])
    _assert_not_pending(other)
    assert other.json()["detail"] == f"Session {sid!r} is parked on a different tool"

    await _replace(storage, _running(asked))
    running = await _answer(client, sid, "tc-1", first["gate_id"])
    _assert_not_pending(running)
    assert running.json()["detail"] == f"Session {sid!r} has no pending ask_user prompt"

    no_call_id = {"schema_version": 1, "yielded": {**asked.parked_state["yielded"], "resume_metadata": {"prompt": "Which currency?"}}}
    await _replace(storage, _agent_row(sid, "tc-1", state=no_call_id))
    malformed = await _answer(client, sid, "tc-1", None)
    _assert_plain_404(malformed)
    assert malformed.json()["detail"] == f"Session {sid!r} has a malformed ask_user park"

    for state in ({}, {"schema_version": 1, "tool_call_id": "tc-1", "yielded": {}}):
        await _replace(storage, _agent_row(sid, "tc-1", state=state))
        empty = await _answer(client, sid, "tc-1", first["gate_id"])
        _assert_plain_404(empty)
        assert empty.json()["detail"] == f"Session {sid!r} park names nothing it waits on"

    await storage.delete(sid)
    gone = await _answer(client, sid, "tc-1", first["gate_id"])
    _assert_plain_404(gone)
    assert gone.json()["detail"] == f"Session {sid!r} does not exist"
