"""Two-phase park: an approval gate on a *yielding* agent-session tool.

When an approval gate sits on a yielding tool the park is two-phase:

* Phase 1 - the session parks for the operator's approval decision
  (``tool_name='_approval'``).
* Phase 2 - once APPROVED, ``_resume_tool_approval`` re-dispatches the
  real tool with ``bypass_approval=True``; if that tool is a yielding
  tool it raises :class:`YieldToWorker` a second time. The resume path
  must RE-PARK on the tool's real event key (not synthesise a fail-closed
  error tool_result), and resume to completion when the real event fires.

A REJECT still short-circuits to a clean error tool_result (the tool
never runs) - the existing fail-closed behaviour.

The same two phases hold when the gate was raised by ``system__call_tool`` on
the tool it was asked to run (pivot P1): the approved re-run runs that tool as
the resumed run, and a tool that pauses (``ask_user``) re-parks the session
under the ``call_tool`` call's id, so the answer lands on that call.

Driven end-to-end through ``WorkerPool._run_engine_session`` with a real
InMemoryClaimEngine + SessionClaimAdapter so the on_release park-column
book-keeping is exercised, mirroring tests/worker/test_engine_session_resume.py.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from primer.agent.tool_manager import ToolExecutionManager
from primer.api.registries import ProviderRegistry
from primer.claim.adapters.sessions import SessionClaimAdapter
from primer.claim.in_memory import InMemoryClaimEngine
from primer.int.claim import ClaimKind
from primer.int.toolset import ToolsetProvider
from primer.model.chat import Message, Tool, ToolCallPart, ToolResultPart
from primer.model.scheduler import WorkerConfig
from primer.model.tool_approval import ToolApprovalPolicy
from primer.model.workspace_session import (
    AgentSessionBinding,
    SessionStatus,
    WorkspaceSession,
)
from primer.model.yield_ import ToolContext, Yielded, YieldToWorker
from primer.toolset.system import build_system_toolset
from primer.worker.frames import AgentFrame, AgentResumeContext
from primer.worker.pool import WorkerPool
from primer.worker.yield_runtime import ParkedState

from tests.agent.test_inner_dispatch import USER, _Session
from tests.conftest import _FakeStorageProvider
from tests.toolset.test_call_tool_door import _Registry as _DoorRegistry
from tests.toolset.test_system import _required_policy


async def _async_return(value):
    return value


class _RecordingExecutor:
    def __init__(self, *, tool_manager=None):
        self._tool_manager = tool_manager
        self.injected: list[list[Message]] = []

    async def inject_resume_messages(self, messages):
        self.injected.append(list(messages))


class _YieldingToolManager:
    """Fake ToolExecutionManager whose execute() yields once (the real
    tool's own event) when re-dispatched with bypass_approval=True."""

    def __init__(self, *, yielded: Yielded, tool_call_id: str):
        self._yielded = yielded
        self._tool_call_id = tool_call_id
        self.calls: list[ToolCallPart] = []

    async def execute(self, call: ToolCallPart, *, bypass_approval: bool = False):
        self.calls.append(call)
        # The approved tool runs and yields for its OWN real event.
        raise YieldToWorker(self._yielded, tool_call_id=self._tool_call_id)


class _NoopPersist:
    pass


def _build_engine(session_storage) -> InMemoryClaimEngine:
    return InMemoryClaimEngine(
        adapters={
            ClaimKind.SESSION: SessionClaimAdapter(session_storage=session_storage),
        },
    )


def _build_pool(storage, engine) -> WorkerPool:
    pool = WorkerPool(
        config=WorkerConfig(concurrency=1),
        scheduler=None,                  # type: ignore[arg-type]
        storage=storage,
        workspace_registry=None,         # type: ignore[arg-type]
        provider_registry=None,          # type: ignore[arg-type]
        engine=engine,
    )
    pool._worker_id = "wrk-approval-yield"
    return pool


def _make_approval_parked_session(
    sid: str,
    *,
    tool_call_id: str,
    real_tool_name: str,
    real_arguments: dict,
    resume_event_payload: dict,
    llm_messages: list,
    scoped_tool_call_id: str | None = None,
    yielded: Yielded | None = None,
) -> WorkspaceSession:
    """A session parked (phase 1) on an approval gate guarding a yielding
    tool. resume_metadata carries the original_call so the approved resume
    re-dispatches the real tool. ``yielded`` is the real park of a caller that
    made one (``system__call_tool``'s gate); omitted, a bare gate is built."""
    parked_at = datetime.now(timezone.utc) - timedelta(seconds=5)
    if yielded is None:
        yielded = Yielded(
            tool_name="_approval",
            event_key=f"tool_approval:{sid}:{tool_call_id}",
            timeout=600.0,
            resume_metadata={
                "parked_at_iso": parked_at.isoformat(),
                "original_call": {
                    "id": tool_call_id,
                    "name": real_tool_name,
                    "arguments": real_arguments,
                },
            },
        )
    parked_state = ParkedState(
        yielded=yielded,
        llm_messages=llm_messages,
        turn_no=0,
        started_at=parked_at,
        tool_call_id=tool_call_id,
        resume_event_payload=resume_event_payload,
        scoped_tool_call_id=scoped_tool_call_id,
    )
    return WorkspaceSession(
        id=sid,
        workspace_id=f"ws-{sid}",
        binding=AgentSessionBinding(kind="agent", agent_id="ag-1"),
        status=SessionStatus.RUNNING,
        created_at=datetime.now(timezone.utc),
        turn_no=0,
        parked_status="resumable",
        parked_event_key=yielded.event_key,
        parked_until=parked_at + timedelta(seconds=600),
        parked_at=parked_at,
        parked_state=parked_state.to_jsonable(),
    )


async def _claim_session(engine, sid: str):
    await engine.mark_resumable(ClaimKind.SESSION, sid)
    leases = await engine.claim_due("wrk-approval-yield", max_count=10)
    for lease in leases:
        if lease.kind == ClaimKind.SESSION and lease.entity_id == sid:
            return lease
    raise AssertionError(f"no claimable lease for session {sid!r}")


def _assistant_msg(tool_call_id: str, real_tool_name: str) -> Message:
    return Message(
        role="assistant",
        parts=[
            ToolCallPart(
                id=tool_call_id,
                name=real_tool_name,
                arguments={"seconds": 30},
            ),
        ],
    )


@pytest.mark.asyncio
async def test_approved_yielding_tool_reparks_then_resumes(monkeypatch):
    """APPROVE an approval-gated yielding tool -> the session RE-PARKS on the
    tool's real event key (NOT an error tool_result, NOT ended). Then the real
    event fires -> the session resumes to completion via the tool resume hook."""
    sid = "sess-approval-yield-ok"
    tool_call_id = "tc-sleep"
    real_tool_name = "_test_yield_repark_tool"

    # Register a resume hook for the real tool so phase-2 resume completes.
    from primer.worker.yield_resume_registry import register_resume_hook

    class _HookResult:
        def __init__(self, output, is_error=False):
            self.output = output
            self.is_error = is_error

    def _hook(resume_metadata, payload, ctx):
        return _HookResult(json.dumps({"woke": True, "payload": payload}))

    register_resume_hook(real_tool_name, _hook)

    storage_provider = _FakeStorageProvider()
    session_storage = storage_provider.get_storage(WorkspaceSession)
    engine = _build_engine(session_storage)
    pool = _build_pool(storage_provider, engine)

    # The real tool's own yield (phase 2): a timer event, NOT an approval.
    real_event = Yielded(
        tool_name=real_tool_name,
        event_key=f"timer:{tool_call_id}",
        timeout=120.0,
        resume_metadata={"seconds": 30},
    )
    tm = _YieldingToolManager(yielded=real_event, tool_call_id=tool_call_id)
    fake_executor = _RecordingExecutor(tool_manager=tm)

    sess = _make_approval_parked_session(
        sid,
        tool_call_id=tool_call_id,
        real_tool_name=real_tool_name,
        real_arguments={"seconds": 30},
        resume_event_payload={"decision": "approved"},
        llm_messages=[_assistant_msg(tool_call_id, real_tool_name).model_dump(mode="json")],
    )
    await session_storage.create(sess)

    monkeypatch.setattr(
        pool, "_load_workspace_for_persist",
        lambda _ws_id: _async_return(_NoopPersist()),
    )
    monkeypatch.setattr(
        pool, "_build_agent_executor",
        lambda _s, _w: _async_return(fake_executor),
    )

    lease = await _claim_session(engine, sid)
    await pool._run_engine_session(lease)

    # The approved tool was re-dispatched with bypass_approval (it yielded).
    assert len(tm.calls) == 1
    # No error tool_result was injected - the session re-parked instead.
    assert fake_executor.injected == []

    row = await session_storage.get(sid)
    assert row is not None
    # Phase 2: re-parked on the tool's REAL event key, not ended/failed.
    assert row.status != SessionStatus.ENDED
    assert row.parked_status == "parked"
    assert row.parked_event_key == f"timer:{tool_call_id}"
    # The re-park stamped the real tool name into the blob (so the eventual
    # real-event resume routes to the tool's resume hook, not re-dispatch).
    assert row.parked_state["yielded"]["tool_name"] == real_tool_name
    # The in-progress turn messages were preserved across the re-park.
    assert row.parked_state["llm_messages"]


@pytest.mark.asyncio
async def test_repark_carries_scoped_tool_call_id_through_phase_2(monkeypatch):
    """01a068ea-dc95: phase 2 re-dispatches the SAME tool call directly
    (bypass_approval=True), outside the LLM-streaming pipeline that
    mints scoped ids at ToolCallStart - there is no new one to look up
    for the fresh yield, so repark_resumed_yield_outcome must carry the
    phase-1 park's scoped_tool_call_id forward unchanged. Without this,
    the eventual TOOL_RESULT (once the real event fires) would fall back
    to the raw id and stay unpaired from the durable TOOL_CALL record,
    same failure mode as the primary bug this task fixes."""
    sid = "sess-approval-yield-scoped-id"
    tool_call_id = "tc-sleep-scoped"
    real_tool_name = "_test_yield_repark_tool_scoped"
    scoped_id = "x:tool:0:1"

    from primer.worker.yield_resume_registry import register_resume_hook

    class _HookResult:
        def __init__(self, output, is_error=False):
            self.output = output
            self.is_error = is_error

    register_resume_hook(
        real_tool_name,
        lambda resume_metadata, payload, ctx: _HookResult(
            json.dumps({"woke": True}),
        ),
    )

    storage_provider = _FakeStorageProvider()
    session_storage = storage_provider.get_storage(WorkspaceSession)
    engine = _build_engine(session_storage)
    pool = _build_pool(storage_provider, engine)

    real_event = Yielded(
        tool_name=real_tool_name,
        event_key=f"timer:{tool_call_id}",
        timeout=120.0,
        resume_metadata={"seconds": 30},
    )
    tm = _YieldingToolManager(yielded=real_event, tool_call_id=tool_call_id)
    fake_executor = _RecordingExecutor(tool_manager=tm)

    sess = _make_approval_parked_session(
        sid,
        tool_call_id=tool_call_id,
        real_tool_name=real_tool_name,
        real_arguments={"seconds": 30},
        resume_event_payload={"decision": "approved"},
        llm_messages=[_assistant_msg(tool_call_id, real_tool_name).model_dump(mode="json")],
        scoped_tool_call_id=scoped_id,
    )
    await session_storage.create(sess)

    monkeypatch.setattr(
        pool, "_load_workspace_for_persist",
        lambda _ws_id: _async_return(_NoopPersist()),
    )
    monkeypatch.setattr(
        pool, "_build_agent_executor",
        lambda _s, _w: _async_return(fake_executor),
    )

    lease = await _claim_session(engine, sid)
    await pool._run_engine_session(lease)

    row = await session_storage.get(sid)
    assert row is not None
    assert row.parked_status == "parked"
    # The re-parked (phase 2) blob still carries the ORIGINAL scoped id,
    # not None and not something re-derived.
    assert row.parked_state["scoped_tool_call_id"] == scoped_id

    # ---- Phase 2 resume: the real (timer) event fires -> resume to done. ----
    # Re-stamp the row as resumable carrying the real event payload, mirroring
    # what the listener leaves behind when the timer wakes the park.
    new_blob = dict(row.parked_state)
    new_blob["resume_event_payload"] = {"woke_at": "now"}
    row2 = row.model_copy(update={
        "parked_status": "resumable",
        "parked_state": new_blob,
    })
    await session_storage.update(row2)

    # A fresh executor for the continuation (real tool has no tool_manager
    # call this time - it goes through the resume hook).
    fake_executor2 = _RecordingExecutor(tool_manager=tm)
    monkeypatch.setattr(
        pool, "_build_agent_executor",
        lambda _s, _w: _async_return(fake_executor2),
    )
    lease2 = await _claim_session(engine, sid)
    await pool._run_engine_session(lease2)

    # The resume hook produced a tool_result that got injected (turn continues).
    assert len(fake_executor2.injected) == 1
    injected = fake_executor2.injected[0]
    assert injected[-1].role == "tool"
    tool_part = next(p for p in injected[-1].parts if isinstance(p, ToolResultPart))
    assert tool_part.id == tool_call_id
    assert tool_part.error is False
    body = json.loads(tool_part.output)
    assert body["woke"] is True

    row3 = await session_storage.get(sid)
    assert row3 is not None
    assert row3.parked_status is None  # park cleared, continuation runs
    assert row3.status != SessionStatus.ENDED


@pytest.mark.asyncio
async def test_rejected_yielding_tool_short_circuits_clean_error(monkeypatch):
    """REJECT the approval on a yielding tool -> a clean error tool_result is
    injected; the real tool never runs, the session is NOT re-parked."""
    sid = "sess-approval-yield-reject"
    tool_call_id = "tc-sleep-r"
    real_tool_name = "_test_yield_repark_tool_r"

    storage_provider = _FakeStorageProvider()
    session_storage = storage_provider.get_storage(WorkspaceSession)
    engine = _build_engine(session_storage)
    pool = _build_pool(storage_provider, engine)

    # If execute() were ever called, it would yield - assert it is NOT.
    real_event = Yielded(tool_name=real_tool_name, event_key=f"timer:{tool_call_id}")
    tm = _YieldingToolManager(yielded=real_event, tool_call_id=tool_call_id)
    fake_executor = _RecordingExecutor(tool_manager=tm)

    sess = _make_approval_parked_session(
        sid,
        tool_call_id=tool_call_id,
        real_tool_name=real_tool_name,
        real_arguments={"seconds": 30},
        resume_event_payload={"decision": "rejected", "reason": "no thanks"},
        llm_messages=[_assistant_msg(tool_call_id, real_tool_name).model_dump(mode="json")],
    )
    await session_storage.create(sess)

    monkeypatch.setattr(
        pool, "_load_workspace_for_persist",
        lambda _ws_id: _async_return(_NoopPersist()),
    )
    monkeypatch.setattr(
        pool, "_build_agent_executor",
        lambda _s, _w: _async_return(fake_executor),
    )

    lease = await _claim_session(engine, sid)
    await pool._run_engine_session(lease)

    # The real tool was NEVER dispatched.
    assert tm.calls == []
    # A clean error tool_result was injected (short-circuit, not re-park).
    assert len(fake_executor.injected) == 1
    injected = fake_executor.injected[0]
    tool_part = next(p for p in injected[-1].parts if isinstance(p, ToolResultPart))
    assert tool_part.error is True
    body = json.loads(tool_part.output)
    assert body["rejected"] is True
    assert "no thanks" in body["reason"]

    row = await session_storage.get(sid)
    assert row is not None
    # NOT re-parked - the continuation turn runs (park cleared).
    assert row.parked_status is None
    assert row.status != SessionStatus.ENDED


@pytest.mark.asyncio
async def test_a_gated_ask_user_through_call_tool_reparks_and_the_answer_lands_on_the_call_tool_call(monkeypatch):
    """The whole round trip, through the real pieces: a USER-role run asks ``system__call_tool`` for the gated ``ask_user``; the gate parks (phase 1, written by the real handler); the
    approval runs the inner tool as the resumed run, which pauses the turn for the user's answer, and the session re-parks on that question (phase 2, under the ``call_tool`` call's
    id, naming ``ask_user``); the answer is then delivered to the ``call_tool`` call. Before pivot P1 the approved re-run had no context, so ``ask_user`` could not even start."""
    sid = "sess-call-tool-ask"
    tool_call_id = "tc-call-tool"              # the id of the model's system__call_tool call
    arguments = {"toolset_id": "system", "tool_name": "ask_user", "arguments": {"prompt": "Proceed?"}}

    storage_provider = _FakeStorageProvider()
    registry = ProviderRegistry(
        storage_provider,  # type: ignore[arg-type]
        llm_factory=lambda p: object(),  # type: ignore[arg-type,return-value]
        embedder_factory=lambda p: object(),  # type: ignore[arg-type,return-value]
        cross_encoder_factory=lambda p: object(),  # type: ignore[arg-type,return-value]
        toolset_factory=lambda p: object(),  # type: ignore[arg-type,return-value]
    )
    system_toolset = build_system_toolset(storage_provider=storage_provider, provider_registry=registry)  # type: ignore[arg-type]
    registry._system_toolset_provider = system_toolset  # type: ignore[attr-defined]
    await storage_provider.get_storage(ToolApprovalPolicy).create(_required_policy("system", "ask_user"))

    # Phase 1: the park the real call_tool handler writes for the gated inner tool.
    ctx = ToolContext(tool_call_id=tool_call_id, session_id=sid, workspace_id=f"ws-{sid}", initiated_by=USER)
    with pytest.raises(YieldToWorker) as gate:
        await system_toolset.call(tool_name="call_tool", ctx=ctx, arguments=arguments)
    assert gate.value.yielded.tool_name == "_approval" and gate.value.tool_call_id == tool_call_id

    session_storage = storage_provider.get_storage(WorkspaceSession)
    engine = _build_engine(session_storage)
    pool = _build_pool(storage_provider, engine)
    manager = ToolExecutionManager.for_workspace(
        toolset_providers={"system": system_toolset}, session=_Session(sid, f"ws-{sid}"), initiated_by=USER, provider_registry=registry,
    )
    outer_call = Message(role="assistant", parts=[ToolCallPart(id=tool_call_id, name="system__call_tool", arguments=arguments)])
    await session_storage.create(_make_approval_parked_session(
        sid,
        tool_call_id=tool_call_id,
        real_tool_name="ask_user",
        real_arguments=arguments["arguments"],
        resume_event_payload={"decision": "approved"},
        llm_messages=[outer_call.model_dump(mode="json")],
        yielded=gate.value.yielded,
    ))
    fake_executor = _RecordingExecutor(tool_manager=manager)
    monkeypatch.setattr(pool, "_load_workspace_for_persist", lambda _ws_id: _async_return(_NoopPersist()))
    monkeypatch.setattr(pool, "_build_agent_executor", lambda _s, _w: _async_return(fake_executor))

    await pool._run_engine_session(await _claim_session(engine, sid))

    # Phase 2: the approved ask_user paused the turn. No error result was injected; the session waits for the answer under the call_tool call's id.
    assert fake_executor.injected == [], "the pause was swallowed into an error result instead of parking the session again"
    row = await session_storage.get(sid)
    assert row is not None and row.status != SessionStatus.ENDED
    assert row.parked_status == "parked"
    assert row.parked_event_key == f"ask_user:{sid}:{tool_call_id}"
    assert row.parked_state["yielded"]["tool_name"] == "ask_user", "the answer will be routed to ask_user's resume hook"
    assert row.parked_state["tool_call_id"] == tool_call_id
    assert row.parked_state["yielded"]["resume_metadata"]["prompt"] == "Proceed?"

    # The user answers: the hook found by the parked tool name turns it into the result of the call_tool call.
    answered = row.model_copy(update={"parked_status": "resumable", "parked_state": {**row.parked_state, "resume_event_payload": {"response": "yes"}}})
    await session_storage.update(answered)
    fake_executor2 = _RecordingExecutor(tool_manager=manager)
    monkeypatch.setattr(pool, "_build_agent_executor", lambda _s, _w: _async_return(fake_executor2))
    await pool._run_engine_session(await _claim_session(engine, sid))

    (injected,) = fake_executor2.injected
    assert injected[0].parts[0].id == tool_call_id and injected[0].parts[0].name == "system__call_tool", "the turn continues from the call_tool call"
    result = next(p for p in injected[-1].parts if isinstance(p, ToolResultPart))
    assert result.id == tool_call_id and result.error is False
    assert json.loads(result.output) == {"response": "yes"}
    done = await session_storage.get(sid)
    assert done is not None and done.parked_status is None and done.status != SessionStatus.ENDED


# ---- an approved tool whose CHILD run pauses (held: strict xfail) -----------------------------------------------------------------------------------


class _DelegatesAndPauses(ToolsetProvider):
    """Toolset ``rec`` with one role-user tool, ``delegate``, standing in for ``invoke_agent``: it starts a child run that asks the user a question,
    and raises the yield ``invoke_agent`` raises then (the child's question, the child's frame on it keyed by THIS call's id)."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def required_role(self, tool_name: str) -> str:
        return "user"

    async def list_tools(self, *, principal=None):
        yield Tool(
            id="delegate", description="delegates to a child run", toolset_id="rec",
            args_schema={"type": "object", "properties": {}, "additionalProperties": True},
        )

    async def call(self, *, tool_name, arguments, principal=None, ctx=None):
        self.calls.append(ctx.tool_call_id)
        paused = YieldToWorker(
            Yielded(
                tool_name="ask_user", event_key=f"ask_user:{ctx.session_id}:child-call", timeout=600.0,
                resume_metadata={"prompt": "Which one?"},
            ),
            tool_call_id="child-call",
        )
        paused.frames = [AgentFrame(
            agent_id="child", llm_messages=[], tool_call_id=ctx.tool_call_id, depth=1,
            context=AgentResumeContext(ctx.session_id, ctx.workspace_id, None, None, [], initiated_by=ctx.initiated_by),
        )]
        raise paused


async def _approve_a_tool_whose_child_run_pauses(monkeypatch, *, through_call_tool: bool):
    """A user's session approves a gated tool that delegates, and the child run pauses. Driven through the real pool resume and the real tool
    manager, on the park the real ``call_tool`` handler writes (``through_call_tool``) or on the agent's own gated call of ``rec__delegate``.
    Returns the session row after the resume, the session's executor, the delegating toolset and the session's call id."""
    sid = f"sess-nested-pause-{'call-tool' if through_call_tool else 'direct'}"
    outer = "tc-outer"
    storage_provider = _FakeStorageProvider()
    provider = _DelegatesAndPauses()
    real = ProviderRegistry(
        storage_provider,  # type: ignore[arg-type]
        llm_factory=lambda p: object(),  # type: ignore[arg-type,return-value]
        embedder_factory=lambda p: object(),  # type: ignore[arg-type,return-value]
        cross_encoder_factory=lambda p: object(),  # type: ignore[arg-type,return-value]
        toolset_factory=lambda p: object(),  # type: ignore[arg-type,return-value]
    )
    registry = _DoorRegistry(real, {"rec": provider})
    yielded = None
    if through_call_tool:
        system_toolset = build_system_toolset(storage_provider=storage_provider, provider_registry=registry)  # type: ignore[arg-type]
        real._system_toolset_provider = system_toolset  # type: ignore[attr-defined]
        await storage_provider.get_storage(ToolApprovalPolicy).create(_required_policy("rec", "delegate"))
        arguments = {"toolset_id": "rec", "tool_name": "delegate", "arguments": {}}
        ctx = ToolContext(tool_call_id=outer, session_id=sid, workspace_id=f"ws-{sid}", initiated_by=USER)
        with pytest.raises(YieldToWorker) as gate:
            await system_toolset.call(tool_name="call_tool", ctx=ctx, arguments=arguments)
        yielded, parked_name = gate.value.yielded, "delegate"
        outer_call = ToolCallPart(id=outer, name="system__call_tool", arguments=arguments)
        manager = ToolExecutionManager.for_workspace(
            toolset_providers={"system": system_toolset}, session=_Session(sid, f"ws-{sid}"), initiated_by=USER, provider_registry=registry,
        )
    else:
        parked_name = "rec__delegate"
        outer_call = ToolCallPart(id=outer, name=parked_name, arguments={})
        manager = ToolExecutionManager.for_workspace(
            toolset_providers={"rec": provider}, session=_Session(sid, f"ws-{sid}"), initiated_by=USER, tools=[parked_name],
        )
    session_storage = storage_provider.get_storage(WorkspaceSession)
    engine = _build_engine(session_storage)
    pool = _build_pool(storage_provider, engine)
    await session_storage.create(_make_approval_parked_session(
        sid,
        tool_call_id=outer,
        real_tool_name=parked_name,
        real_arguments={},
        resume_event_payload={"decision": "approved"},
        llm_messages=[Message(role="assistant", parts=[outer_call]).model_dump(mode="json")],
        yielded=yielded,
    ))
    executor = _RecordingExecutor(tool_manager=manager)
    monkeypatch.setattr(pool, "_load_workspace_for_persist", lambda _ws_id: _async_return(_NoopPersist()))
    monkeypatch.setattr(pool, "_build_agent_executor", lambda _s, _w: _async_return(executor))

    await pool._run_engine_session(await _claim_session(engine, sid))
    return await session_storage.get(sid), executor, provider, outer


_NESTED_PAUSE_SHAPES = pytest.mark.parametrize(
    "through_call_tool", [True, False], ids=["the-inner-tool-of-call-tool", "a-directly-gated-delegating-tool"],
)


@pytest.mark.asyncio
@_NESTED_PAUSE_SHAPES
async def test_an_approved_tool_whose_child_run_pauses_reaches_the_childs_pause(monkeypatch, through_call_tool: bool):
    """SCENARIO (holds whatever the session path does with it): the approval re-ran the delegating tool once, under the session's call id, and the
    child run's pause reached the session path."""
    row, _executor, provider, outer = await _approve_a_tool_whose_child_run_pauses(monkeypatch, through_call_tool=through_call_tool)

    assert provider.calls == [outer]
    assert row is not None and row.status != SessionStatus.ENDED


@pytest.mark.asyncio
@_NESTED_PAUSE_SHAPES
async def test_an_approved_tool_whose_child_run_pauses_is_refused_with_an_error_on_the_session_path(monkeypatch, through_call_tool: bool):
    """What the session path does until the held fix lands (audit G6-3): the coordinator's flat re-park keeps no frames, so a pause inside a nested
    agent or graph run after an approval is REFUSED, the same way for both shapes. The session's own call is answered with an error result, and the
    session is not parked on the child's question.

    The directly gated shape used to be re-parked flat, under the CHILD's call id: when the person answered the child's question, the answer was
    injected as a tool_result for ``child-call`` into a transcript whose pending call is the session's own, the session's call was never answered,
    and the child run was lost."""
    row, executor, provider, outer = await _approve_a_tool_whose_child_run_pauses(monkeypatch, through_call_tool=through_call_tool)

    assert provider.calls == [outer], "the approved tool ran once"
    ((*_assistant, answer),) = executor.injected
    (part,) = answer.parts
    assert part.id == outer and part.error is True, part
    body = json.loads(part.output)
    assert body["is_error"] is True and "nested agent or graph run" in body["reason"], body
    assert row is not None and row.parked_status != "parked", "the session must not wait on the child's question"
    assert (row.parked_state or {}).get("tool_call_id") != "child-call"


@pytest.mark.asyncio
@pytest.mark.xfail(
    strict=True,
    raises=AssertionError,
    reason=(
        "held: the session path's re-park (repark_resumed_yield_outcome) keeps no frames, so a pause inside a nested agent or graph run after an "
        "approval is refused with an error result, through call_tool and on a directly gated delegating tool alike (audit G6-3)"
    ),
)
@_NESTED_PAUSE_SHAPES
async def test_an_approved_tool_whose_child_run_pauses_parks_the_session_on_the_childs_question(monkeypatch, through_call_tool: bool):
    """BEHAVIOUR a fix must reach: the session parks again on the child's question with the child's frame kept under the session's call, so the
    answer unwinds through the child run to that call. Today both shapes are refused with an error result: a call_tool re-run in
    ``_resume_tool_approval``, a directly gated delegating tool in the coordinator (it used to be parked flat without the frame, so the child's
    answer was delivered to the child's own call id)."""
    row, executor, _provider, outer = await _approve_a_tool_whose_child_run_pauses(monkeypatch, through_call_tool=through_call_tool)

    state = (row.parked_state if row is not None else None) or {}
    broken = []
    if executor.injected:
        broken.append(f"the session's call was answered instead of waiting for the child: {executor.injected[0][-1].parts[0].output}")
    if row is None or row.parked_status != "parked":
        broken.append(f"the session is not parked again: parked_status={getattr(row, 'parked_status', None)!r}")
    if (state.get("yielded") or {}).get("tool_name") != "ask_user":
        broken.append(f"the park is not the child's question: {state.get('yielded')}")
    kept = [frame.get("tool_call_id") for frame in state.get("frames") or []]
    if kept != [outer]:
        broken.append(f"the child's frame is not kept under the session's call {outer!r}: frames keyed {kept}")
    assert not broken, broken
