"""An agent session whose model asks for several tool calls in ONE round, one of them approval-gated, answers every call of the round (ticket 01a15327).

The reviewer's reproduction of PR #800 (``test_m2_*``) drove the real ``run_agent_turn``, the park the session stamps and ``inject_resume_and_continue``: the
gate parked the session in the middle of the round's batch, and the park kept only the assistant message that holds every tool_use of the round. On resume
the result of a call that ran BEFORE the gate was lost, a call AFTER it never ran, and the next request carried tool_uses no result answers (asked
``[a, g, b]``, answered ``[g]``), which a strict provider (Anthropic, OpenAI) rejects.

The park now carries the round (``ParkedState.completed_results`` / ``remaining_calls``): on resume the decision is applied to the parked call as before,
the calls after it run in order with the resumed run's own manager (each may park again: the round is carried forward), and ONE tool message that answers
every call of the round once, in order, is injected before the continuation's model call.

Every case runs the real chain: the first turn through ``run_one_session_turn`` with the real ``WorkspaceAgentExecutor`` over a real local workspace and a
real ``ToolExecutionManager`` whose tools an approval policy gates; the park blob that turn returns, flipped resumable as the wake leaves it; the resume
through ``WorkerPool._resume_engine_session``; and the continuation turn, whose model request is what the assertions read.
"""

from __future__ import annotations

import json
import shutil
from collections.abc import AsyncIterator
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from primer.agent.tool_manager import ToolExecutionManager
from primer.agent.workspace_executor import WorkspaceAgentExecutor
from primer.int.claim import ReleaseOutcome
from primer.int.toolset import ToolsetProvider
from primer.model.agent import Agent, AgentModel
from primer.model.chat import Done, Message, StreamEvent, TextDelta, Tool, ToolCallEnd, ToolCallPart, ToolCallStart, ToolResultPart
from primer.model.scheduler import WorkerConfig
from primer.model.tool_approval import ToolApprovalPolicy
from primer.model.workspace_session import AgentSessionBinding, SessionStatus, WorkspaceSession
from primer.model.yield_ import Yielded, YieldToWorker, gate_id_of, with_wake_gate
from primer.session.dispatch import SessionDispatchDeps, run_one_session_turn
from primer.toolset.system import build_system_toolset
from primer.worker.continuation import InvocationServices
from primer.worker.frames import AgentFrame, AgentResumeContext
from primer.worker.pool import WorkerPool
from primer.worker.yield_runtime import ParkedState
from tests.agent.test_inner_dispatch import USER, _Recorder
from tests.agent.test_tool_manager_approval_gate import _PoliciesOnlyResolver
from tests.agent.test_workspace_executor import _build_session, _FakeLLM, _model
from tests.conftest import _FakeStorageProvider
from tests.graph.test_agent_node_approval_resume import _policy
from tests.session.test_dispatch import FakeWorkspaceIO, _make_lease
from tests.toolset.test_call_tool_door import _Registry

# Re-export so pytest resolves the in-memory storage and the real provider registry the system toolset needs (the ask_user cases).
from tests.toolset.test_system import pr, sp  # noqa: F401

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="the workspace state repo needs git")

GATED = "rec__user_x"
SECOND_GATE = "rec__user_y"
TOOLS = [GATED, "rec__user_a", "rec__user_b", SECOND_GATE]
_SCHEMA = {"type": "object", "properties": {}, "additionalProperties": True}


def _asks_many(calls: list[tuple[str, str, dict[str, Any]]]) -> list[StreamEvent]:
    """One model reply that asks for every call of ``calls`` in ONE round, in that order."""
    events: list[StreamEvent] = [TextDelta(text="calling the tools", index=0)]
    for i, (call_id, name, arguments) in enumerate(calls, start=1):
        events.append(ToolCallStart(id=call_id, name=name, index=i))
        events.append(ToolCallEnd(id=call_id, arguments=arguments, index=i))
    events.append(Done(stop_reason="tool_use", raw_reason="tool_use"))
    return events


def _says(text: str) -> list[StreamEvent]:
    return [TextDelta(text=text, index=0), Done(stop_reason="stop", raw_reason="stop")]


def _pairing(messages: list[Message]) -> tuple[list[str], list[str]]:
    """The ids of the tool_uses the messages hold, and the ids of the tool results that answer them, each in order."""
    asked = [p.id for m in messages if m.role == "assistant" for p in m.parts if isinstance(p, ToolCallPart)]
    answered = [p.id for m in messages if m.role == "tool" for p in m.parts if isinstance(p, ToolResultPart)]
    return asked, answered


def _results(messages: list[Message]) -> dict[str, ToolResultPart]:
    return {p.id: p for m in messages if m.role == "tool" for p in m.parts if isinstance(p, ToolResultPart)}


def _the_round(gate_at: int) -> list[tuple[str, str, dict[str, Any]]]:
    """``call-a`` and ``call-b`` (ungated) with the gated ``call-g`` at ``gate_at``."""
    order = [("call-a", "rec__user_a"), ("call-b", "rec__user_b")]
    order.insert(gate_at, ("call-g", GATED))
    return [(call_id, name, {"n": i}) for i, (call_id, name) in enumerate(order)]


class _Bus:
    async def publish(self, key: str, payload: dict) -> None:
        return None


class _Scheduler:
    async def enqueue(self, session_id: str) -> None:
        return None


class _Engine:
    async def upsert(self, *args: Any, **kwargs: Any) -> None:
        return None


async def _value(value: Any) -> Any:
    return value


class _Session:
    """One agent session over a real local workspace: the executors of its turns and of its resume, each built fresh as the worker builds them."""

    def __init__(self, tmp_path: Path, *, providers: dict[str, Any], tools: list[str], policies: list[ToolApprovalPolicy], monkeypatch: Any) -> None:
        self.tmp_path = tmp_path
        self.providers = providers
        self.tools = tools
        self.policies = policies
        self.monkeypatch = monkeypatch
        self.storage = _FakeStorageProvider()
        self.sessions = self.storage.get_storage(WorkspaceSession)
        self.records = FakeWorkspaceIO()           # the session's display log (the dispatch writer and the resume's record writer)

    async def start(self) -> None:
        self._backend, self.workspace, self.slot = await _build_session(self.tmp_path)
        self.id = self.slot.session_id
        await self.sessions.create(WorkspaceSession(
            id=self.id, workspace_id=self.workspace.id, binding=AgentSessionBinding(agent_id="researcher"), status=SessionStatus.RUNNING,
            created_at=datetime.now(timezone.utc), turn_status="running",
        ))

    def executor(self, llm: _FakeLLM) -> WorkspaceAgentExecutor:
        manager = ToolExecutionManager.for_workspace(
            toolset_providers=self.providers, session=self.slot, tools=self.tools, initiated_by=USER,
            approval_resolver=_PoliciesOnlyResolver(self.policies),
        )
        agent = Agent(id="researcher", description="r", model=AgentModel(profile_id="openai-1--m"), system_prompt=["Be terse."], tools=self.tools)
        return WorkspaceAgentExecutor(agent=agent, llm=llm, llm_model=_model(), tool_manager=manager, session=self.slot)

    async def first_turn(self, reply: list[StreamEvent]) -> ReleaseOutcome:
        llm = _FakeLLM(scripts=[reply])

        async def build(_session: WorkspaceSession) -> WorkspaceAgentExecutor:
            return self.executor(llm)

        deps = SessionDispatchDeps(
            storage_provider=self.storage, workspace_io=self.records, event_bus=_Bus(), build_executor=build, scheduler=_Scheduler(), claim_engine=_Engine(),
        )
        outcome = await run_one_session_turn(_make_lease(self.id), deps)
        assert outcome.park is not None, "the turn parked"
        return outcome

    async def resume(self, parked: ReleaseOutcome, payload: dict[str, Any], *, gate: bool = True, services: Any = None) -> ReleaseOutcome:
        """Flip the park resumable with ``payload`` (as the wake does: the reply, the key it fired, the gate it names) and run the pool's resume branch.

        ``services``: the bundle the continuation walk of a nested park drives the child run through (``None``: the pool's own)."""
        park = parked.park
        assert park is not None
        state = park.parked_state
        if gate:
            payload = with_wake_gate(payload, gate_id_of(state["yielded"]["resume_metadata"]))
        row = await self.sessions.get(self.id)
        await self.sessions.update(row.model_copy(update={
            "parked_status": "resumable", "parked_at": park.parked_at, "parked_until": park.parked_until, "parked_event_key": park.parked_event_key,
            "parked_state": {**state, "resume_event_payload": payload, "resume_event_key": park.parked_event_key},
        }))
        pool = WorkerPool(
            config=WorkerConfig(concurrency=1), scheduler=None, storage=self.storage, workspace_registry=None,  # type: ignore[arg-type]
            provider_registry=None, engine=None,  # type: ignore[arg-type]
        )
        self.monkeypatch.setattr(pool, "_load_workspace_for_persist", lambda _ws: _value(self.records))
        self.monkeypatch.setattr(pool, "_build_agent_executor", lambda _s, _w: _value(self.executor(_FakeLLM(scripts=[_says("the round was skipped")]))))
        if services is not None:
            self.monkeypatch.setattr(pool, "_build_invocation_services", lambda *_a: services)
        return await pool._resume_engine_session(None, await self.sessions.get(self.id))

    async def next_request(self) -> list[Message]:
        """What the continuation turn sends the model: the session's own history, read back from the workspace."""
        llm = _FakeLLM(scripts=[_says("done")])
        async for _event in self.executor(llm).invoke([]):
            pass
        assert len(llm.calls) == 1
        return llm.calls[0]["messages"]

    def logged(self, kind: str) -> list[dict]:
        return [json.loads(line) for line in self.records.read_lines(self.id) if json.loads(line)["kind"] == kind]

    def rows(self) -> dict[str, str]:
        """Tool name -> the scoped id of its TOOL_CALL record."""
        return {r["payload"]["name"]: r["payload"]["id"] for r in self.logged("tool_call")}


def _ran(rec: _Recorder) -> list[str]:
    return [call[3].tool_call_id for call in rec.calls]


async def _session(tmp_path: Path, monkeypatch: Any) -> tuple[_Session, _Recorder]:
    rec = _Recorder({"user_x": "user", "user_a": "user", "user_b": "user", "user_y": "user"})
    session = _Session(tmp_path, providers={"rec": rec}, tools=TOOLS, policies=[_policy("rec", "user_x"), _policy("rec", "user_y")], monkeypatch=monkeypatch)
    await session.start()
    return session, rec


# ---- one gated call, in every position ---------------------------------------------------------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("gate_at", [0, 1, 2], ids=["gate-first", "gate-middle", "gate-last"])
@pytest.mark.parametrize("decision", ["approved", "rejected"])
async def test_every_call_of_the_round_is_answered_once_in_order(tmp_path: Path, monkeypatch: Any, gate_at: int, decision: str) -> None:
    session, rec = await _session(tmp_path, monkeypatch)
    calls = _the_round(gate_at)
    ids = [c[0] for c in calls]
    parked = await session.first_turn(_asks_many(calls))
    assert parked.park is not None and parked.park.parked_state["tool_call_id"] == "call-g"
    assert _ran(rec) == ids[:gate_at], "the calls before the gate ran, the gate parked the round, the calls after it did not run yet"

    outcome = await session.resume(parked, {"decision": decision})

    assert (outcome.success, outcome.drop_lease, outcome.park) == (True, False, None), "the round completed: the session continues"
    request = await session.next_request()
    asked, answered = _pairing(request)
    assert asked == ids and answered == ids, f"every call of the round must be answered once, in order: asked {asked}, answered {answered}"
    assert [m.role for m in request][-2:] == ["assistant", "tool"], "one tool message answers the whole round, right after the assistant message"
    results = _results(request)
    for call_id in ("call-a", "call-b"):
        assert (results[call_id].error, json.loads(results[call_id].output)) == (False, {"ran": f"user_{call_id[-1]}"}), "an ungated call keeps its real result"
    if decision == "approved":
        assert (results["call-g"].error, json.loads(results["call-g"].output)) == (False, {"ran": "user_x"})
        assert sorted(_ran(rec)) == sorted(ids), "every call ran once"
    else:
        assert results["call-g"].error is True and json.loads(results["call-g"].output)["rejected"] is True
        assert sorted(_ran(rec)) == sorted(i for i in ids if i != "call-g"), "the rejected call did not run, the others ran once"


@pytest.mark.asyncio
async def test_every_call_of_the_round_is_recorded_once_under_its_call_row(tmp_path: Path, monkeypatch: Any) -> None:
    """The display log pairs too: the result of a call before the gate is recorded as the round parks (it was dropped), the gated call's answer and the
    result of the call after it are recorded by the resume, each once and under the scoped id of its own call row."""
    session, _rec = await _session(tmp_path, monkeypatch)
    parked = await session.first_turn(_asks_many(_the_round(1)))
    rows = session.rows()
    assert [r["payload"]["call_id"] for r in session.logged("tool_result")] == [rows["rec__user_a"]]
    assert parked.park is not None and parked.park.parked_state["remaining_scoped_call_ids"] == {"call-b": rows["rec__user_b"]}

    await session.resume(parked, {"decision": "approved"})

    results = session.logged("tool_result")
    assert [r["payload"]["call_id"] for r in results] == [rows["rec__user_a"], rows[GATED], rows["rec__user_b"]]
    assert all(r["payload"]["error"] is False for r in results)
    seqs = [json.loads(line)["seq"] for line in session.records.read_lines(session.id)]
    assert seqs == sorted(set(seqs)), "the resume's records continue the log's sequence"


# ---- a second gated call in the same round: two sequential parks --------------------------------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("second", ["approved", "rejected"])
async def test_a_second_gated_call_of_the_round_parks_the_session_again_carrying_what_is_done(tmp_path: Path, monkeypatch: Any, second: str) -> None:
    session, rec = await _session(tmp_path, monkeypatch)
    calls = [("call-g", GATED, {}), ("call-a", "rec__user_a", {}), ("call-h", SECOND_GATE, {}), ("call-b", "rec__user_b", {})]
    ids = [c[0] for c in calls]
    parked = await session.first_turn(_asks_many(calls))
    assert rec.calls == []

    reparked = await session.resume(parked, {"decision": "approved"})

    assert reparked.park is not None and reparked.drop_lease is True, "the second gated call parks the session again"
    state = reparked.park.parked_state
    assert (state["yielded"]["tool_name"], state["tool_call_id"]) == ("_approval", "call-h")
    assert state["yielded"]["resume_metadata"]["original_call"]["name"] == SECOND_GATE
    assert reparked.park.parked_event_key == f"tool_approval:{session.id}:call-h"
    assert gate_id_of(state["yielded"]["resume_metadata"]) != gate_id_of(parked.park.parked_state["yielded"]["resume_metadata"]), "a gate of its own"
    assert _ran(rec) == ["call-g", "call-a"], "the approved call and the call after it ran; the second gate parked before the last call"
    rows = session.rows()
    assert state["scoped_tool_call_id"] == rows[SECOND_GATE], "the new park answers the second gated call's own row"
    assert [p["id"] for p in state["completed_results"]] == ["call-g", "call-a"] and [c["id"] for c in state["remaining_calls"]] == ["call-b"]
    assert state["remaining_scoped_call_ids"] == {"call-b": rows["rec__user_b"]}
    assert state["llm_messages"] == parked.park.parked_state["llm_messages"], "the in-progress turn rides along unchanged"

    final = await session.resume(reparked, {"decision": second})

    assert (final.success, final.drop_lease, final.park) == (True, False, None)
    request = await session.next_request()
    assert _pairing(request) == (ids, ids)
    results = _results(request)
    assert json.loads(results["call-g"].output) == {"ran": "user_x"} and json.loads(results["call-a"].output) == {"ran": "user_a"}
    assert json.loads(results["call-b"].output) == {"ran": "user_b"}
    if second == "approved":
        assert json.loads(results["call-h"].output) == {"ran": "user_y"} and _ran(rec) == ["call-g", "call-a", "call-h", "call-b"]
    else:
        assert results["call-h"].error is True and json.loads(results["call-h"].output)["rejected"] is True
        assert _ran(rec) == ["call-g", "call-a", "call-b"]
    assert [r["payload"]["call_id"] for r in session.logged("tool_result")] == [rows[GATED], rows["rec__user_a"], rows[SECOND_GATE], rows["rec__user_b"]], (
        "each call's result is recorded once, under its row"
    )


# ---- a later call that pauses (ask_user), and an approved call that pauses ----------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_later_ask_user_parks_the_session_on_its_question_and_its_answer_completes_the_round(
    tmp_path: Path, monkeypatch: Any, sp: Any, pr: Any,  # noqa: F811 - the re-exported fixtures
) -> None:
    """The call after the approved one is the system toolset's own ``ask_user``: it asks, the session waits on its key, and the person's answer (through the
    real ``ask_user`` resume hook) is that call's result; the call after it runs before the model is called."""
    rec = _Recorder({"user_x": "user", "user_b": "user"})
    system = build_system_toolset(storage_provider=sp, provider_registry=_Registry(pr, {"rec": rec}))
    tools = [GATED, "system__ask_user", "rec__user_b"]
    session = _Session(tmp_path, providers={"rec": rec, "system": system}, tools=tools, policies=[_policy("rec", "user_x")], monkeypatch=monkeypatch)
    await session.start()
    calls = [("call-g", GATED, {}), ("call-q", "system__ask_user", {"prompt": "which colour?"}), ("call-b", "rec__user_b", {})]
    parked = await session.first_turn(_asks_many(calls))

    asking = await session.resume(parked, {"decision": "approved"})

    assert asking.park is not None, "the ask_user call asks: the session waits for the person"
    state = asking.park.parked_state
    assert (state["yielded"]["tool_name"], state["tool_call_id"]) == ("ask_user", "call-q")
    assert state["yielded"]["resume_metadata"]["prompt"] == "which colour?" and "parked_at_iso" in state["yielded"]["resume_metadata"]
    assert asking.park.parked_event_key == f"ask_user:{session.id}:call-q"
    assert _ran(rec) == ["call-g"]

    final = await session.resume(asking, {"response": "blue"}, gate=False)

    assert (final.success, final.drop_lease, final.park) == (True, False, None)
    request = await session.next_request()
    assert _pairing(request) == (["call-g", "call-q", "call-b"], ["call-g", "call-q", "call-b"])
    results = _results(request)
    assert json.loads(results["call-q"].output) == {"response": "blue"}
    assert _ran(rec) == ["call-g", "call-b"]


@pytest.mark.asyncio
async def test_an_approved_call_that_pauses_keeps_the_rest_of_its_round(
    tmp_path: Path, monkeypatch: Any, sp: Any, pr: Any,  # noqa: F811 - the re-exported fixtures
) -> None:
    """The two-phase park inside a round: the gated call is ``ask_user`` itself. The approval runs it, it asks, and the session parks again on its question
    (``repark_resumed_yield_outcome``) carrying the round unchanged (the result of the call before it, the call after it), so the answer completes the round."""
    rec = _Recorder({"user_a": "user", "user_b": "user"})
    system = build_system_toolset(storage_provider=sp, provider_registry=_Registry(pr, {"rec": rec}))
    tools = ["rec__user_a", "system__ask_user", "rec__user_b"]
    session = _Session(tmp_path, providers={"rec": rec, "system": system}, tools=tools, policies=[_policy("system", "ask_user")], monkeypatch=monkeypatch)
    await session.start()
    calls = [("call-a", "rec__user_a", {}), ("call-q", "system__ask_user", {"prompt": "which colour?"}), ("call-b", "rec__user_b", {})]
    parked = await session.first_turn(_asks_many(calls))
    assert parked.park is not None and parked.park.parked_state["yielded"]["tool_name"] == "_approval" and _ran(rec) == ["call-a"]

    asking = await session.resume(parked, {"decision": "approved"})

    assert asking.park is not None
    state = asking.park.parked_state
    assert (state["yielded"]["tool_name"], state["tool_call_id"]) == ("ask_user", "call-q")
    first = parked.park.parked_state
    assert (state["completed_results"], state["remaining_calls"], state["remaining_scoped_call_ids"]) == (
        first["completed_results"], first["remaining_calls"], first["remaining_scoped_call_ids"],
    ), "the same call of the round still waits: the round around it is unchanged"

    final = await session.resume(asking, {"response": "blue"}, gate=False)

    assert (final.success, final.drop_lease, final.park) == (True, False, None)
    request = await session.next_request()
    assert _pairing(request) == (["call-a", "call-q", "call-b"], ["call-a", "call-q", "call-b"])
    results = _results(request)
    assert json.loads(results["call-a"].output) == {"ran": "user_a"} and json.loads(results["call-q"].output) == {"response": "blue"}
    assert _ran(rec) == ["call-a", "call-b"]


class _Delegates(ToolsetProvider):
    """Toolset ``dlg``: ``delegate`` stands in for ``invoke_agent`` whose child run asks the user a question. It raises the yield ``invoke_agent`` raises
    then: the child's question as the leaf, under the child's own call id, and the child's frame on it keyed by THIS call's id."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def required_role(self, tool_name: str) -> str:
        return "user"

    async def list_tools(self, *, principal: str | None = None) -> AsyncIterator[Tool]:
        yield Tool(id="delegate", description="delegates to a child run", toolset_id="dlg", args_schema=_SCHEMA)

    async def call(self, *, tool_name, arguments, principal=None, ctx=None):
        self.calls.append(ctx.tool_call_id)
        paused = YieldToWorker(
            Yielded(tool_name="ask_user", event_key=f"ask_user:{ctx.session_id}:child-call", timeout=600.0, resume_metadata={"prompt": "Which one?"}),
            tool_call_id="child-call",
        )
        paused.frames = [AgentFrame(
            agent_id="child", llm_messages=[], tool_call_id=ctx.tool_call_id, depth=1,
            context=AgentResumeContext(ctx.session_id, ctx.workspace_id, None, None, [], initiated_by=ctx.initiated_by),
        )]
        raise paused


@pytest.mark.asyncio
async def test_a_later_call_that_pauses_inside_a_child_run_parks_on_the_childs_question_and_its_answer_completes_the_round(
    tmp_path: Path, monkeypatch: Any,
) -> None:
    """A later call of the round delegates and the child run pauses: the session parks on the child's question with the child's frame, keyed by the
    session's own call, and the scoped id of that call's row; the continuation walk unwinds the child's answer into that call's result, and the call after
    it runs before the model is called."""
    rec = _Recorder({"user_x": "user", "user_b": "user"})
    delegating = _Delegates()
    tools = [GATED, "dlg__delegate", "rec__user_b"]
    session = _Session(tmp_path, providers={"rec": rec, "dlg": delegating}, tools=tools, policies=[_policy("rec", "user_x")], monkeypatch=monkeypatch)
    await session.start()
    parked = await session.first_turn(_asks_many([("call-g", GATED, {}), ("call-d", "dlg__delegate", {}), ("call-b", "rec__user_b", {})]))

    nested = await session.resume(parked, {"decision": "approved"})

    assert nested.park is not None and delegating.calls == ["call-d"] and _ran(rec) == ["call-g"]
    state = nested.park.parked_state
    assert (state["yielded"]["tool_name"], state["tool_call_id"]) == ("ask_user", "child-call"), "the session waits on the child's question"
    assert [(f["kind"], f["tool_call_id"]) for f in state["frames"]] == [("agent", "call-d")], "the child's frame is kept, keyed by the session's call"
    assert state["scoped_tool_call_id"] == session.rows()["dlg__delegate"], "the answer will pair with the row of the session's own call"
    assert [p["id"] for p in state["completed_results"]] == ["call-g"] and [c["id"] for c in state["remaining_calls"]] == ["call-b"]

    async def resume_subagent(*, agent_id, context, llm_messages, child_result, depth, invoke_tool_call_id):
        return f"the child heard {json.loads(child_result.output)['response']}"

    async def _unused(*_a: Any, **_k: Any) -> Any:
        raise AssertionError("not reached by an agent frame")

    services = InvocationServices(
        build_subagent_toolmanager=_unused, resume_subagent=resume_subagent, resolve_graph=_unused, build_child_graph_executor=_unused,
        graph_agent_tool_result=_unused, session_id=session.id,
    )
    final = await session.resume(nested, {"response": "the blue one"}, gate=False, services=services)

    assert (final.success, final.drop_lease, final.park) == (True, False, None)
    request = await session.next_request()
    assert _pairing(request) == (["call-g", "call-d", "call-b"], ["call-g", "call-d", "call-b"])
    assert json.loads(_results(request)["call-d"].output) == {"output": "the child heard the blue one"}
    assert _ran(rec) == ["call-g", "call-b"] and delegating.calls == ["call-d"]


# ---- a park written before the round was carried -----------------------------------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_park_without_the_round_resumes_as_it_did(tmp_path: Path, monkeypatch: Any) -> None:
    """A park written by an older build has no round in its blob: it resumes as before (only the parked call is answered) and nothing crashes."""
    session, rec = await _session(tmp_path, monkeypatch)
    parked = await session.first_turn(_asks_many(_the_round(1)))
    assert parked.park is not None
    for key in ("completed_results", "remaining_calls", "remaining_scoped_call_ids"):
        parked.park.parked_state.pop(key)

    outcome = await session.resume(parked, {"decision": "approved"})

    assert (outcome.success, outcome.drop_lease, outcome.park) == (True, False, None)
    request = await session.next_request()
    assert _pairing(request) == (["call-a", "call-g", "call-b"], ["call-g"]), "an old park keeps today's behaviour"
    assert _ran(rec) == ["call-a", "call-g"], "the call after the gate is not run for a park that does not name it"


def test_the_park_record_round_trips_the_round_and_a_record_from_before_has_none() -> None:
    round_fields = {
        "completed_results": [ToolResultPart(id="call-a", output="a").model_dump(mode="json")],
        "remaining_calls": [ToolCallPart(id="call-b", name="rec__user_b", arguments={"n": 1}).model_dump(mode="json")],
        "remaining_scoped_call_ids": {"call-b": "None:tool:3:2"},
    }
    parked = ParkedState(
        yielded=Yielded(tool_name="_approval", event_key="tool_approval:s:call-g"), llm_messages=[], turn_no=3, started_at=datetime.now(timezone.utc),
        tool_call_id="call-g", **round_fields,
    )

    blob = json.loads(json.dumps(parked.to_jsonable()))
    back = ParkedState.from_jsonable(blob)

    assert {key: blob[key] for key in round_fields} == round_fields
    assert (back.completed_results, back.remaining_calls, back.remaining_scoped_call_ids) == tuple(round_fields.values())
    for key in round_fields:
        blob.pop(key)
    old = ParkedState.from_jsonable(blob)
    assert (old.completed_results, old.remaining_calls, old.remaining_scoped_call_ids) == (None, None, None)
