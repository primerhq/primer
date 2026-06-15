"""Task 7.1 - faithful end-to-end nested-yield park/resume matrix.

The layer units (apply_leaf, resume_subagent, AgentFrame/GraphFrame.resume +
resume_leaf, resume_continuation, run_subagent's frame push, the GraphFrame
two-id descent) are each already covered in isolation. This module proves they
COMPOSE through the REAL worker park/resume path: a resumable session row whose
``parked_state`` carries a non-empty frame stack is claimed and driven through
``WorkerPool._run_engine_session`` -> the ``if parked.frames:`` continuation
branch -> the REAL ``resume_continuation`` walk -> the REAL ``resume_subagent``
(rebuilt over a fake-toolset-backed ToolExecutionManager + scripted LLM) -> the
shared inject/continue tail (or a real re-park).

Faithfulness: every scenario here runs the FULL session-worker loop
(``pool._run_engine_session(lease)`` over a real InMemoryClaimEngine + real
SessionClaimAdapter + real fake storage). The ONLY things stubbed are:

* ``_load_workspace_for_persist`` (returns a noop persist holder), and
* ``_build_agent_executor`` for the SESSION's own turn (returns a recording
  executor exposing ``inject_resume_messages`` + a ``_tool_manager`` slot) -
  exactly as ``tests/worker/test_engine_session_resume.py`` does.

The subagent leg is NOT stubbed: the pool's ``_build_invocation_services`` reads
the pool's real ``_storage`` / ``_provider_registry`` / ``_approval_resolver``,
so the continuation walk re-dispatches the approval-gated tool, runs the
ask_user resume hook, and re-runs the subagent LLM turn for real. The fakes here
are the LLM (scripted streams) and the toolset providers - the same fakes the
``tests/agent/test_*subagent*`` unit suites use to drive ``resume_subagent``.

Scenario 5 (graph agent-node -> subagent -> ask_user) is intentionally NOT
duplicated here: ``tests/worker/test_graph_node_subagent_resume.py`` already
drives the faithful graph-session re-descent (real ``resume_continuation`` over
a graph checkpoint's nested entry). See the note at the bottom of this file.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

from primer.agent.approval import ApprovalResolver
from primer.claim.adapters.sessions import SessionClaimAdapter
from primer.claim.in_memory import InMemoryClaimEngine
from primer.int.claim import ClaimKind
from primer.model.agent import Agent, AgentModel
from primer.model.chat import (
    Done,
    Message,
    StreamStart,
    TextDelta,
    Tool,
    ToolCallEnd,
    ToolCallPart,
    ToolCallResult,
    ToolCallStart,
    ToolResultPart,
)
from primer.model.provider import LLMModel
from primer.model.scheduler import WorkerConfig
from primer.model.tool_approval import (
    RequiredApprovalConfig,
    ToolApprovalPolicy,
)
from primer.model.workspace_session import (
    AgentSessionBinding,
    SessionStatus,
    WorkspaceSession,
)
from primer.model.yield_ import Yielded, YieldToWorker
from primer.worker.frames import AgentFrame, AgentResumeContext
from primer.worker.pool import WorkerPool
from primer.worker.yield_runtime import ParkedState

from tests.conftest import _FakeStorageProvider

# Register the ask_user + sleep resume hooks (module-import side effects).
import primer.toolset.system  # noqa: F401,E402  (register_resume_hook("ask_user"))
import primer.toolset.misc  # noqa: F401,E402  (register_resume_hook("sleep"))


# ===========================================================================
# Fakes (mirror tests/agent/test_subagent_resume.py + test_run_subagent_yield)
# ===========================================================================


class _ScriptedLLM:
    """Stub LLM emitting one scripted stream per turn, in order.

    ``resume_subagent`` re-runs the subagent's LLM turn; each call to
    ``stream`` pops the next scripted event list, so a single provider can
    drive both an initial yield-resume and a follow-on completion turn.
    """

    def __init__(self, *, scripts: list[list]) -> None:
        self._scripts = list(scripts)

    def stream(self, *, model, messages, **kwargs):  # noqa: ANN001
        events = self._scripts.pop(0)

        async def _gen() -> AsyncIterator:
            for ev in events:
                yield ev

        return _gen()


class _PlainToolset:
    """Toolset 't1' with a non-yielding tool 'do_it'."""

    async def list_tools(self, *, principal: str | None = None) -> AsyncIterator[Tool]:
        yield Tool(
            id="do_it",
            description="does the thing",
            toolset_id="t1",
            args_schema={"type": "object", "properties": {}, "additionalProperties": True},
        )

    def is_yielding(self, tool_name: str) -> bool:
        return False

    async def call(self, *, tool_name, arguments, principal=None, ctx=None) -> ToolCallResult:  # noqa: ANN001
        return ToolCallResult(output="did the thing", is_error=False)


class _GatedYieldingToolset:
    """Toolset 't1' with 'do_wait': both approval-gated AND yielding.

    On the FIRST (gated) dispatch the approval gate raises ``_approval``
    before the body runs. After APPROVE the body runs with
    ``bypass_approval=True`` and ITSELF yields a timer leaf (scenario 4).
    """

    async def list_tools(self, *, principal: str | None = None) -> AsyncIterator[Tool]:
        yield Tool(
            id="do_wait",
            description="gated and yielding",
            toolset_id="t1",
            args_schema={"type": "object", "properties": {}, "additionalProperties": True},
        )

    def is_yielding(self, tool_name: str) -> bool:
        return tool_name == "do_wait"

    async def call(self, *, tool_name, arguments, principal=None, ctx=None) -> ToolCallResult:  # noqa: ANN001
        assert ctx is not None
        raise YieldToWorker(
            Yielded(
                tool_name="sleep",
                event_key=f"timer:{ctx.session_id}:{ctx.tool_call_id}",
                resume_metadata={},
            ),
            tool_call_id=ctx.tool_call_id,
        )


class _ProviderRow:
    """Lightweight LLM-provider stand-in: resume_subagent only reads .models."""

    def __init__(self, models: list[LLMModel]) -> None:
        self.models = models


class _Store:
    def __init__(self, obj: Any) -> None:
        self._obj = obj

    async def get(self, _id: str) -> Any:
        return self._obj


class _AgentStorageProvider:
    """Storage facade for the subagent leg: serves Agent + LLMProvider rows.

    Wraps the real _FakeStorageProvider so the WorkspaceSession storage the
    pool/engine use is the SAME one the session row lives in, while Agent /
    LLMProvider lookups (driven by resume_subagent) resolve to our fakes.
    """

    def __init__(self, *, base: _FakeStorageProvider, agent: Agent, provider_row: _ProviderRow) -> None:
        self._base = base
        self._agent = agent
        self._provider_row = provider_row

    def get_storage(self, cls: type) -> Any:
        from primer.model.provider import LLMProvider

        if cls is Agent:
            return _Store(self._agent)
        if cls is LLMProvider:
            return _Store(self._provider_row)
        return self._base.get_storage(cls)


class _ProviderRegistry:
    def __init__(self, *, llm: _ScriptedLLM, toolset: Any) -> None:
        self._llm = llm
        self._toolset = toolset

    async def get_llm(self, _provider_id: str) -> _ScriptedLLM:
        return self._llm

    async def get_toolset(self, _toolset_id: str) -> Any:
        return self._toolset


class _PoliciesOnlyResolver(ApprovalResolver):
    """Approval resolver returning fixed policies without storage."""

    def __init__(self, policies: list[ToolApprovalPolicy]) -> None:
        self._policies = policies
        self._ttl = 60.0
        self._cache = {}

    async def find(self, *, toolset_id, tool_name):  # noqa: ANN001
        for p in self._policies:
            if p.toolset_id == toolset_id and p.tool_name == tool_name:
                return p
        return None


class _RecordingExecutor:
    """Session's own executor: records inject_resume_messages + a _tool_manager."""

    def __init__(self, *, tool_manager=None):
        self._tool_manager = tool_manager
        self.injected: list[list[Message]] = []

    async def inject_resume_messages(self, messages):
        self.injected.append(list(messages))


class _NoopPersist:
    pass


# ===========================================================================
# Builders
# ===========================================================================


async def _async_return(value):
    return value


def _provider_row() -> _ProviderRow:
    return _ProviderRow(models=[LLMModel(name="m1", context_length=128_000)])


def _agent(*, tools: list[str]) -> Agent:
    return Agent(
        id="agent-sub",
        description="subagent",
        model=AgentModel(provider_id="prov-1", model_name="m1"),
        system_prompt=["you are a subagent"],
        tools=tools,
    )


def _final_text_script(text: str) -> list:
    return [
        StreamStart(model="m1"),
        TextDelta(text=text, index=0),
        Done(stop_reason="stop", raw_reason="stop"),
    ]


def _tool_call_script(scoped_name: str, call_id: str) -> list:
    return [
        StreamStart(model="m1"),
        ToolCallStart(id=call_id, name=scoped_name, index=0),
        ToolCallEnd(id=call_id, arguments={}, index=0),
        Done(stop_reason="tool_use", raw_reason="tool_use"),
    ]


def _sub_history(scoped_name: str, call_id: str) -> list[dict]:
    """The subagent's mid-flight history: the assistant turn that emitted the
    tool_use which parked, as serialised dicts (as on an AgentFrame)."""
    assistant = Message(
        role="assistant",
        parts=[ToolCallPart(id=call_id, name=scoped_name, arguments={})],
    )
    return [assistant.model_dump(mode="json")]


def _context(*, tools: list[str], sid: str) -> AgentResumeContext:
    return AgentResumeContext(
        session_id=sid,
        workspace_id=f"ws-{sid}",
        chat_id=None,
        principal="user-1",
        tools=tools,
    )


def _agent_frame(
    *, sid: str, invoke_tcid: str, sub_history: list[dict], tools: list[str], depth: int = 0
) -> AgentFrame:
    return AgentFrame(
        agent_id="agent-sub",
        llm_messages=sub_history,
        tool_call_id=invoke_tcid,
        depth=depth,
        context=_context(tools=tools, sid=sid),
    )


def _build_pool(*, base_storage, agent_storage, registry, resolver) -> tuple[WorkerPool, InMemoryClaimEngine]:
    """A real pool + engine.

    The pool's WorkspaceSession storage (used by the engine/adapter) comes from
    ``agent_storage`` (which delegates that lookup to ``base_storage``), so the
    session row read on release is the row created in the test. The pool's
    ``_storage`` is the agent_storage facade so the continuation walk's
    Agent/LLMProvider lookups resolve.
    """
    session_storage = base_storage.get_storage(WorkspaceSession)
    engine = InMemoryClaimEngine(
        adapters={ClaimKind.SESSION: SessionClaimAdapter(session_storage=session_storage)},
    )
    pool = WorkerPool(
        config=WorkerConfig(concurrency=1),
        scheduler=None,  # type: ignore[arg-type]
        storage=agent_storage,
        workspace_registry=None,  # type: ignore[arg-type]
        provider_registry=registry,
        engine=engine,
        approval_resolver=resolver,
    )
    pool._worker_id = "wrk-nested-yield"
    return pool, engine


def _make_session(
    sid: str,
    *,
    leaf: Yielded,
    leaf_tcid: str,
    invoke_tcid: str,
    frames: list[AgentFrame],
    resume_event_payload: dict | None,
    session_history: list[dict] | None = None,
    turn_no: int = 0,
) -> WorkspaceSession:
    """Build a resumable agent session row parked on ``leaf`` with ``frames``.

    Mirrors what primer/session/dispatch.py persists: ``parked.yielded`` is the
    innermost LEAF yield, ``parked.tool_call_id`` is the leaf's tcid,
    ``parked.frames`` is the root-first caller stack, and ``parked.llm_messages``
    is the SESSION's own in-progress turn (the invoke_agent tool_use).
    """
    parked_at = datetime.now(timezone.utc) - timedelta(seconds=5)
    stamped = Yielded(
        tool_name=leaf.tool_name,
        event_key=leaf.event_key,
        timeout=leaf.timeout if leaf.timeout is not None else 600.0,
        resume_metadata={**(leaf.resume_metadata or {}), "parked_at_iso": parked_at.isoformat()},
    )
    if session_history is None:
        sess_turn = Message(
            role="assistant",
            parts=[ToolCallPart(id=invoke_tcid, name="system__invoke_agent", arguments={})],
        )
        session_history = [sess_turn.model_dump(mode="json")]

    parked_state = ParkedState(
        yielded=stamped,
        llm_messages=session_history,
        turn_no=turn_no,
        started_at=parked_at,
        tool_call_id=leaf_tcid,
        resume_event_payload=resume_event_payload,
        frames=list(frames),
    )

    return WorkspaceSession(
        id=sid,
        workspace_id=f"ws-{sid}",
        binding=AgentSessionBinding(kind="agent", agent_id="agent-sub"),
        status=SessionStatus.RUNNING,
        created_at=datetime.now(timezone.utc),
        turn_no=turn_no,
        parked_status="resumable",
        parked_event_key=leaf.event_key,
        parked_until=parked_at + timedelta(seconds=600),
        parked_at=parked_at,
        parked_state=parked_state.to_jsonable(),
    )


async def _claim(engine, sid: str):
    await engine.mark_resumable(ClaimKind.SESSION, sid)
    leases = await engine.claim_due("wrk-nested-yield", max_count=10)
    for lease in leases:
        if lease.kind == ClaimKind.SESSION and lease.entity_id == sid:
            return lease
    raise AssertionError(f"no claimable lease for {sid!r}")


def _wire_session_executor(pool, monkeypatch):
    executor = _RecordingExecutor()
    monkeypatch.setattr(pool, "_load_workspace_for_persist", lambda _w: _async_return(_NoopPersist()))
    monkeypatch.setattr(pool, "_build_agent_executor", lambda _s, _w: _async_return(executor))
    return executor


def _injected_tool_result(executor: _RecordingExecutor, expect_id: str) -> ToolResultPart:
    """Pull the ToolResultPart the continuation delivered into the session turn."""
    assert len(executor.injected) == 1, "exactly one inject_resume_messages call"
    msgs = executor.injected[0]
    assert msgs[-1].role == "tool"
    part = next(p for p in msgs[-1].parts if isinstance(p, ToolResultPart))
    assert part.id == expect_id, f"delivered tool_result keyed by {expect_id!r}"
    return part


def _approval_leaf(*, sid: str, gated_call_id: str, gated_name: str) -> Yielded:
    """The ``_approval`` leaf an approval gate raised inside the subagent."""
    return Yielded(
        tool_name="_approval",
        event_key=f"tool_approval:{sid}:{gated_call_id}",
        timeout=600.0,
        resume_metadata={
            "policy_id": "p",
            "original_call": {"id": gated_call_id, "name": gated_name, "arguments": {}},
        },
    )


# ===========================================================================
# Scenario 1: approval-gated tool inside a subagent (APPROVE + REJECT)
# ===========================================================================


@pytest.mark.asyncio
async def test_subagent_approval_gate_approve_redispatches_and_delivers(monkeypatch):
    """FULL session loop. Session called invoke_agent; the subagent called an
    approval-gated tool 't1__do_it'; the session PARKED on ``tool_approval:*``
    with ``frames=[AgentFrame]``. Deliver APPROVE: apply_leaf re-dispatches the
    real gated tool (bypass_approval), the subagent's LLM turn continues to
    final text, and the session's invoke_agent tool_call receives the result."""
    sid = "sess-sub-approve"
    invoke_tcid = "invoke-tc-1"
    gated_call_id = "gated-call-1"

    # Subagent: re-dispatch 't1__do_it' (runs, returns), then LLM produces text.
    llm = _ScriptedLLM(scripts=[_final_text_script("approved and done")])
    registry = _ProviderRegistry(llm=llm, toolset=_PlainToolset())
    resolver = _PoliciesOnlyResolver(
        [ToolApprovalPolicy(id="p", toolset_id="t1", tool_name="do_it", approval=RequiredApprovalConfig())]
    )
    base = _FakeStorageProvider()
    agent_storage = _AgentStorageProvider(base=base, agent=_agent(tools=["t1__do_it"]), provider_row=_provider_row())
    pool, engine = _build_pool(base_storage=base, agent_storage=agent_storage, registry=registry, resolver=resolver)

    frame = _agent_frame(
        sid=sid, invoke_tcid=invoke_tcid,
        sub_history=_sub_history("t1__do_it", gated_call_id), tools=["t1__do_it"],
    )
    sess = _make_session(
        sid,
        leaf=_approval_leaf(sid=sid, gated_call_id=gated_call_id, gated_name="t1__do_it"),
        leaf_tcid=gated_call_id, invoke_tcid=invoke_tcid, frames=[frame],
        resume_event_payload={"decision": "approved"},
    )
    await base.get_storage(WorkspaceSession).create(sess)

    # PARK shape: a single AgentFrame + an _approval leaf keyed tool_approval:*.
    rehydrated = ParkedState.from_jsonable(sess.parked_state)
    assert rehydrated.yielded.tool_name == "_approval"
    assert rehydrated.yielded.event_key == f"tool_approval:{sid}:{gated_call_id}"
    assert len(rehydrated.frames) == 1 and rehydrated.frames[0].kind == "agent"

    executor = _wire_session_executor(pool, monkeypatch)
    await pool._run_engine_session(await _claim(engine, sid))

    # RESUME: the result delivered to the session is keyed by the INVOKE_AGENT
    # call id (the outermost frame's tool_call_id), carries the subagent's final
    # text, and is NOT an error.
    part = _injected_tool_result(executor, invoke_tcid)
    assert part.error is False
    assert json.loads(part.output) == {"output": "approved and done"}

    row = await base.get_storage(WorkspaceSession).get(sid)
    assert row.parked_status is None  # park cleared on release
    assert row.status != SessionStatus.ENDED


@pytest.mark.asyncio
async def test_subagent_approval_gate_reject_clean_error_tool_never_runs(monkeypatch):
    """REJECT variant. Deliver a rejection: apply_leaf synthesises a clean
    fail-closed error tool_result, the gated tool body NEVER runs, the subagent
    LLM turn continues (with the error result) to a final text, and the session
    gets a (non-error continuation) delivery. The toolset's 'call' is wired to
    blow up so a stray re-dispatch would fail loudly."""
    sid = "sess-sub-reject"
    invoke_tcid = "invoke-tc-2"
    gated_call_id = "gated-call-2"

    class _ExplodingToolset(_PlainToolset):
        async def call(self, *, tool_name, arguments, principal=None, ctx=None):  # noqa: ANN001
            raise AssertionError("rejected tool body must NOT run")

    # Subagent continues after the error tool_result -> final text.
    llm = _ScriptedLLM(scripts=[_final_text_script("ok it was rejected")])
    registry = _ProviderRegistry(llm=llm, toolset=_ExplodingToolset())
    resolver = _PoliciesOnlyResolver(
        [ToolApprovalPolicy(id="p", toolset_id="t1", tool_name="do_it", approval=RequiredApprovalConfig())]
    )
    base = _FakeStorageProvider()
    agent_storage = _AgentStorageProvider(base=base, agent=_agent(tools=["t1__do_it"]), provider_row=_provider_row())
    pool, engine = _build_pool(base_storage=base, agent_storage=agent_storage, registry=registry, resolver=resolver)

    frame = _agent_frame(
        sid=sid, invoke_tcid=invoke_tcid,
        sub_history=_sub_history("t1__do_it", gated_call_id), tools=["t1__do_it"],
    )
    sess = _make_session(
        sid,
        leaf=_approval_leaf(sid=sid, gated_call_id=gated_call_id, gated_name="t1__do_it"),
        leaf_tcid=gated_call_id, invoke_tcid=invoke_tcid, frames=[frame],
        resume_event_payload={"decision": "rejected", "reason": "no thanks"},
    )
    await base.get_storage(WorkspaceSession).create(sess)

    executor = _wire_session_executor(pool, monkeypatch)
    await pool._run_engine_session(await _claim(engine, sid))

    # The subagent finished (its rejection-aware turn produced text); the session
    # delivery is keyed by the invoke_agent id. The REJECTION manifested as the
    # leaf's error tool_result fed INTO the subagent (its body never ran - the
    # exploding toolset proves it), and the subagent's own completion is clean.
    part = _injected_tool_result(executor, invoke_tcid)
    assert json.loads(part.output) == {"output": "ok it was rejected"}


# ===========================================================================
# Scenario 2: ask_user inside a subagent
# ===========================================================================


@pytest.mark.asyncio
async def test_subagent_ask_user_resume_continues_and_completes(monkeypatch):
    """FULL session loop. The subagent called ask_user; the session PARKED on
    ``ask_user:*`` with ``frames=[AgentFrame]``. Deliver the operator reply: the
    ask_user resume hook produces ``{"response": ...}``, the subagent continues
    to final text, and the session's invoke_agent tool_call gets the result."""
    sid = "sess-sub-ask"
    invoke_tcid = "invoke-tc-3"
    ask_call_id = "ask-call-3"

    llm = _ScriptedLLM(scripts=[_final_text_script("your name is Alice")])
    registry = _ProviderRegistry(llm=llm, toolset=_PlainToolset())
    base = _FakeStorageProvider()
    agent_storage = _AgentStorageProvider(base=base, agent=_agent(tools=["t1__do_it"]), provider_row=_provider_row())
    pool, engine = _build_pool(base_storage=base, agent_storage=agent_storage, registry=registry, resolver=None)

    leaf = Yielded(
        tool_name="ask_user",
        event_key=f"ask_user:{sid}:{ask_call_id}",
        resume_metadata={"prompt": "what is your name?"},
    )
    frame = _agent_frame(
        sid=sid, invoke_tcid=invoke_tcid,
        sub_history=_sub_history("t1__do_it", ask_call_id), tools=["t1__do_it"],
    )
    sess = _make_session(
        sid, leaf=leaf, leaf_tcid=ask_call_id, invoke_tcid=invoke_tcid,
        frames=[frame], resume_event_payload={"response": "Alice"},
    )
    await base.get_storage(WorkspaceSession).create(sess)

    rehydrated = ParkedState.from_jsonable(sess.parked_state)
    assert rehydrated.yielded.tool_name == "ask_user"
    assert rehydrated.yielded.event_key == f"ask_user:{sid}:{ask_call_id}"
    assert len(rehydrated.frames) == 1

    executor = _wire_session_executor(pool, monkeypatch)
    await pool._run_engine_session(await _claim(engine, sid))

    part = _injected_tool_result(executor, invoke_tcid)
    assert part.error is False
    assert json.loads(part.output) == {"output": "your name is Alice"}


# ===========================================================================
# Scenario 3: two-deep (agent -> S1 -> S2; S2 calls ask_user)
# ===========================================================================


@pytest.mark.asyncio
async def test_two_deep_ask_user_unwinds_s2_then_s1(monkeypatch):
    """FULL session loop. agent -> invoke_agent S1 -> invoke_agent S2; S2 called
    ask_user. PARK: ``frames == [AgentFrame(S1), AgentFrame(S2)]`` (outer-first).
    Deliver the reply: the walk resolves S2's ask_user leaf, resumes S2 to text,
    threads that up to S1 (S1 resumes to its own text), then delivers S1's result
    keyed by the SESSION's invoke_agent call id. Both subagent turns re-run for
    real (two scripted LLM streams)."""
    sid = "sess-two-deep"
    s1_invoke_tcid = "invoke-S1"   # session -> S1
    s2_invoke_tcid = "invoke-S2"   # S1 -> S2
    ask_call_id = "ask-call-deep"  # S2's ask_user call

    # S2 resumes (ask answer) -> text; then S1 resumes (S2's result) -> text.
    llm = _ScriptedLLM(scripts=[
        _final_text_script("S2 says blue"),
        _final_text_script("S1 wraps: blue"),
    ])
    registry = _ProviderRegistry(llm=llm, toolset=_PlainToolset())
    base = _FakeStorageProvider()
    agent_storage = _AgentStorageProvider(base=base, agent=_agent(tools=["t1__do_it"]), provider_row=_provider_row())
    pool, engine = _build_pool(base_storage=base, agent_storage=agent_storage, registry=registry, resolver=None)

    # Root-first: S1 outer (invoked by session via s1_invoke_tcid; its own
    # pending child call is s2_invoke_tcid), S2 inner (invoked by S1 via
    # s2_invoke_tcid; its pending call is the ask_user call).
    f_s1 = _agent_frame(
        sid=sid, invoke_tcid=s1_invoke_tcid,
        sub_history=_sub_history("system__invoke_agent", s2_invoke_tcid),
        tools=["t1__do_it"], depth=0,
    )
    f_s2 = _agent_frame(
        sid=sid, invoke_tcid=s2_invoke_tcid,
        sub_history=_sub_history("t1__do_it", ask_call_id), tools=["t1__do_it"], depth=1,
    )
    leaf = Yielded(
        tool_name="ask_user",
        event_key=f"ask_user:{sid}:{ask_call_id}",
        resume_metadata={"prompt": "color?"},
    )
    sess = _make_session(
        sid, leaf=leaf, leaf_tcid=ask_call_id, invoke_tcid=s1_invoke_tcid,
        frames=[f_s1, f_s2], resume_event_payload={"response": "blue"},
    )
    await base.get_storage(WorkspaceSession).create(sess)

    # PARK shape: two frames, outer-first, with distinct invoke ids.
    rehydrated = ParkedState.from_jsonable(sess.parked_state)
    assert [f.tool_call_id for f in rehydrated.frames] == [s1_invoke_tcid, s2_invoke_tcid]
    assert rehydrated.yielded.tool_name == "ask_user"

    executor = _wire_session_executor(pool, monkeypatch)
    await pool._run_engine_session(await _claim(engine, sid))

    # RESUME: unwind S2 then S1; the delivery is keyed by the SESSION's
    # invoke-S1 call id and carries S1's wrapped final text.
    part = _injected_tool_result(executor, s1_invoke_tcid)
    assert part.error is False
    assert json.loads(part.output) == {"output": "S1 wraps: blue"}
    # Both subagent LLM turns ran (both scripts consumed).
    assert llm._scripts == []


# ===========================================================================
# Scenario 4: approval-on-a-yielding-tool inside a subagent (composition)
# ===========================================================================


@pytest.mark.asyncio
async def test_subagent_approval_on_yielding_tool_reparks_then_completes(monkeypatch):
    """FULL session loop, two-phase composition. agent -> invoke_agent S1 ->
    invoke_agent S2; S2 called a tool that is BOTH approval-gated AND yielding.
    The session PARKED on ``tool_approval:*`` with ``frames=[S1, S2]``.

    Phase 1 - APPROVE: apply_leaf re-dispatches the real gated tool with
    bypass_approval; the tool body ITSELF yields a timer leaf. The walk Reparks,
    and because the re-dispatched bare tool yield carries no nested frame of its
    own, the reconstructed stack is the frames *outer* of S2 - i.e. ``[S1]`` is
    preserved - re-parked on ``timer:*`` (S2's tcid is consumed as the leaf id).

    Phase 2 - fire the timer: S1 (the surviving outer frame) resolves the timer
    leaf via its sleep hook, its subagent turn resumes to final text, and the
    session's invoke-S1 call gets the result. This proves the composition
    (apply_leaf re-dispatch -> tool re-yield -> mid-unwind repark preserving the
    outer frames -> fire-the-event -> unwind to completion) end to end."""
    sid = "sess-gated-yield"
    s1_invoke_tcid = "invoke-S1-gy"     # session -> S1
    s2_invoke_tcid = "invoke-S2-gy"     # S1 -> S2
    gated_call_id = "gated-yield-call"  # S2's gated+yielding tool call

    # Exactly ONE subagent LLM turn runs (S1's phase-2 continuation); S2 never
    # re-runs its LLM (its gated tool re-dispatch yields before any turn).
    llm = _ScriptedLLM(scripts=[_final_text_script("S1 saw the timer fire")])
    registry = _ProviderRegistry(llm=llm, toolset=_GatedYieldingToolset())
    resolver = _PoliciesOnlyResolver(
        [ToolApprovalPolicy(id="p", toolset_id="t1", tool_name="do_wait", approval=RequiredApprovalConfig())]
    )
    base = _FakeStorageProvider()
    agent_storage = _AgentStorageProvider(base=base, agent=_agent(tools=["t1__do_wait"]), provider_row=_provider_row())
    pool, engine = _build_pool(base_storage=base, agent_storage=agent_storage, registry=registry, resolver=resolver)

    f_s1 = _agent_frame(
        sid=sid, invoke_tcid=s1_invoke_tcid,
        sub_history=_sub_history("system__invoke_agent", s2_invoke_tcid),
        tools=["t1__do_wait"], depth=0,
    )
    f_s2 = _agent_frame(
        sid=sid, invoke_tcid=s2_invoke_tcid,
        sub_history=_sub_history("t1__do_wait", gated_call_id), tools=["t1__do_wait"], depth=1,
    )
    sess = _make_session(
        sid,
        leaf=_approval_leaf(sid=sid, gated_call_id=gated_call_id, gated_name="t1__do_wait"),
        leaf_tcid=gated_call_id, invoke_tcid=s1_invoke_tcid, frames=[f_s1, f_s2],
        resume_event_payload={"decision": "approved"},
    )
    await base.get_storage(WorkspaceSession).create(sess)

    executor = _wire_session_executor(pool, monkeypatch)

    # PHASE 1: APPROVE -> re-dispatch of the gated tool yields a timer -> the
    # session RE-PARKS (no delivery yet).
    await pool._run_engine_session(await _claim(engine, sid))
    assert executor.injected == [], "phase-1 approve re-parks; no inject yet"

    row = await base.get_storage(WorkspaceSession).get(sid)
    assert row.parked_event_key == f"timer:{sid}:{gated_call_id}", "re-parked on the tool's own timer leaf"
    reparked = ParkedState.from_jsonable(row.parked_state)
    assert reparked.yielded.tool_name == "sleep"
    # The OUTER frame (S1) is preserved across the mid-unwind re-park; S2 (the
    # frame whose own tool re-yielded) is consumed.
    assert [f.tool_call_id for f in reparked.frames] == [s1_invoke_tcid]

    # PHASE 2: fire the timer. Stamp a real timer payload onto the re-parked
    # blob (the re-park left resume_event_payload=None) and re-claim.
    phase2_blob = dict(row.parked_state)
    phase2_blob["resume_event_payload"] = {"fired": True}
    row2 = row.model_copy(update={
        "parked_status": "resumable",
        "parked_state": phase2_blob,
        "parked_at": datetime.now(timezone.utc) - timedelta(seconds=1),
    })
    await base.get_storage(WorkspaceSession).update(row2)

    await pool._run_engine_session(await _claim(engine, sid))

    # The surviving S1 frame resolved the timer + resumed to text; the session's
    # invoke-S1 call gets it.
    part = _injected_tool_result(executor, s1_invoke_tcid)
    assert part.error is False
    assert json.loads(part.output) == {"output": "S1 saw the timer fire"}
    assert llm._scripts == [], "exactly one subagent turn ran (S1's continuation)"


# ===========================================================================
# Scenario 5: graph agent-node -> subagent -> ask_user
# ===========================================================================
#
# NOT duplicated here. tests/worker/test_graph_node_subagent_resume.py already
# drives the faithful graph-session re-descent: a graph checkpoint carrying a
# nested ``pending_agent_yields`` entry (frames + leaf) is resumed through the
# REAL ``resume_continuation`` walk (test_continuation_real_walk_resumes_
# subagent_then_delivers) into the node's agent_tool_result, plus the Repark
# branch re-parks the graph session on the deeper leaf. Re-implementing the full
# graph-session worker loop here would duplicate that coverage with heavier
# scaffolding for no added faithfulness, so it is intentionally omitted.
