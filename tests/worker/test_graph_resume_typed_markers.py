"""The multi-event graph drain (``resume_graph_engine``'s ``payloads_map`` branch) hands a node typed markers, once.

A graph park waits on several keys, so every reply is accumulated into ``resume_event_payloads`` and the resume
drains each entry. The timeout sweeper and the yields-cancel route publish MARKER dicts (``__yield_timeout__`` /
``__yield_cancelled__``); the single-event path converts its one payload into :class:`YieldTimeout` /
:class:`YieldCancelled` before any resume hook sees it, and the multi-event path has to do the same. ask_user's hook
recognises a timeout or a cancel only by ``isinstance``, so a raw marker dict was answered as an operator reply with
no response.

Each entry also carries its own ``event_key``. A park written by an older build holds a leaf under the RAW dispatch
key; after the leaf-key encoding (``leaf_key_for``) a resend or an echo of the same reply lands a second entry under
the ENCODED key. The drain keeps ONE entry per ``event_key``, the one stored under the raw spelling (only older code
writes that spelling, so it is the older entry), so the tool call is not delivered twice.

Driven through the REAL ``resume_graph_engine``, the real continuation walk (``resume_graph_continuation``), the
real re-park builder, the real agent-node hook seam and the real ``ask_user`` resume hook, over a real graph executor
and checkpoint. The session row is written by the real claim adapter and flipped by the real durable flip. Faked:
the agent turns (``run_agent_turn``), a tool_call node's dispatcher, the subagent's tool manager and the subagent
turn's resume.
"""

from __future__ import annotations

import dataclasses
import json
from datetime import datetime, timedelta, timezone

import pytest

import primer.toolset.system  # noqa: F401  (registers the ask_user resume hook)
from primer.claim.adapters.sessions import SessionClaimAdapter
from primer.graph.executor import GraphExecutor
from primer.int.claim import ParkRequest, ReleaseOutcome
from primer.model.agent import Agent, AgentModel
from primer.model.chat import ToolResultPart
from primer.model.graph import Graph, GraphNodeMessage, GraphThread, _AgentNodeRef, _BeginNode, _EndNode, _StaticEdge
from primer.model.workspace_session import GraphSessionBinding, SessionStatus, WorkspaceSession
from primer.model.yield_ import Yielded, YieldToWorker
from primer.session.yields import durably_mark_session_resumable, flip_sessions_parked_on
from primer.worker import graph_resume_coordinator
from primer.worker.continuation import InvocationServices
from primer.worker.frames import AgentFrame, AgentResumeContext
from primer.worker.yield_runtime import ParkedState, is_timeout_payload, make_cancelled_payload, make_timeout_payload

from tests._resume_hook_fakes import (
    EngineFakePool,
    EngineStorageProvider,
    NullWorkspaceIO,
    build_ask_user_graph,
    drain_until_yield,
    make_toolcall_executor,
)
from tests.graph.test_tool_wait_graph_park import _UnusedLLM, _model, _parallel_graph, _patch_run_agent_turn
from tests.graph.test_toolcall_dispatch import _InMemoryStorage

_SID = "gs-1"


def _now() -> datetime:
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# graphs and executors
# ---------------------------------------------------------------------------


def _single_agent_graph() -> Graph:
    return Graph(
        id="g-one", description="begin -> A -> exit",
        nodes=[_BeginNode(id="begin"), _AgentNodeRef(id="A", agent_id="agent-a"), _EndNode(id="exit")],
        edges=[_StaticEdge(from_node="begin", to_node="A"), _StaticEdge(from_node="A", to_node="exit")],
    )


async def _executor(graph: Graph) -> GraphExecutor:
    """A graph executor with the tool-calls-as-claims flag at its default (off)."""

    async def agent_resolver(agent_id: str) -> Agent:
        return Agent(id=agent_id, description=agent_id, model=AgentModel(profile_id="p--m"))

    async def llm_resolver(_agent):
        return (_UnusedLLM(), _model())

    threads: _InMemoryStorage[GraphThread] = _InMemoryStorage(GraphThread)
    messages: _InMemoryStorage[GraphNodeMessage] = _InMemoryStorage(GraphNodeMessage)
    thread = await GraphExecutor.open_thread(graph=graph, thread_storage=threads)  # type: ignore[arg-type]
    return GraphExecutor(
        graph=graph, agent_resolver=agent_resolver, llm_resolver=llm_resolver,  # type: ignore[arg-type]
        thread_storage=threads, message_storage=messages,  # type: ignore[arg-type]
        graph_thread_id=thread.id,
    )


async def _first_park(graph: Graph) -> YieldToWorker:
    """Run the graph until its first superstep parks; return the park the executor raised."""
    ex = await _executor(graph)
    try:
        async for _ev in ex.invoke([]):
            pass
    except YieldToWorker as raised:
        return raised
    raise AssertionError("the graph did not park")


# ---------------------------------------------------------------------------
# the node's agent turns
# ---------------------------------------------------------------------------


def _turn_so_far(text: str) -> list[dict]:
    """The in-progress assistant turn a park stamps (the node's resume rebuilds its prompt from it)."""
    return [{"role": "assistant", "parts": [{"type": "text", "text": text}]}]


def _subagent_frame(invoke_tcid: str) -> AgentFrame:
    return AgentFrame(
        agent_id="sub",
        llm_messages=_turn_so_far("subagent asking"),
        tool_call_id=invoke_tcid,
        depth=0,
        context=AgentResumeContext(
            session_id=_SID, workspace_id="ws-1", chat_id=None, principal="p", tools=["system__ask_user"],
        ),
    )


def _nested(yielded: Yielded, tcid: str, *, invoke_tcid: str) -> YieldToWorker:
    """The node's agent called invoke_agent and the SUBAGENT yielded: ``frames`` carries the subagent's frame."""
    yld = YieldToWorker(yielded, tool_call_id=tcid, llm_messages=_turn_so_far("invoking the subagent"))
    yld.frames = [_subagent_frame(invoke_tcid)]
    return yld


def _ask_user(tcid: str) -> Yielded:
    return Yielded(tool_name="ask_user", event_key=f"ask_user:{_SID}:{tcid}", resume_metadata={"prompt": "color?"})


def _own_ask_user(tcid: str) -> YieldToWorker:
    """The node's OWN ask_user (no nesting)."""
    return YieldToWorker(_ask_user(tcid), tool_call_id=tcid, llm_messages=_turn_so_far("asking"))


def _gated_ask_user(tcid: str) -> Yielded:
    """The approval gate on the subagent's ask_user call: approving it re-dispatches the call, which then yields."""
    return Yielded(
        tool_name="_approval",
        event_key=f"tool_approval:{_SID}:{tcid}",
        resume_metadata={"original_call": {"id": tcid, "name": "ask_user", "arguments": {"prompt": "color?"}}},
    )


class _YieldingToolManager:
    """The subagent's tool manager: the approved call runs and yields, as ask_user does."""

    async def execute(self, call, *, bypass_approval: bool = False):
        assert bypass_approval, "an approved gate re-dispatches with bypass_approval=True"
        raise YieldToWorker(
            Yielded(tool_name=call.name, event_key=f"ask_user:{_SID}:{call.id}", resume_metadata=dict(call.arguments)),
            tool_call_id=call.id,
        )


class _Subagent:
    """The continuation walk's services: the subagent's tool manager and its turn's resume."""

    def __init__(self) -> None:
        self.child_results: list[ToolResultPart] = []

    async def _build_subagent_toolmanager(self, context):
        return _YieldingToolManager()

    async def _resume_subagent(self, *, agent_id, context, llm_messages, child_result, depth, invoke_tool_call_id):
        self.child_results.append(child_result)
        return "subagent done"

    def services(self) -> InvocationServices:
        return InvocationServices(
            build_subagent_toolmanager=self._build_subagent_toolmanager,
            resume_subagent=self._resume_subagent,
            resolve_graph=None,
            build_child_graph_executor=None,
            graph_agent_tool_result=None,
            session_id=_SID,
            resolve_provider=None,
        )

    def leaf_answers(self) -> list[dict]:
        """What each resumed leaf resolved to (the ask_user hook's output, threaded into the subagent's turn)."""
        return [json.loads(r.output) for r in self.child_results]


# ---------------------------------------------------------------------------
# the pool and the session row
# ---------------------------------------------------------------------------


class _Pool(EngineFakePool):
    """``resume_graph_engine``'s pool with the REAL continuation walk, re-park builder and agent-node hook seam."""

    _provider_registry = None
    _approval_resolver = None

    def __init__(self, *, storage, graph: Graph, subagent: _Subagent, executor_factory=None) -> None:
        async def factory():
            return await _executor(graph)

        super().__init__(
            storage=storage, workspace_io=NullWorkspaceIO(), executor_factory=executor_factory or factory,
        )
        self._subagent = subagent
        self.agent_node_payloads: list[tuple[str | None, object]] = []

    def _build_invocation_services(self, session, workspace, executor, tool_manager):
        return self._subagent.services()

    async def _resume_graph_continuation(self, session, parked, checkpoint, ay, payload, workspace, executor):
        return await graph_resume_coordinator.resume_graph_continuation(
            self, session, parked, checkpoint, ay, payload, workspace, executor,
        )

    def _repark_graph_continuation(self, session, parked, checkpoint, ay, outcome):
        return graph_resume_coordinator.repark_graph_continuation(self, session, parked, checkpoint, ay, outcome)

    async def _graph_agent_tool_result(self, checkpoint, tcid, payload, *, session_id, event_key=None):
        self.agent_node_payloads.append((tcid, payload))
        return await graph_resume_coordinator.graph_agent_tool_result(
            self, checkpoint, tcid, payload, session_id=session_id, event_key=event_key,
        )


async def _parked_session(storage, raised: YieldToWorker, *, parked_at: datetime) -> WorkspaceSession:
    """Create the session row and park it as dispatch parks a graph session: multi-event, on every pending key."""
    sessions = storage.get_storage(WorkspaceSession)
    await sessions.create(WorkspaceSession(
        id=_SID, workspace_id="ws-1", binding=GraphSessionBinding(graph_id="g"),
        status=SessionStatus.WAITING, created_at=_now(), turn_no=0,
    ))
    parked_state = ParkedState(
        yielded=raised.yielded, llm_messages=[], turn_no=0,
        # deliberately NOT parked_at: a marker's elapsed_seconds is measured from the park's parked_at
        started_at=parked_at - timedelta(seconds=4000),
        tool_call_id=raised.tool_call_id, graph_checkpoint=raised.graph_checkpoint,
    )
    assert raised.yielded.event_keys, "a graph park waits on the full key set"
    await _apply_park(storage, ParkRequest(
        parked_state=parked_state.to_jsonable(),
        parked_event_key=raised.yielded.event_key,
        parked_event_keys=list(raised.yielded.event_keys),
        parked_until=parked_at + timedelta(hours=1),
        parked_at=parked_at,
    ))
    return await sessions.get(_SID)


async def _apply_park(storage, park: ParkRequest) -> None:
    adapter = SessionClaimAdapter(session_storage=storage.get_storage(WorkspaceSession))
    await adapter.on_release(None, _SID, outcome=ReleaseOutcome(success=True, drop_lease=True, park=park))


async def _resume(pool: _Pool, storage):
    """Claim the resumable row and run the graph resume over it, as the worker does."""
    row = await storage.get_storage(WorkspaceSession).get(_SID)
    assert row.parked_status == "resumable"
    return await graph_resume_coordinator.resume_graph_engine(pool, row, ParkedState.from_jsonable(row.parked_state))


async def _reply(storage, event_key: str, payload: dict) -> None:
    """A REST reply: the durable flip over a FRESH read of the row (it accumulates onto what the row holds)."""
    sessions = storage.get_storage(WorkspaceSession)
    row = await sessions.get(_SID)
    assert await durably_mark_session_resumable(
        row, event_key=event_key, payload=payload, session_storage=sessions, engine=None,
    )


async def _the_deadline_passes(storage) -> None:
    """Move the park's ``parked_until`` into the past, as the clock moving on does: the sweeper selects a row only once its deadline has passed."""
    sessions = storage.get_storage(WorkspaceSession)
    row = await sessions.get(_SID)
    await sessions.update(row.model_copy(update={"parked_until": _now() - timedelta(seconds=1)}))


async def _bus_delivery(storage, event_key: str, payload: dict) -> None:
    """What the timeout sweeper's or the yields-cancel route's publish reaches: the shared bus-side flip.

    The sweeper publishes a timeout marker only for a park past its ``parked_until`` and the flip refuses one whose deadline is still ahead (ticket 01a1208d),
    so a timeout marker is delivered once the deadline has passed.
    """
    if is_timeout_payload(payload):
        await _the_deadline_passes(storage)
    flipped = await flip_sessions_parked_on(
        event_key, payload, session_storage=storage.get_storage(WorkspaceSession), engine=None,
    )
    assert flipped == 1


# ===========================================================================
# nested re-yield, then a marker (mutation N72)
# ===========================================================================


_GATED = "tc-gated"


async def _reparked_after_an_approval(monkeypatch) -> tuple[_Pool, EngineStorageProvider, _Subagent, str]:
    """A graph agent node's subagent hit an approval gate; the operator approves; the approved call yields again.

    Returns with the session re-parked on the new leaf, as the continuation re-park leaves it, in its MULTI-event
    form (``parked_event_keys == [leaf key]``): the form an initial graph park already has, and the form every
    continuation re-park takes once the re-park key builders rebuild the key list (plan change (1), S2a PR-11).
    """
    _patch_run_agent_turn(monkeypatch, {"agent-a": _nested(_gated_ask_user(_GATED), _GATED, invoke_tcid="invoke-a")})
    graph = _single_agent_graph()
    raised = await _first_park(graph)
    storage = EngineStorageProvider()
    subagent = _Subagent()
    pool = _Pool(storage=storage, graph=graph, subagent=subagent)
    await _parked_session(storage, raised, parked_at=_now())

    await _reply(storage, f"tool_approval:{_SID}:{_GATED}", {"decision": "approved"})
    outcome = await _resume(pool, storage)

    assert isinstance(outcome, ReleaseOutcome) and outcome.park is not None, (
        f"approving the gate re-dispatches the call, which yields again: the graph re-parks, got {outcome!r}"
    )
    new_key = f"ask_user:{_SID}:{_GATED}"
    assert outcome.park.parked_event_key == new_key
    assert subagent.child_results == [], "the subagent's turn has not resumed yet: its tool is waiting again"
    await _apply_park(storage, dataclasses.replace(
        outcome.park, parked_event_keys=outcome.park.parked_event_keys or [outcome.park.parked_event_key],
    ))
    return pool, storage, subagent, new_key


@pytest.mark.asyncio
async def test_nested_reyield_then_timeout_delivers_timed_out_to_the_leaf(monkeypatch):
    pool, storage, subagent, key = await _reparked_after_an_approval(monkeypatch)

    await _bus_delivery(storage, key, make_timeout_payload())
    outcome = await _resume(pool, storage)

    (answer,) = subagent.leaf_answers()
    assert answer.get("timed_out") is True, (
        f"the re-yielded ask_user leaf was answered as an operator reply instead of a timeout: {answer!r}"
    )
    assert answer["elapsed_seconds"] >= 0
    assert outcome == "ENDED:completed"


@pytest.mark.asyncio
async def test_nested_reyield_then_cancel_delivers_cancelled_to_the_leaf(monkeypatch):
    pool, storage, subagent, key = await _reparked_after_an_approval(monkeypatch)

    await _bus_delivery(storage, key, make_cancelled_payload(reason="operator cancelled"))
    outcome = await _resume(pool, storage)

    (answer,) = subagent.leaf_answers()
    assert answer.get("cancelled") is True and answer.get("reason") == "operator cancelled", (
        f"the re-yielded ask_user leaf was answered as an operator reply instead of a cancel: {answer!r}"
    )
    assert outcome == "ENDED:completed"


# ===========================================================================
# a marker beside a real reply, on the initial graph park (d18, nested leg)
# ===========================================================================


@pytest.mark.asyncio
async def test_a_nested_leaf_timeout_beside_a_real_reply_is_typed_and_timed_from_the_park(monkeypatch):
    """Node A's subagent asked the operator; node B asked directly. A times out, B is answered, in one drain."""
    _patch_run_agent_turn(monkeypatch, {
        "agent-a": _nested(_ask_user("tc-leaf-a"), "tc-leaf-a", invoke_tcid="invoke-a"),
        "agent-b": _own_ask_user("tc-b"),
    })
    graph = _parallel_graph()
    raised = await _first_park(graph)
    storage = EngineStorageProvider()
    subagent = _Subagent()
    pool = _Pool(storage=storage, graph=graph, subagent=subagent)
    await _parked_session(storage, raised, parked_at=_now() - timedelta(seconds=900))

    await _bus_delivery(storage, f"ask_user:{_SID}:tc-leaf-a", make_timeout_payload())
    await _reply(storage, f"ask_user:{_SID}:tc-b", {"response": "blue"})
    outcome = await _resume(pool, storage)

    (answer,) = subagent.leaf_answers()
    assert answer.get("timed_out") is True, f"the nested leaf was not told it timed out: {answer!r}"
    assert 900 <= answer["elapsed_seconds"] < 960, (
        f"elapsed_seconds is measured from the park's parked_at (900 s ago), got {answer['elapsed_seconds']}"
    )
    assert pool.agent_node_payloads == [("tc-b", {"response": "blue"})], "B's real reply reaches B as the reply dict"
    assert outcome == "ENDED:completed"


@pytest.mark.asyncio
async def test_a_plain_agent_node_cancel_beside_a_nested_reply_is_typed(monkeypatch):
    """The other pairing: B's own ask_user is cancelled, A's subagent is answered."""
    _patch_run_agent_turn(monkeypatch, {
        "agent-a": _nested(_ask_user("tc-leaf-a"), "tc-leaf-a", invoke_tcid="invoke-a"),
        "agent-b": _own_ask_user("tc-b"),
    })
    graph = _parallel_graph()
    raised = await _first_park(graph)
    storage = EngineStorageProvider()
    subagent = _Subagent()
    pool = _Pool(storage=storage, graph=graph, subagent=subagent)
    await _parked_session(storage, raised, parked_at=_now() - timedelta(seconds=900))

    await _reply(storage, f"ask_user:{_SID}:tc-leaf-a", {"response": "red"})
    await _bus_delivery(storage, f"ask_user:{_SID}:tc-b", make_cancelled_payload(reason="not needed"))
    outcome = await _resume(pool, storage)

    assert subagent.leaf_answers() == [{"response": "red"}]
    ((tcid, payload),) = pool.agent_node_payloads
    assert tcid == "tc-b"
    assert not isinstance(payload, dict), f"B received the raw cancel marker dict: {payload!r}"
    assert (payload.reason, 900 <= payload.elapsed_seconds < 960) == ("not needed", True), payload
    assert outcome == "ENDED:completed"


# ===========================================================================
# a value-yielding tool_call node takes the same multi-event path
# ===========================================================================


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("marker", "expected"),
    [
        pytest.param(make_timeout_payload(), {"timed_out": True}, id="timeout"),
        pytest.param(
            make_cancelled_payload(reason="skipped"), {"cancelled": True, "reason": "skipped"}, id="cancel",
        ),
    ],
)
async def test_a_value_yield_tool_call_node_gets_the_typed_marker(marker, expected):
    """A ``tool_call`` node running ``system__ask_user``: the hook's result becomes the node's text."""
    graph = build_ask_user_graph()

    async def first_dispatcher(node, arguments):
        raise YieldToWorker(
            Yielded(tool_name="ask_user", event_key=f"ask_user:{_SID}:tc-ask", resume_metadata={"prompt": "?"}),
            tool_call_id="tc-ask",
        )

    async def resume_dispatcher(node, arguments, bypass_approval=False):  # pragma: no cover - must not run
        raise AssertionError("a value-yielding tool_call is answered by its hook, not re-dispatched")

    threads: _InMemoryStorage[GraphThread] = _InMemoryStorage(GraphThread)
    messages: _InMemoryStorage[GraphNodeMessage] = _InMemoryStorage(GraphNodeMessage)
    thread = await GraphExecutor.open_thread(graph=graph, thread_storage=threads)  # type: ignore[arg-type]
    _events, raised = await drain_until_yield(
        make_toolcall_executor(graph, thread, threads, messages, first_dispatcher).invoke([]),
    )
    assert raised is not None
    resumer = make_toolcall_executor(graph, thread, threads, messages, resume_dispatcher)

    async def factory():
        return resumer

    storage = EngineStorageProvider()
    pool = _Pool(storage=storage, graph=graph, subagent=_Subagent(), executor_factory=factory)
    await _parked_session(storage, raised, parked_at=_now())

    await _bus_delivery(storage, f"ask_user:{_SID}:tc-ask", marker)
    outcome = await _resume(pool, storage)

    answer = json.loads(resumer._context.nodes["ask"].text)
    assert {k: answer.get(k) for k in expected} == expected, f"the node was answered as an empty reply: {answer!r}"
    assert outcome == "ENDED:completed"


# ===========================================================================
# one reply per event_key: the raw-key leaf and its encoded duplicate (d16 de-dup leg; mutations N88, N95)
# ===========================================================================


_QUOTED = 'a"b'                  # a provider tool_call_id with a double quote in it
_RAW_KEY = _QUOTED               # the dispatch key older code stores the leaf under: the event_key's tail
_ENCODED_KEY = "a%22b"           # the same leaf under the percent-encoded key (leaf_key_for, S2a PR-4b)


async def _raw_and_encoded_leaves(monkeypatch, *, raw_payload: dict, encoded_payload: dict, encoded_first: bool):
    """A park holding A's reply under the raw key (written by the durable flip as it stands) and a later duplicate
    of it under the encoded key; B's gate is still unanswered, so the drain goes on past A's first entry."""
    _patch_run_agent_turn(monkeypatch, {"agent-a": _own_ask_user(_QUOTED), "agent-b": _own_ask_user("tc-b")})
    graph = _parallel_graph()
    raised = await _first_park(graph)
    storage = EngineStorageProvider()
    pool = _Pool(storage=storage, graph=graph, subagent=_Subagent())
    await _parked_session(storage, raised, parked_at=_now())

    event_key = f"ask_user:{_SID}:{_QUOTED}"
    await _reply(storage, event_key, raw_payload)
    sessions = storage.get_storage(WorkspaceSession)
    row = await sessions.get(_SID)
    state = dict(row.parked_state)
    raw_entry = state["resume_event_payloads"][_RAW_KEY]
    assert raw_entry == {"payload": raw_payload, "event_key": event_key}, "the flip stores the leaf under the raw key"
    encoded_entry = {"payload": encoded_payload, "event_key": event_key}
    state["resume_event_payloads"] = (
        {_ENCODED_KEY: encoded_entry, _RAW_KEY: raw_entry} if encoded_first
        else {_RAW_KEY: raw_entry, _ENCODED_KEY: encoded_entry}
    )
    await sessions.update(row.model_copy(update={"parked_state": state}))
    return pool, storage


@pytest.mark.asyncio
@pytest.mark.parametrize("encoded_first", [False, True], ids=["raw-first", "encoded-first"])
async def test_a_pre_upgrade_raw_key_leaf_and_its_encoded_duplicate_drain_one_reply(monkeypatch, encoded_first):
    pool, storage = await _raw_and_encoded_leaves(
        monkeypatch, raw_payload={"response": "blue"}, encoded_payload={"response": "blue"},
        encoded_first=encoded_first,
    )

    outcome = await _resume(pool, storage)

    assert pool.agent_node_payloads == [(_QUOTED, {"response": "blue"})], (
        f"the one reply was delivered {len(pool.agent_node_payloads)} times: {pool.agent_node_payloads!r}"
    )
    assert outcome == "REPARKED"
    (repark,) = pool.repark_calls
    assert [ay["node_id"] for ay in repark.graph_checkpoint["pending_agent_yields"]] == ["B"]


@pytest.mark.asyncio
@pytest.mark.parametrize("encoded_first", [False, True], ids=["raw-first", "encoded-first"])
async def test_the_raw_key_entry_is_the_one_drained_whatever_the_insertion_order(monkeypatch, encoded_first):
    """The two spellings can hold different payloads (a bus delivery is last-write-wins); the raw one is older."""
    pool, storage = await _raw_and_encoded_leaves(
        monkeypatch, raw_payload={"response": "pre-upgrade"}, encoded_payload={"response": "post-upgrade"},
        encoded_first=encoded_first,
    )

    outcome = await _resume(pool, storage)

    assert pool.agent_node_payloads == [(_QUOTED, {"response": "pre-upgrade"})], pool.agent_node_payloads
    assert outcome == "REPARKED"
