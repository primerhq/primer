"""A human gate takes ONE decision: a second one is refused with 409 ``already_decided`` (board ticket 01a12606).

Two decisions on one gate, made before the worker claims the row, used to behave by park shape (the #730 review's probe e):

* a GRAPH park (``parked_event_keys`` set) let the flip advance from ``resumable`` and rewrote the gate's leaf, so the second decision replaced the
  first in ``resume_event_payload(s)`` and the worker ran it, while the second respond-time ``ToolApprovalRecord`` was dropped by the unique index
  on ``gate_event_key``: the audit named the first decision and the worker ran the second. Both responds answered 202.
* an AGENT park (single event) refused the second flip (it advances from ``parked`` only) and still answered 202, so a decision that was never
  applied was reported accepted.

Driven through the real respond routes of an app on ``SqliteStorageProvider`` (the record's unique index is real), the real flip, the real record
writer and, for what runs, the real ``WorkerPool`` resume of the row the responds left. A retry of the decision that landed (the same payload) is
still accepted: the respond after a half-applied first attempt is the retry that repairs it.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
import pytest_asyncio
from httpx import ASGITransport

from primer.api.app import create_test_app
from primer.api.registries import ProviderRegistry
from primer.auth.passwords import hash_password
from primer.claim.adapters.sessions import SessionClaimAdapter
from primer.claim.in_memory import InMemoryClaimEngine
from primer.graph.executor import GraphExecutor
from primer.int.claim import ClaimKind
from primer.model.chat import ToolResultPart
from primer.model.graph import GraphNodeMessage, GraphThread
from primer.model.provider import SqliteConfig
from primer.model.scheduler import WorkerConfig
from primer.model.storage import OffsetPage
from primer.model.tool_approval import ToolApprovalRecord
from primer.model.user import User
from primer.model.workspace_session import GraphSessionBinding, SessionStatus, WorkspaceSession
from primer.model.yield_ import Yielded, YieldToWorker, new_gate_id
from primer.session.persistence import _CoalesceState, stash_graph_scoped_ids, translate_stream_event
from primer.storage.sqlite import SqliteStorageProvider
from primer.worker.graph_resume_coordinator import _repark_graph_yield_outcome
from primer.worker.pool import WorkerPool
from tests._resume_hook_fakes import NullWorkspaceIO, make_toolcall_executor
from tests.api.test_gate_fence import _ask_user_session
from tests.graph.test_toolcall_dispatch import _InMemoryStorage
from tests.worker.test_approval_record_resume import _approval_session, _FakeToolManager, _RecordingExecutor
from tests.worker.test_tool_call_node_resume_answers_its_call import _call_id, _graph

WID = "ws-one"
G = "c" * 32


async def _async_return(value: Any) -> Any:
    return value


# ---- the app: real routes over SQLite, the engine the worker claims from -------------------------------------------------------------------


@pytest_asyncio.fixture
async def sp(tmp_path) -> AsyncIterator[SqliteStorageProvider]:
    provider = SqliteStorageProvider(SqliteConfig(path=str(tmp_path / "one-decision.sqlite")))
    await provider.initialize()
    await provider._ensure_events_schema()  # noqa: SLF001 - the routes' wake and audit events append to it, as in production
    try:
        yield provider
    finally:
        await provider.aclose()


@pytest.fixture
def engine(sp: SqliteStorageProvider) -> InMemoryClaimEngine:
    return InMemoryClaimEngine(adapters={ClaimKind.SESSION: SessionClaimAdapter(session_storage=sp.get_storage(WorkspaceSession))})


@pytest_asyncio.fixture
async def client(sp: SqliteStorageProvider, engine: InMemoryClaimEngine) -> AsyncIterator[httpx.AsyncClient]:
    registry = ProviderRegistry(
        sp,  # type: ignore[arg-type]
        llm_factory=lambda p: object(),
        embedder_factory=lambda p: object(),
        cross_encoder_factory=lambda p: object(),
        toolset_factory=lambda t: object(),
    )
    app = create_test_app(storage_provider=sp, provider_registry=registry)  # type: ignore[arg-type]
    app.state.claim_engine = engine
    async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        reg = await c.post("/v1/auth/register", json={"username": "testuser", "password": "testpassword"})
        assert reg.status_code == 200, reg.text
        yield c


async def _login_as(client: httpx.AsyncClient, sp: SqliteStorageProvider, username: str) -> None:
    await sp.get_storage(User).create(User(
        id="user-" + username, username=username, password_hash=await hash_password(username + "pass1"), created_at=datetime.now(UTC), role="user",
    ))
    login = await client.post("/v1/auth/login", json={"username": username, "password": username + "pass1"})
    assert login.status_code == 200, login.text


async def _approve_or_reject(client: httpx.AsyncClient, sid: str, card: dict, decision: str) -> httpx.Response:
    body: dict[str, Any] = {"tool_call_id": card["tool_call_id"], "gate_id": card["gate_id"], "decision": decision}
    if decision == "rejected":
        body["reason"] = "no"
    return await client.post(f"/v1/sessions/{sid}/tool_approval/respond", json=body)


def _answer(resp: httpx.Response) -> tuple[int, str | None]:
    """The status, and the problem ``code`` of a refusal."""
    code = (resp.json().get("extensions") or {}).get("code") if resp.status_code >= 400 else None
    return resp.status_code, code


async def _records(sp: SqliteStorageProvider, sid: str) -> list[tuple[str, str | None]]:
    page = await sp.get_storage(ToolApprovalRecord).list(OffsetPage(offset=0, length=50))
    return sorted((r.decision, r.decided_by) for r in page.items if r.session_id == sid)


def _pool(sp: SqliteStorageProvider, engine: InMemoryClaimEngine) -> WorkerPool:
    pool = WorkerPool(
        config=WorkerConfig(concurrency=1), scheduler=None, storage=sp,  # type: ignore[arg-type]
        workspace_registry=None, provider_registry=None, engine=engine,  # type: ignore[arg-type]
    )
    pool._worker_id = "wrk-one-decision"
    return pool


async def _claim_and_run(pool: WorkerPool, engine: InMemoryClaimEngine, sid: str) -> None:
    """What the worker does with the row the responds left: claim its lease and run the resume branch."""
    leases = await engine.claim_due("wrk-one-decision", max_count=10)
    lease = next(ln for ln in leases if ln.kind == ClaimKind.SESSION and ln.entity_id == sid)
    await pool._run_engine_session(lease)


# ---- a graph session parked at a gated ToolCall node (the probe's park) ---------------------------------------------------------------------


class _Tool:
    """The gated tool: the first dispatch parks at its approval gate, the approved re-dispatch (``bypass_approval``) runs it."""

    def __init__(self, sid: str) -> None:
        self.sid = sid
        self.ran: list[str] = []

    async def __call__(self, node: Any, arguments: Any, bypass_approval: bool = False) -> ToolResultPart:
        tcid = _call_id()
        if not bypass_approval:
            meta = {
                "policy_id": "pol", "approval_type": "required", "gate_reason": "matched", "approvers": None, "gate_id": new_gate_id(),
                "original_call": {"id": tcid, "name": "dangerous__tool", "arguments": dict(arguments)},
            }
            raise YieldToWorker(Yielded(tool_name="_approval", event_key=f"tool_approval:{self.sid}:{tcid}", resume_metadata=meta), tool_call_id=tcid)
        self.ran.append(node.id)
        return ToolResultPart(id=tcid, output="ran")


async def _park_graph(sp: SqliteStorageProvider, sid: str) -> tuple[_Tool, GraphExecutor]:
    """Run the graph to its gate, store the park as the worker does, and return the tool and the executor a resume builds."""
    tool = _Tool(sid)
    graph = _graph()
    ts: _InMemoryStorage[GraphThread] = _InMemoryStorage(GraphThread)
    ms: _InMemoryStorage[GraphNodeMessage] = _InMemoryStorage(GraphNodeMessage)
    thread = await GraphExecutor.open_thread(graph=graph, thread_storage=ts, title="t")  # type: ignore[arg-type]
    parker = make_toolcall_executor(graph, thread, ts, ms, tool)
    state = _CoalesceState()
    parker.bind_coalesce_state(state)
    with pytest.raises(YieldToWorker) as ei:
        async for ev in parker.invoke([]):
            translate_stream_event(ev, state, turn_no=1)
    checkpoint = parker.snapshot_state()
    stash_graph_scoped_ids(checkpoint, state)
    yld = ei.value
    yld.graph_checkpoint = checkpoint
    park = _repark_graph_yield_outcome(SimpleNamespace(turn_no=1), yld).park
    assert park.parked_event_keys, "a graph park waits on its key set (a multi-event park)"
    await sp.get_storage(WorkspaceSession).create(WorkspaceSession(
        id=sid, workspace_id=WID, binding=GraphSessionBinding(graph_id="g-park"), status=SessionStatus.RUNNING, created_at=datetime.now(UTC),
        parked_status="parked", parked_at=park.parked_at, parked_event_key=park.parked_event_key, parked_event_keys=park.parked_event_keys,
        parked_until=park.parked_until, parked_state=park.parked_state,
    ))
    return tool, make_toolcall_executor(graph, thread, ts, ms, tool)


async def _approval_card(client: httpx.AsyncClient, sid: str) -> dict:
    """The approval card the console draws for the session (the per-session pending list)."""
    items = (await client.get(f"/v1/workspaces/{WID}/sessions/{sid}/yields/pending")).json()["items"]
    (card,) = [i for i in items if i["kind"] == "approval"]
    assert card["gate_id"], "the card names its gate"
    return card


async def _run_graph_worker(sp: SqliteStorageProvider, engine: InMemoryClaimEngine, monkeypatch: pytest.MonkeyPatch, sid: str,
                            executor: GraphExecutor) -> None:
    pool = _pool(sp, engine)
    monkeypatch.setattr(pool, "_load_workspace_for_persist", lambda _w: _async_return(NullWorkspaceIO()))
    monkeypatch.setattr(pool, "_build_graph_executor", lambda _s, _w: _async_return(executor))
    await _claim_and_run(pool, engine, sid)


def _gate_leaf(row: WorkspaceSession) -> dict:
    (entry,) = ((row.parked_state or {}).get("resume_event_payloads") or {}).values()
    return entry["payload"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "second, by",
    [("rejected", "testuser"), ("approved", "otheruser")],
    ids=["the-same-operator-changes-their-mind", "another-operator-approves-too"],
)
async def test_a_second_decision_on_a_graph_gate_is_refused_and_the_worker_runs_the_first_which_the_audit_names(
    sp, engine, client, monkeypatch, caplog, second: str, by: str,
) -> None:
    sid = "s-one-graph"
    tool, resumer = await _park_graph(sp, sid)
    card = await _approval_card(client, sid)

    first = await _approve_or_reject(client, sid, card, "approved")
    if by != "testuser":
        await _login_as(client, sp, by)
    refused = await _approve_or_reject(client, sid, card, second)
    row = await sp.get_storage(WorkspaceSession).get(sid)
    leaf = _gate_leaf(row)
    with caplog.at_level(logging.ERROR, logger="primer.agent.approval_record"):
        await _run_graph_worker(sp, engine, monkeypatch, sid, resumer)

    assert {
        "first": _answer(first),
        "second": _answer(refused),
        "the gate's leaf (what the worker drains)": (leaf["decision"], leaf["decided_by"]),
        "ran": tool.ran,
        "records": await _records(sp, sid),
        "the resume-time write found a disagreement": any("disagreement" in r.getMessage() for r in caplog.records),
    } == {
        "first": (202, None),
        "second": (409, "already_decided"),
        "the gate's leaf (what the worker drains)": ("approved", "testuser"),
        "ran": ["t"],
        "records": [("approved", "testuser")],
        "the resume-time write found a disagreement": False,
    }


@pytest.mark.asyncio
async def test_a_retried_identical_decision_on_a_graph_gate_is_accepted_again_and_recorded_once(sp, engine, client, monkeypatch) -> None:
    """The same operator's same decision sent twice (a retry after a lost answer, or after a half-applied first attempt) is the decision that
    landed, not a second one: 202 again, one record, and it runs."""
    sid = "s-one-graph-retry"
    tool, resumer = await _park_graph(sp, sid)
    card = await _approval_card(client, sid)

    first = await _approve_or_reject(client, sid, card, "approved")
    retry = await _approve_or_reject(client, sid, card, "approved")
    await _run_graph_worker(sp, engine, monkeypatch, sid, resumer)

    assert (_answer(first), _answer(retry), tool.ran, await _records(sp, sid)) == ((202, None), (202, None), ["t"], [("approved", "testuser")])


# ---- an agent session parked at an approval gate ---------------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_second_decision_on_an_agent_gate_is_refused_with_409_and_the_first_runs(sp, engine, client, monkeypatch) -> None:
    sid, raw = "s-one-agent", "call_0"
    parked = _approval_session(sid, tcid=raw, resume_payload=None)
    parked.parked_state["yielded"]["resume_metadata"]["gate_id"] = G
    await sp.get_storage(WorkspaceSession).create(parked.model_copy(update={"parked_status": "parked"}))
    card = {"tool_call_id": raw, "gate_id": G}

    first = await _approve_or_reject(client, sid, card, "approved")
    refused = await _approve_or_reject(client, sid, card, "rejected")
    row = await sp.get_storage(WorkspaceSession).get(sid)
    pool = _pool(sp, engine)
    executor = _RecordingExecutor(tool_manager=_FakeToolManager())
    monkeypatch.setattr(pool, "_load_workspace_for_persist", lambda _w: _async_return(NullWorkspaceIO()))
    monkeypatch.setattr(pool, "_build_agent_executor", lambda _s, _w: _async_return(executor))
    await _claim_and_run(pool, engine, sid)
    outputs = [part.output for batch in executor.injected for message in batch for part in getattr(message, "parts", []) if hasattr(part, "output")]

    assert {
        "first": _answer(first),
        "second": _answer(refused),
        "the stamped decision": row.parked_state["resume_event_payload"]["decision"],
        "the tool ran": any('"ran": true' in str(out) for out in outputs),
        "records": await _records(sp, sid),
    } == {
        "first": (202, None),
        "second": (409, "already_decided"),
        "the stamped decision": "approved",
        "the tool ran": True,
        "records": [("approved", "testuser")],
    }


# ---- a question (ask_user) answered twice -----------------------------------------------------------------------------------------------------


def _graph_question(sid: str) -> WorkspaceSession:
    """A graph agent node's ask_user park: the outer yield is the graph label, the prompt is the checkpoint's pending agent yield."""
    now = datetime.now(UTC)
    ek = f"ask_user:{sid}:A:tc-q"
    return WorkspaceSession(
        id=sid, workspace_id=WID, binding=GraphSessionBinding(graph_id="g-q"), status=SessionStatus.RUNNING, created_at=now,
        parked_status="parked", parked_at=now, parked_until=now + timedelta(seconds=600), parked_event_key=ek, parked_event_keys=[ek],
        parked_state={
            "tool_call_id": "tc-q",
            "yielded": {"tool_name": "_approval", "event_key": ek, "timeout": 600.0, "resume_metadata": {}, "event_keys": [ek]},
            "llm_messages": [], "turn_no": 1, "started_at": now.isoformat(), "resume_event_payload": None,
            "graph_checkpoint": {"pending_agent_yields": [{
                "node_id": "A", "tool_call_id": "tc-q", "event_key": ek, "tool_name": "ask_user",
                "resume_metadata": {"prompt": "Which colour?", "gate_id": G}, "llm_messages": [], "iteration": 1,
            }]},
        },
    )


def _stamped_answer(row: WorkspaceSession) -> Any:
    state = row.parked_state or {}
    leaves = state.get("resume_event_payloads")
    payload = next(iter(leaves.values()))["payload"] if leaves else state["resume_event_payload"]
    return payload["response"]


@pytest.mark.asyncio
@pytest.mark.parametrize("shape", ["graph", "agent"])
async def test_a_second_answer_to_a_question_is_refused_with_409_and_the_first_stands(sp, client, shape: str) -> None:
    sid = f"s-one-question-{shape}"
    parked = _graph_question(sid) if shape == "graph" else _ask_user_session(session_id=sid, tool_call_id="tc-q", gate_id=G, workspace_id=WID)
    await sp.get_storage(WorkspaceSession).create(parked)

    first = await client.post(f"/v1/sessions/{sid}/ask_user/respond", json={"tool_call_id": "tc-q", "gate_id": G, "response": "red"})
    refused = await client.post(f"/v1/sessions/{sid}/ask_user/respond", json={"tool_call_id": "tc-q", "gate_id": G, "response": "blue"})
    row = await sp.get_storage(WorkspaceSession).get(sid)

    assert (_answer(first), _answer(refused), _stamped_answer(row)) == ((202, None), (409, "already_decided"), "red")
