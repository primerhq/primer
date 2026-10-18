"""A graph AGENT node whose gated tool call is decided gets the decision applied (the review of #787, open item 3).

A node's agent calls a tool that an approval policy gates. The gate parks the node (a ``pending_agent_yields`` entry whose ``tool_name`` is ``_approval``) and the worker resumes it with the decision.
The resume loop used to answer EVERY such entry with ``ToolResultPart(output="")``, a successful empty result: an approved tool never ran, and the model read the same empty success whether the
person approved or rejected. The same held for the inner gate of ``system__call_tool``, and for the agent node of a child graph (``invoke_graph``), which resumes through the same loop.

The node's own manager now answers the entry with the helper the agent session uses (``yield_runtime._resume_tool_approval``, which runs an approved ``call_tool`` park through
``dispatch_inner_tool``):

* APPROVED: the gated call runs with the resumed node's manager and context (the session, the run's identity, the executor's principal string), the inner tool's role is checked again, a principal
  stored with a park is never read, and the real result is what the model reads. A tool that pauses again parks the node again on its own event.
* REJECTED, timed out, cancelled or unreadable: nothing runs, and the model reads the rejection the agent session gives it (an error result that says so).
* One approval runs at most one gated call, counted over agent-node gates and ToolCall inner-call gates together: it cannot be shown to be about several siblings that share a key, or that a key-less drain selects.

These cases drive the real ``WorkspaceGraphExecutor`` with a real ``ToolExecutionManager``, policy-gated tools and the real worker adapter (``resume_graph_from_checkpoint``), after a live first run
whose records go through the real ``translate_stream_event``. The last case drives the real ``resume_graph_engine``.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from primer.agent.tool_manager import ToolExecutionManager
from primer.graph.invoke_graph import ChildGraphFailed, resume_invoke_graph
from primer.graph.workspace_executor import WorkspaceGraphExecutor
from primer.int.toolset import ToolsetProvider
from primer.model.agent import Agent, AgentModel
from primer.model.chat import Done, Message, StreamEvent, TextDelta, Tool, ToolCallEnd, ToolCallPart, ToolCallResult, ToolCallStart, ToolResultPart
from primer.model.graph import Graph, _AgentNodeRef, _BeginNode, _EndNode, _StaticEdge, _ToolCallNode
from primer.model.model_profile import ModelProfileConfig
from primer.model.principal import PrincipalRef
from primer.model.tool_approval import RequiredApprovalConfig, ToolApprovalPolicy
from primer.model.yield_ import YieldCancelled, YieldTimeout, YieldToWorker, gate_id_of, with_wake_gate
from primer.model_profile import ResolvedModel
from primer.session.persistence import _CoalesceState, stash_graph_scoped_ids, translate_stream_event
from primer.toolset.system import build_system_toolset
from primer.worker import graph_resume_coordinator
from primer.worker.graph_resume import resume_graph_from_checkpoint
from primer.worker.yield_runtime import ParkedState, _resume_tool_approval
from tests._resume_hook_fakes import (
    DrainTapPool,
    EngineFakePool,
    EngineStorageProvider,
    FakeSessionRow,
    FakeSessionStorage,
    FakeStorage,
    NullWorkspaceIO,
    RecordingWorkspaceIO,
    waiting_graph_session,
)
from tests.agent.test_inner_dispatch import ADMIN, USER, _Failing, _Recorder, _Yielding
from tests.agent.test_tool_manager_approval_gate import _PoliciesOnlyResolver
from tests.graph.test_workspace_executor import _make_state_repo
from tests.toolset.test_call_tool_door import _Registry

# Re-export so pytest resolves the in-memory storage and the real provider registry the neighbouring call_tool tests use.
from tests.toolset.test_system import pr, sp  # noqa: F401

GATED = "rec__user_x"
ARGS = {"a": 1}


def _policy(toolset_id: str, tool_name: str) -> ToolApprovalPolicy:
    return ToolApprovalPolicy(id=f"p-{toolset_id}-{tool_name}", toolset_id=toolset_id, tool_name=tool_name, enabled=True, approval=RequiredApprovalConfig())


class _Holder:
    """The workspace session a graph run holds (the worker passes the graph session's own): what ``WorkspaceGraphExecutor`` and ``for_workspace`` read of it."""

    session_id = "s-1"
    workspace_id = "ws-1"
    agent_id = "agent-1"
    system_prompt_fragment = ""
    workspace_tools: list = []


class _Model:
    """The model of a node: one scripted reply per request. Every request it is sent is kept, so a test reads what the model was told."""

    def __init__(self, *replies: list[StreamEvent]) -> None:
        self._replies = list(replies)
        self.requests: list[list[Message]] = []

    async def list_models(self) -> list[str]:
        return ["m"]

    def stream(self, **kw: Any) -> AsyncIterator[StreamEvent]:
        self.requests.append(list(kw.get("messages") or []))
        reply = self._replies.pop(0)

        async def events() -> AsyncIterator[StreamEvent]:
            for event in reply:
                yield event

        return events()


def _asks(name: str, arguments: dict[str, Any], call_id: str = "call-1") -> list[StreamEvent]:
    return [
        TextDelta(text="calling the tool", index=0),
        ToolCallStart(id=call_id, name=name, index=1),
        ToolCallEnd(id=call_id, arguments=arguments, index=1),
        Done(stop_reason="tool_use", raw_reason="tool_use"),
    ]


def _says(text: str) -> list[StreamEvent]:
    return [TextDelta(text=text, index=0), Done(stop_reason="stop", raw_reason="stop")]


def _read(model: _Model) -> list[ToolResultPart]:
    """The tool results the model was shown, in the order of its requests."""
    return [part for request in model.requests for message in request if message.role == "tool" for part in message.parts if isinstance(part, ToolResultPart)]


def _asked(model: _Model) -> list[str]:
    """The ids of the tool calls the model sees itself having made (a result must pair with one of them)."""
    return [part.id for request in model.requests for message in request if message.role == "assistant" for part in message.parts if isinstance(part, ToolCallPart)]


def _graph() -> Graph:
    return Graph(
        id="g-agent", description="begin -> A -> exit",
        nodes=[_BeginNode(id="begin"), _AgentNodeRef(id="A", agent_id="x", input_template="go"), _EndNode(id="exit", output_template="{{ nodes.A.text }}")],
        edges=[_StaticEdge(from_node="begin", to_node="A"), _StaticEdge(from_node="A", to_node="exit")],
    )


@dataclass
class _Resumed:
    executor: WorkspaceGraphExecutor
    model: _Model
    repark: Any
    records: list[dict[str, Any]]


class _Run:
    """One graph run parked at its agent node: builds the executors that park it and resume it (a fresh one per step, as the worker builds one per turn).

    The node's manager is built the way ``executor_builders`` builds it: for the workspace, with the node agent's tool surface, the approval resolver, the provider registry and the run's identity.
    """

    def __init__(
        self, tmp_path: Path, *, providers: dict[str, Any], tools: list[str], policies: list[ToolApprovalPolicy] | None = None, registry: Any = None,
        initiated_by: PrincipalRef = USER, principal: str | None = None, graph: Graph | None = None,
    ) -> None:
        self.tmp_path = tmp_path
        self.providers = providers
        self.tools = tools
        self.policies = policies or []
        self.registry = registry
        self.initiated_by = initiated_by
        self.principal = principal
        self.graph = graph or _graph()

    def as_run_of(self, initiated_by: PrincipalRef) -> _Run:
        """The same run as seen by an executor whose identity is ``initiated_by`` (a stale or forged park is resumed by a run that is not the one that parked)."""
        return _Run(
            self.tmp_path, providers=self.providers, tools=self.tools, policies=self.policies, registry=self.registry, initiated_by=initiated_by,
            principal=self.principal, graph=self.graph,
        )

    async def executor(self, model: _Model) -> WorkspaceGraphExecutor:
        async def agent_resolver(_agent_id: str) -> Agent:
            return Agent(id="x", description="x", model=AgentModel(profile_id="p--m"), system_prompt=["Be terse."], tools=self.tools)

        async def llm_resolver(_agent: Any, *_rest: Any) -> Any:
            return model, ResolvedModel(profile_id="p", provider_id="prov", model_name="m", context_length=128_000, config=ModelProfileConfig())

        async def tool_manager_resolver(agent: Agent) -> ToolExecutionManager:
            return ToolExecutionManager.for_workspace(
                toolset_providers=self.providers, session=_Holder(), tools=agent.tools, initiated_by=self.initiated_by,
                approval_resolver=_PoliciesOnlyResolver(self.policies), provider_registry=self.registry,
            )

        async def toolset_resolver(toolset_id: str) -> Any:         # the ToolCall nodes of a graph that has them (the worker wires the registry's get_toolset)
            return self.providers[toolset_id]

        return WorkspaceGraphExecutor(
            graph=self.graph, agent_resolver=agent_resolver, llm_resolver=llm_resolver,  # type: ignore[arg-type]
            tool_manager_resolver=tool_manager_resolver, state_repo=await _make_state_repo(self.tmp_path), graph_session_id="gs-1",
            workspace_session=_Holder(), identity=self.initiated_by, principal=self.principal,  # type: ignore[arg-type]
            toolset_resolver=toolset_resolver, approval_resolver=_PoliciesOnlyResolver(self.policies),
        )

    async def park(self, model: _Model) -> tuple[dict, list[dict], Any, YieldToWorker]:
        """Run to the node's park: the checkpoint, the live records (through ``translate_stream_event``), the stash ``dispatch.py`` keeps, and the ``YieldToWorker`` the worker parks on."""
        executor = await self.executor(model)
        state = _CoalesceState()
        executor.bind_coalesce_state(state)
        live: list[dict] = []
        with pytest.raises(YieldToWorker) as parked:
            async for event in executor.invoke([]):
                record = translate_stream_event(event, state, turn_no=1)
                live.extend(r.model_dump(mode="json") for r in ([] if record is None else record if isinstance(record, list) else [record]))
        checkpoint = parked.value.graph_checkpoint
        assert checkpoint is not None
        return checkpoint, live, stash_graph_scoped_ids(checkpoint, state), parked.value

    async def resume(
        self, model: _Model, checkpoint: dict, entry: dict, payload: Any, *, seq: Any = None, with_tap: bool = False, agent_tool_result: Message | None = None,
    ) -> _Resumed:
        """Resume the park ``entry`` names through the worker adapter, with a fresh executor: the re-park (``None`` when the graph drained) and, with ``with_tap``, the records the drain wrote."""
        executor = await self.executor(model)
        io = RecordingWorkspaceIO()
        row = FakeSessionRow(sid="gs-1", workspace_id="ws-1", turn_no=1, last_seq=10)
        pool = DrainTapPool(workspace_io=io, storage=FakeStorage(FakeSessionStorage(row))) if with_tap else None
        _decision, repark, _seq = await resume_graph_from_checkpoint(
            executor=executor, checkpoint=checkpoint, payload=payload, resumed_tcid=entry["tool_call_id"], resumed_event_key=entry["event_key"],
            agent_tool_result=agent_tool_result, pool=pool, session=row if with_tap else None, node_tool_call_seq=seq,  # type: ignore[arg-type]
        )
        records = [json.loads(one) for _sid, line in io.lines for one in line.decode().splitlines() if one.strip()]
        return _Resumed(executor=executor, model=model, repark=repark, records=records)


def _only_entry(checkpoint: dict) -> dict:
    (entry,) = checkpoint["pending_agent_yields"]
    return entry


def _the_call(rec: _Recorder) -> tuple[str, dict[str, Any], str | None, Any]:
    """The one call the recording toolset got: ``(tool_name, arguments, principal, ctx)``."""
    assert len(rec.calls) == 1, f"the approved tool ran {len(rec.calls)} times, not once: the approval was not applied"
    return rec.calls[0]


# ---- an approved tool runs -------------------------------------------------------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_an_approved_gated_tool_runs_and_the_model_reads_its_result(tmp_path: Path) -> None:
    rec = _Recorder({"user_x": "user"})
    run = _Run(tmp_path, providers={"rec": rec}, tools=[GATED], policies=[_policy("rec", "user_x")])
    checkpoint, _live, _seq, _yld = await run.park(_Model(_asks(GATED, ARGS)))
    entry = _only_entry(checkpoint)
    assert (entry["tool_name"], entry["tool_call_id"]) == ("_approval", "call-1") and rec.calls == [], "the gate parked the call before the tool ran"

    model = _Model(_says("done"))
    resumed = await run.resume(model, checkpoint, entry, {"decision": "approved"})

    assert resumed.repark is None
    name, arguments, principal, ctx = _the_call(rec)
    assert (name, arguments, principal) == ("user_x", ARGS, None), "the approved tool ran, once, with the arguments the model chose"
    assert (ctx.tool_call_id, ctx.session_id, ctx.workspace_id, ctx.initiated_by) == ("call-1", "s-1", "ws-1", USER), "with the context of the resumed node's manager"
    (result,) = _read(model)
    assert (result.id, result.error) == ("call-1", False)
    assert json.loads(result.output) == {"ran": "user_x"}, f"the model must read the tool's real answer, not {result.output!r}"
    assert _asked(model) == ["call-1"], "the result pairs with the call the model made"
    state = await resumed.executor.load_state()
    assert state is not None and (state["status"], state["ended_reason"]) == ("ended", "completed")


@pytest.mark.asyncio
async def test_the_approved_tool_gets_the_principal_string_the_node_dispatches_with(tmp_path: Path) -> None:
    """The first dispatch of a node passes the executor's principal string to the tool (``run_agent_turn(principal=...)``); the approved re-run does the same, and never a value stored with the park."""
    rec = _Recorder({"user_x": "user"})
    run = _Run(tmp_path, providers={"rec": rec}, tools=[GATED], policies=[_policy("rec", "user_x")], principal="alice")
    checkpoint, _live, _seq, _yld = await run.park(_Model(_asks(GATED, ARGS)))
    entry = _only_entry(checkpoint)

    await run.resume(_Model(_says("done")), checkpoint, entry, {"decision": "approved"})

    _name, _arguments, principal, ctx = _the_call(rec)
    assert principal == "alice" and ctx.principal == "alice"


@pytest.mark.asyncio
async def test_an_approved_tool_that_fails_is_an_error_result_the_node_continues_from(tmp_path: Path) -> None:
    """As the agent session does it: a failure of the approved call (here a tool the node's manager cannot route any more) is an error result, not a crash of the graph."""
    rec = _Recorder({"user_x": "user"})
    run = _Run(tmp_path, providers={"rec": rec}, tools=[GATED], policies=[_policy("rec", "user_x")])
    checkpoint, _live, _seq, _yld = await run.park(_Model(_asks(GATED, ARGS)))
    entry = _only_entry(checkpoint)
    entry["resume_metadata"]["original_call"]["name"] = "rec__gone"      # the agent was edited while the approval was pending

    model = _Model(_says("sorry"))
    resumed = await run.resume(model, checkpoint, entry, {"decision": "approved"})

    assert resumed.repark is None and rec.calls == []
    (result,) = _read(model)
    assert result.error is True and result.id == "call-1"
    body = json.loads(result.output)
    assert body["is_error"] is True and "rec__gone" in body["reason"] and "UnsupportedContentError" in body["reason"], body
    state = await resumed.executor.load_state()
    assert state is not None and (state["status"], state["ended_reason"]) == ("ended", "completed")


# ---- a rejection is a rejection --------------------------------------------------------------------------------------------------------------------------------


_REJECTIONS = [
    pytest.param({"decision": "rejected", "reason": "no thanks"}, "no thanks", id="rejected"),
    pytest.param({"decision": "rejected"}, "(no reason supplied)", id="rejected-without-a-reason"),
    pytest.param(YieldTimeout(elapsed_seconds=60.0), "timed-out", id="timed-out"),
    pytest.param(YieldCancelled(reason="operator left", cancelled_at=datetime.now(UTC), elapsed_seconds=3.0), "operator left", id="cancelled"),
    pytest.param({}, "malformed approval payload (missing decision)", id="no-decision"),
    pytest.param(None, "malformed approval payload (non-dict)", id="not-a-dict"),
]


@pytest.mark.asyncio
@pytest.mark.parametrize(("payload", "reason"), _REJECTIONS)
async def test_a_rejected_gated_tool_does_not_run_and_the_model_reads_the_rejection(tmp_path: Path, payload: Any, reason: str) -> None:
    rec = _Recorder({"user_x": "user"})
    run = _Run(tmp_path, providers={"rec": rec}, tools=[GATED], policies=[_policy("rec", "user_x")])
    checkpoint, _live, _seq, _yld = await run.park(_Model(_asks(GATED, ARGS)))
    entry = _only_entry(checkpoint)

    model = _Model(_says("understood"))
    resumed = await run.resume(model, checkpoint, entry, payload)

    assert resumed.repark is None and rec.calls == [], "nothing runs on a decision that is not an approval"
    (result,) = _read(model)
    assert (result.id, result.error) == ("call-1", True), "the model must not read an empty SUCCESS for a rejection"
    assert json.loads(result.output) == {"rejected": True, "reason": reason, "tool_name": GATED, "arguments": ARGS}


@pytest.mark.asyncio
async def test_the_rejection_is_the_result_the_agent_session_gives(tmp_path: Path) -> None:
    """One text for one refusal, wherever the gate sits: the graph node's answer equals what ``_resume_tool_approval`` returns for the same park and payload."""
    rec = _Recorder({"user_x": "user"})
    run = _Run(tmp_path, providers={"rec": rec}, tools=[GATED], policies=[_policy("rec", "user_x")])
    checkpoint, _live, _seq, _yld = await run.park(_Model(_asks(GATED, ARGS)))
    entry = _only_entry(checkpoint)
    payload = {"decision": "rejected", "reason": "no thanks"}

    model = _Model(_says("understood"))
    await run.resume(model, checkpoint, entry, payload)

    from_the_session = await _resume_tool_approval(blob={"yielded": {"resume_metadata": entry["resume_metadata"]}}, payload=payload, tool_manager=None)
    (result,) = _read(model)
    assert (result.id, result.output, result.error) == (from_the_session.id, from_the_session.output, from_the_session.error)


@pytest.mark.asyncio
async def test_an_answer_that_was_delivered_is_used_as_it_is(tmp_path: Path) -> None:
    """A caller that delivers the node's answer itself (``agent_tool_result``) is not second-guessed: nothing runs, and the model reads what it was given."""
    rec = _Recorder({"user_x": "user"})
    run = _Run(tmp_path, providers={"rec": rec}, tools=[GATED], policies=[_policy("rec", "user_x")])
    checkpoint, _live, _seq, _yld = await run.park(_Model(_asks(GATED, ARGS)))
    entry = _only_entry(checkpoint)

    model = _Model(_says("done"))
    given = Message(role="tool", parts=[ToolResultPart(id="call-1", output="delivered")])
    await run.resume(model, checkpoint, entry, {"decision": "approved"}, agent_tool_result=given)

    assert rec.calls == []
    assert [(p.id, p.output, p.error) for p in _read(model)] == [("call-1", "delivered", False)]


# ---- the call_tool inner gate -----------------------------------------------------------------------------------------------------------------------------------


@dataclass
class _World:
    rec: _Recorder
    pausing: _Yielding
    registry: Any
    system: Any
    sp: Any

    def run(self, tmp_path: Path, **kw: Any) -> _Run:
        return _Run(tmp_path, providers={"system": self.system}, tools=["system__call_tool"], registry=self.registry, **kw)


@pytest.fixture
def world(sp, pr) -> _World:  # noqa: F811 - the re-exported fixtures
    """The real system toolset, whose ``call_tool`` resolves its inner toolsets through the registry the node's manager holds too: ``rec`` records, ``y`` pauses, ``f`` fails."""
    rec = _Recorder({"user_x": "user", "admin_x": "admin"})
    pausing = _Yielding()
    registry = _Registry(pr, {"rec": rec, "y": pausing, "f": _Failing()})
    system = build_system_toolset(storage_provider=sp, provider_registry=registry)  # type: ignore[arg-type]
    pr._system_toolset_provider = system  # type: ignore[attr-defined]
    return _World(rec=rec, pausing=pausing, registry=registry, system=system, sp=sp)


def _call_tool(toolset_id: str, tool_name: str, arguments: dict[str, Any] | None = None) -> list[StreamEvent]:
    return _asks("system__call_tool", {"toolset_id": toolset_id, "tool_name": tool_name, "arguments": arguments or {}})


@pytest.mark.asyncio
async def test_an_approved_call_tool_inner_gate_runs_the_inner_tool(world: _World, tmp_path: Path) -> None:
    await world.sp.get_storage(ToolApprovalPolicy).create(_policy("rec", "user_x"))
    run = world.run(tmp_path)
    checkpoint, _live, _seq, _yld = await run.park(_Model(_call_tool("rec", "user_x", ARGS)))
    entry = _only_entry(checkpoint)
    assert entry["tool_name"] == "_approval" and entry["resume_metadata"]["via_call_tool"] == {"toolset_id": "rec", "tool_name": "user_x"}
    assert world.rec.calls == [], "the gate parked the inner tool before it ran"

    model = _Model(_says("done"))
    resumed = await run.resume(model, checkpoint, entry, {"decision": "approved"})

    assert resumed.repark is None
    name, arguments, principal, ctx = _the_call(world.rec)
    assert (name, arguments, principal) == ("user_x", ARGS, None)
    assert (ctx.tool_call_id, ctx.session_id, ctx.workspace_id, ctx.initiated_by) == ("call-1", "s-1", "ws-1", USER), "the inner tool runs as the resumed node"
    (result,) = _read(model)
    assert (result.id, result.error) == ("call-1", False), "the answer is the result of the call_tool call"
    assert json.loads(result.output) == {"ran": "user_x"}
    assert _asked(model) == ["call-1"]


@pytest.mark.asyncio
async def test_a_rejected_call_tool_inner_gate_does_not_run_the_inner_tool(world: _World, tmp_path: Path) -> None:
    await world.sp.get_storage(ToolApprovalPolicy).create(_policy("rec", "user_x"))
    run = world.run(tmp_path)
    checkpoint, _live, _seq, _yld = await run.park(_Model(_call_tool("rec", "user_x", ARGS)))
    entry = _only_entry(checkpoint)

    model = _Model(_says("understood"))
    await run.resume(model, checkpoint, entry, {"decision": "rejected", "reason": "no thanks"})

    assert world.rec.calls == []
    (result,) = _read(model)
    assert (result.id, result.error) == ("call-1", True)
    body = json.loads(result.output)
    assert body["rejected"] is True and body["reason"] == "no thanks" and body["tool_name"] == "user_x"


@pytest.mark.asyncio
async def test_an_approved_call_tool_inner_tool_is_not_run_by_a_node_that_may_not_run_it(world: _World, tmp_path: Path) -> None:
    """The role is checked again against the node that resumes: a park made by an admin-initiated run, resumed by a node whose identity is a user's (a stale or forged park), is refused."""
    await world.sp.get_storage(ToolApprovalPolicy).create(_policy("rec", "admin_x"))
    run = world.run(tmp_path, initiated_by=ADMIN)
    checkpoint, _live, _seq, _yld = await run.park(_Model(_call_tool("rec", "admin_x")))
    entry = _only_entry(checkpoint)

    model = _Model(_says("understood"))
    await run.as_run_of(USER).resume(model, checkpoint, entry, {"decision": "approved"})

    assert world.rec.calls == [], "the approval ran an admin tool for a node that is not an admin's"
    (result,) = _read(model)
    assert (result.id, result.error) == ("call-1", True)
    assert json.loads(result.output)["type"] == "forbidden"


@pytest.mark.asyncio
async def test_the_same_park_is_run_by_a_node_that_may_run_it(world: _World, tmp_path: Path) -> None:
    await world.sp.get_storage(ToolApprovalPolicy).create(_policy("rec", "admin_x"))
    run = world.run(tmp_path, initiated_by=ADMIN)
    checkpoint, _live, _seq, _yld = await run.park(_Model(_call_tool("rec", "admin_x")))
    entry = _only_entry(checkpoint)

    model = _Model(_says("done"))
    await run.resume(model, checkpoint, entry, {"decision": "approved"})

    assert [c[0] for c in world.rec.calls] == ["admin_x"]
    (result,) = _read(model)
    assert result.error is False


@pytest.mark.asyncio
async def test_an_approved_call_tool_inner_tool_gets_the_principal_string_the_node_dispatches_with(world: _World, tmp_path: Path) -> None:
    """The same for the inner tool of a ``call_tool`` gate: the context the node's manager builds carries the executor's principal string, and the inner provider is handed that and nothing else."""
    await world.sp.get_storage(ToolApprovalPolicy).create(_policy("rec", "user_x"))
    run = world.run(tmp_path, principal="alice")
    checkpoint, _live, _seq, _yld = await run.park(_Model(_call_tool("rec", "user_x", ARGS)))

    await run.resume(_Model(_says("done")), checkpoint, _only_entry(checkpoint), {"decision": "approved"})

    _name, _arguments, principal, ctx = _the_call(world.rec)
    assert principal == "alice" and ctx.principal == "alice"


@pytest.mark.asyncio
async def test_a_principal_stored_in_a_legacy_call_tool_park_is_never_read(world: _World, tmp_path: Path) -> None:
    """A park from before the pivot carries ``via_call_tool.principal``, a value a model chose that an MCP toolset keys its OAuth tokens on."""
    await world.sp.get_storage(ToolApprovalPolicy).create(_policy("rec", "user_x"))
    run = world.run(tmp_path)
    checkpoint, _live, _seq, _yld = await run.park(_Model(_call_tool("rec", "user_x")))
    entry = _only_entry(checkpoint)
    entry["resume_metadata"]["via_call_tool"]["principal"] = "mallory"

    await run.resume(_Model(_says("done")), checkpoint, entry, {"decision": "approved"})

    _name, _arguments, principal, ctx = _the_call(world.rec)
    assert principal is None and ctx.principal is None


# ---- a tool that pauses again, a tool that fails ----------------------------------------------------------------------------------------------------------------


class _PausingTool(_Yielding):
    """A role-user toolset whose ``wait`` tool pauses the turn for its own event, as ``ask_user`` does (and records that it was called)."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    async def call(self, *, tool_name, arguments, principal=None, ctx=None):
        self.calls.append(ctx.tool_call_id)
        return await super().call(tool_name=tool_name, arguments=arguments, principal=principal, ctx=ctx)


@pytest.mark.asyncio
async def test_an_approved_tool_that_pauses_parks_the_node_again_on_its_own_event(tmp_path: Path) -> None:
    """The two-phase park: the approval is phase one; the approved tool then pauses the turn for ITS event, phase two. The node is waiting on the new key, not failed, and the answer to the pause is what
    the model reads for the call it made."""
    pausing = _PausingTool()
    run = _Run(tmp_path, providers={"y": pausing}, tools=["y__wait"], policies=[_policy("y", "wait")])
    checkpoint, _live, _seq, _yld = await run.park(_Model(_asks("y__wait", {})))
    first = _only_entry(checkpoint)
    assert first["event_key"] == "tool_approval:s-1:A:call-1" and pausing.calls == []

    resumed = await run.resume(_Model(_says("the pause was skipped")), checkpoint, first, {"decision": "approved"})

    assert pausing.calls == ["call-1"], "the approved tool ran, under the call's id"
    assert resumed.repark is not None, "the pause must re-park the node"
    assert resumed.repark.yielded.event_keys == ["k:call-1"]
    second = _only_entry(resumed.repark.graph_checkpoint)
    assert (second["node_id"], second["tool_name"], second["tool_call_id"], second["event_key"]) == ("A", "wait", "call-1", "k:call-1")
    assert [m["role"] for m in second["llm_messages"]] == ["assistant"], "the in-progress turn rides along: the assistant message that made the call, and no result yet"
    state = await resumed.executor.load_state()
    assert state is not None and state["status"] == "waiting" and state["node_states"]["A"]["status"] != "failed"

    # the tool's own event fires: its answer is the result of the call the model made
    model = _Model(_says("done"))
    answer = Message(role="tool", parts=[ToolResultPart(id="call-1", output="waited")])
    final = await run.resume(model, resumed.repark.graph_checkpoint, second, {"response": "ok"}, agent_tool_result=answer)
    assert final.repark is None
    assert [(p.id, p.output, p.error) for p in _read(model)] == [("call-1", "waited", False)]
    assert _asked(model) == ["call-1"]
    state = await final.executor.load_state()
    assert state is not None and (state["status"], state["ended_reason"]) == ("ended", "completed")


@pytest.mark.asyncio
async def test_an_approved_call_tool_inner_tool_that_pauses_parks_the_node_again(world: _World, tmp_path: Path) -> None:
    await world.sp.get_storage(ToolApprovalPolicy).create(_policy("y", "wait"))
    run = world.run(tmp_path)
    checkpoint, _live, _seq, _yld = await run.park(_Model(_call_tool("y", "wait")))
    first = _only_entry(checkpoint)

    resumed = await run.resume(_Model(_says("the pause was skipped")), checkpoint, first, {"decision": "approved"})

    assert resumed.repark is not None
    second = _only_entry(resumed.repark.graph_checkpoint)
    assert (second["tool_name"], second["tool_call_id"], second["event_key"]) == ("wait", "call-1", "k:call-1"), "it waits on the inner tool's own event, and the answer is the call_tool call's"
    assert [m["role"] for m in second["llm_messages"]] == ["assistant"]


@pytest.mark.asyncio
async def test_an_approved_ask_user_asks_the_person_and_the_answer_comes_back_as_the_result_of_the_call(world: _World, tmp_path: Path) -> None:
    """The whole two-phase park with the real tool and the real resume hook, nothing stubbed: the gated tool is the system toolset's own ``ask_user``. Approving it asks the question (the node waits on
    ``ask_user``'s key), the person's answer is turned into the call's result by ``graph_agent_tool_result`` (the real hook), and the model reads it for the call it made."""
    run = _Run(tmp_path, providers={"system": world.system}, tools=["system__ask_user"], policies=[_policy("system", "ask_user")], registry=world.registry)
    checkpoint, _live, _seq, _yld = await run.park(_Model(_asks("system__ask_user", {"prompt": "which colour?"})))
    first = _only_entry(checkpoint)
    assert first["tool_name"] == "_approval"

    resumed = await run.resume(_Model(_says("the question was skipped")), checkpoint, first, {"decision": "approved"})

    assert resumed.repark is not None, "the approved ask_user must ask: the node waits for the person"
    second = _only_entry(resumed.repark.graph_checkpoint)
    assert (second["tool_name"], second["tool_call_id"], second["event_key"]) == ("ask_user", "call-1", "ask_user:s-1:A:call-1")
    assert second["resume_metadata"]["prompt"] == "which colour?"

    answer = await graph_resume_coordinator.graph_agent_tool_result(
        SimpleNamespace(_provider_registry=None), resumed.repark.graph_checkpoint, "call-1", {"response": "blue"}, session_id="s-1", event_key=second["event_key"],
    )
    assert answer is not None
    model = _Model(_says("blue it is"))
    final = await run.resume(model, resumed.repark.graph_checkpoint, second, {"response": "blue"}, agent_tool_result=answer)

    assert final.repark is None
    (result,) = _read(model)
    assert (result.id, result.error) == ("call-1", False) and json.loads(result.output) == {"response": "blue"}
    assert _asked(model) == ["call-1"]
    state = await final.executor.load_state()
    assert state is not None and (state["status"], state["ended_reason"]) == ("ended", "completed")


@pytest.mark.asyncio
async def test_an_approved_call_is_still_refused_by_the_role_floor_of_the_resumed_node(tmp_path: Path) -> None:
    """The approval decides about THIS call, not about who may make it: the manager's role floor is not bypassed (``bypass_approval`` skips the gate only), so an admin tool the node's identity may not run is refused with
    the floor's own message and never runs."""
    rec = _Recorder({"admin_x": "admin"})
    run = _Run(tmp_path, providers={"rec": rec}, tools=["rec__admin_x"], policies=[_policy("rec", "admin_x")], initiated_by=USER)
    checkpoint, _live, _seq, _yld = await run.park(_Model(_asks("rec__admin_x", ARGS)))
    entry = _only_entry(checkpoint)
    assert entry["tool_name"] == "_approval", "the gate fires before the role floor does"

    model = _Model(_says("understood"))
    await run.resume(model, checkpoint, entry, {"decision": "approved"})

    assert rec.calls == []
    (result,) = _read(model)
    assert (result.id, result.error) == ("call-1", True) and "requires the 'admin' role" in result.output


@pytest.mark.asyncio
@pytest.mark.parametrize("decision", ["approved", "rejected"])
async def test_the_answer_is_to_the_call_the_entry_waits_on_whatever_id_the_park_recorded(tmp_path: Path, decision: str) -> None:
    """The model made the call under the entry's ``tool_call_id`` and the prompt holds it as a tool_use. The helper reads the id of its answer from the park's own record of the call (``original_call``); they are the
    same at every park site, but an answer under another id leaves the call unanswered in the next request, so the answer carries the entry's."""
    rec = _Recorder({"user_x": "user"})
    run = _Run(tmp_path, providers={"rec": rec}, tools=[GATED], policies=[_policy("rec", "user_x")])
    checkpoint, _live, _seq, _yld = await run.park(_Model(_asks(GATED, ARGS)))
    entry = _only_entry(checkpoint)
    entry["resume_metadata"]["original_call"]["id"] = "stale-id"

    model = _Model(_says("done"))
    await run.resume(model, checkpoint, entry, {"decision": decision})

    (result,) = _read(model)
    assert result.id == "call-1" and _asked(model) == ["call-1"], "the result must pair with the tool_use the prompt holds"


_SCHEMA = {"type": "object", "properties": {}, "additionalProperties": True}


class _GateInGate(ToolsetProvider):
    """Toolset ``y2``: ``outer`` runs ``inner`` through a manager of its own under the id it was called with (what any tool that runs a gated tool does); both are gated by policy."""

    def __init__(self) -> None:
        self.manager: Any = None
        self.outer_runs = 0
        self.inner_runs = 0

    def required_role(self, tool_name: str) -> str:
        return "user"

    async def list_tools(self, *, principal: str | None = None):
        for name in ("outer", "inner"):
            yield Tool(id=name, description=name, toolset_id="y2", args_schema=_SCHEMA)

    async def call(self, *, tool_name: str, arguments: dict[str, Any], principal: str | None = None, ctx: Any = None) -> ToolCallResult:
        if tool_name == "outer":
            self.outer_runs += 1
            result = await self.manager.execute(ToolCallPart(id=ctx.tool_call_id, name="y2__inner", arguments={}), principal=principal)
            return ToolCallResult(output=f"outer({result.output})", is_error=result.error)
        self.inner_runs += 1
        return ToolCallResult(output="inner ran", is_error=False)


@pytest.mark.asyncio
async def test_a_second_gate_raised_by_the_approved_call_parks_the_node_again_and_is_told_apart_by_its_gate_id(tmp_path: Path) -> None:
    """The approved tool runs a second gated tool, and the gate it raises is under the same call id (as in the agent session). The node waits on it, the gate id is the second gate's own, a late echo of the
    FIRST decision (it names gate 1) cannot decide it, and its own decision runs the inner call once: the node completes and the model reads the inner call's answer for the call it made."""
    policies = [_policy("y2", "outer"), _policy("y2", "inner")]
    provider = _GateInGate()
    provider.manager = ToolExecutionManager(toolset_providers={"y2": provider}, workspace_session=_Holder(), initiated_by=USER)  # type: ignore[dict-item,arg-type]
    provider.manager._approval_resolver = _PoliciesOnlyResolver(policies)
    run = _Run(tmp_path, providers={"y2": provider}, tools=["y2__outer", "y2__inner"], policies=policies)
    checkpoint, _live, _seq, _yld = await run.park(_Model(_asks("y2__outer", {})))
    first = _only_entry(checkpoint)
    assert first["resume_metadata"]["original_call"]["name"] == "y2__outer"

    resumed = await run.resume(_Model(_says("the second gate was skipped")), checkpoint, first, {"decision": "approved"})

    assert resumed.repark is not None and (provider.outer_runs, provider.inner_runs) == (1, 0), "the approved outer tool ran and hit the inner gate"
    second = _only_entry(resumed.repark.graph_checkpoint)
    assert second["tool_name"] == "_approval" and second["resume_metadata"]["original_call"]["name"] == "y2__inner"
    gate_one, gate_two = gate_id_of(first["resume_metadata"]), gate_id_of(second["resume_metadata"])
    assert gate_one and gate_two and gate_one != gate_two, "the second gate is a gate of its own"

    echo = await run.executor(_Model(_says("the echo decided the second gate")))
    with pytest.raises(YieldToWorker) as still_parked:
        async for _ev in echo.resume_from_checkpoint(
            resumed.repark.graph_checkpoint, resumed_tcid=second["tool_call_id"], resumed_event_key=second["event_key"], resumed_gate_id=gate_one,
            toolcall_payload={"decision": "approved"},
        ):
            pass
    assert provider.inner_runs == 0, "a decision on gate 1 ran the call of gate 2"
    assert [e["event_key"] for e in still_parked.value.graph_checkpoint["pending_agent_yields"]] == [second["event_key"]]

    model = _Model(_says("done"))
    final = await run.resume(model, resumed.repark.graph_checkpoint, second, {"decision": "approved"})

    assert final.repark is None and (provider.outer_runs, provider.inner_runs) == (1, 1), "the inner call ran once, and the outer tool was not run again"
    (result,) = _read(model)
    assert (result.id, result.error, result.output) == ("call-1", False, "inner ran")
    assert _asked(model) == ["call-1"]


# ---- one approval runs at most one gated call -------------------------------------------------------------------------------------------------------------------


def _two_agents_graph() -> Graph:
    return Graph(
        id="g-two", description="begin -> {A, B}; A -> C; B -> D",
        nodes=[
            _BeginNode(id="begin"), _AgentNodeRef(id="A", agent_id="x", input_template="go"), _AgentNodeRef(id="B", agent_id="x", input_template="go"),
            _EndNode(id="C", output_template="{{ nodes.A.text }}"), _EndNode(id="D", output_template="{{ nodes.B.text }}"),
        ],
        edges=[_StaticEdge(from_node="begin", to_node="A"), _StaticEdge(from_node="begin", to_node="B"), _StaticEdge(from_node="A", to_node="C"), _StaticEdge(from_node="B", to_node="D")],
    )


async def _two_parked_gates(tmp_path: Path) -> tuple[_Run, _Recorder, dict, dict, dict]:
    """A superstep that parks on TWO agent-node approval gates (both nodes call the gated tool): the run, the tool's recorder, the checkpoint and the two entries."""
    rec = _Recorder({"user_x": "user"})
    run = _Run(tmp_path, providers={"rec": rec}, tools=[GATED], policies=[_policy("rec", "user_x")], graph=_two_agents_graph())

    class _BothAsk(_Model):          # one model for both nodes: each node's first request asks for the gated tool, with its own raw call id
        def __init__(self) -> None:
            super().__init__()
            self.asked = 0

        def stream(self, **kw: Any) -> AsyncIterator[StreamEvent]:
            self.requests.append(list(kw.get("messages") or []))
            self.asked += 1
            reply = _asks(GATED, ARGS, call_id="call-1")                  # the same raw id for both siblings, as a provider that numbers its calls itself gives

            async def events() -> AsyncIterator[StreamEvent]:
                for event in reply:
                    yield event

            return events()

    checkpoint, _live, _seq, _yld = await run.park(_BothAsk())
    entries = {e["node_id"]: e for e in checkpoint["pending_agent_yields"]}
    assert set(entries) == {"A", "B"} and rec.calls == []
    return run, rec, checkpoint, entries["A"], entries["B"]


@pytest.mark.asyncio
async def test_an_approval_that_names_one_of_two_gates_runs_only_that_one(tmp_path: Path) -> None:
    run, rec, checkpoint, a, b = await _two_parked_gates(tmp_path)
    assert a["event_key"] != b["event_key"] and a["tool_call_id"] == b["tool_call_id"], "the siblings share the raw call id, and their keys are told apart by the node"

    resumed = await run.resume(_Model(_says("done")), checkpoint, a, {"decision": "approved"})

    assert [c[3].tool_call_id for c in rec.calls] == ["call-1"] and len(rec.calls) == 1, "one approval ran one call"
    assert resumed.repark is not None
    assert [e["node_id"] for e in resumed.repark.graph_checkpoint["pending_agent_yields"]] == ["B"], "the sibling the decision did not name stays parked"


@pytest.mark.asyncio
async def test_an_approval_that_cannot_be_shown_to_be_one_gate_runs_none_of_them(tmp_path: Path) -> None:
    """The key-less drain (a park from before the fired key was stamped) selects every pending entry. One approval cannot be shown to be about any one of several gates, so none of them runs and all stay parked."""
    run, rec, checkpoint, a, _b = await _two_parked_gates(tmp_path)
    executor = await run.executor(_Model(_says("a"), _says("b")))
    with pytest.raises(YieldToWorker) as repark:
        async for _ev in executor.resume_from_checkpoint(checkpoint, toolcall_payload={"decision": "approved"}):
            pass
    assert rec.calls == [], "one approval ran a gated call it cannot be shown to be about"
    assert sorted(e["node_id"] for e in repark.value.graph_checkpoint["pending_agent_yields"]) == ["A", "B"]


@pytest.mark.asyncio
async def test_siblings_that_share_a_key_run_only_the_gate_the_decision_named(tmp_path: Path) -> None:
    """A park from before the gate keys were node-scoped: both entries sit on one key, and the decision on it selects both. The wake names the gate it decided; only that entry runs."""
    run, rec, checkpoint, a, b = await _two_parked_gates(tmp_path)
    for entry in checkpoint["pending_agent_yields"]:
        entry["event_key"] = "tool_approval:s-1:call-1"
    assert gate_id_of(a["resume_metadata"]) != gate_id_of(b["resume_metadata"])
    executor = await run.executor(_Model(_says("a"), _says("b")))

    with pytest.raises(YieldToWorker) as repark:
        async for _ev in executor.resume_from_checkpoint(
            checkpoint, resumed_tcid="call-1", resumed_event_key="tool_approval:s-1:call-1", resumed_gate_id=gate_id_of(b["resume_metadata"]),
            toolcall_payload={"decision": "approved"},
        ):
            pass

    assert len(rec.calls) == 1, f"one approval ran {len(rec.calls)} gated calls"
    assert [e["node_id"] for e in repark.value.graph_checkpoint["pending_agent_yields"]] == ["A"], "the sibling the decision did not name stays parked"


def _mixed_graph() -> Graph:
    """An agent node and a ToolCall node in one superstep, both calling the gated tool."""
    return Graph(
        id="g-mixed", description="begin -> {A, T}; A -> C; T -> D",
        nodes=[
            _BeginNode(id="begin"), _AgentNodeRef(id="A", agent_id="x", input_template="go"), _ToolCallNode(id="T", tool_id=GATED, arguments=ARGS),
            _EndNode(id="C", output_template="{{ nodes.A.text }}"), _EndNode(id="D", output_template="{{ nodes.T.text }}"),
        ],
        edges=[_StaticEdge(from_node="begin", to_node="A"), _StaticEdge(from_node="begin", to_node="T"), _StaticEdge(from_node="A", to_node="C"), _StaticEdge(from_node="T", to_node="D")],
    )


async def _agent_gate_and_tool_node_inner_gate(tmp_path: Path) -> tuple[_Run, _Recorder, dict, dict, dict]:
    """A superstep parked on an agent node's approval gate AND a ToolCall node's: the run, the recorder, the checkpoint and the two entries. The ToolCall node's entry is made the park of an INNER call (what a tool
    that runs a gated tool through the manager, or ``call_tool``, leaves): its ``original_call`` is not the node's own call."""
    rec = _Recorder({"user_x": "user", "other": "user"})
    run = _Run(tmp_path, providers={"rec": rec}, tools=[GATED, "rec__other"], policies=[_policy("rec", "user_x")], graph=_mixed_graph())
    checkpoint, _live, _seq, _yld = await run.park(_Model(_asks(GATED, ARGS)))
    (agent_entry,) = checkpoint["pending_agent_yields"]
    (tool_entry,) = checkpoint["pending_toolcalls"]
    tool_entry["resume_metadata"]["original_call"] = {"id": tool_entry["tool_call_id"], "name": "rec__other", "arguments": {}}
    assert rec.calls == []
    return run, rec, checkpoint, agent_entry, tool_entry


@pytest.mark.asyncio
async def test_one_approval_runs_one_gated_call_among_an_agent_nodes_gate_and_a_tool_nodes(tmp_path: Path) -> None:
    """The count is over every entry that would run a gated call, not per kind of entry: an approval that names no gate and selects an agent node's gate AND a ToolCall node's inner-call gate runs neither."""
    run, rec, checkpoint, agent_entry, tool_entry = await _agent_gate_and_tool_node_inner_gate(tmp_path)

    executor = await run.executor(_Model(_says("a")))
    with pytest.raises(YieldToWorker) as repark:
        async for _ev in executor.resume_from_checkpoint(checkpoint, toolcall_payload={"decision": "approved"}):
            pass

    assert rec.calls == [], "one approval ran a gated call it cannot be shown to be about"
    left = repark.value.graph_checkpoint
    assert [e["node_id"] for e in left["pending_agent_yields"]] == ["A"] and [e["node_id"] for e in left["pending_toolcalls"]] == ["T"]


@pytest.mark.asyncio
async def test_an_approval_that_names_the_tool_nodes_gate_runs_only_its_inner_call(tmp_path: Path) -> None:
    """The control of the case above: the same checkpoint and decision, selected by the ToolCall node's key, runs its inner call, and the agent node's gate stays parked."""
    run, rec, checkpoint, agent_entry, tool_entry = await _agent_gate_and_tool_node_inner_gate(tmp_path)

    executor = await run.executor(_Model(_says("a")))
    with pytest.raises(YieldToWorker) as repark:
        async for _ev in executor.resume_from_checkpoint(
            checkpoint, resumed_tcid=tool_entry["tool_call_id"], resumed_event_key=tool_entry["parked_event_key"], toolcall_payload={"decision": "approved"},
        ):
            pass

    assert [c[0] for c in rec.calls] == ["other"], "the inner call of the gate the decision named ran"
    assert [e["node_id"] for e in repark.value.graph_checkpoint["pending_agent_yields"]] == ["A"], "the agent node's gate stays parked"


@pytest.mark.asyncio
async def test_a_wake_that_decided_nothing_runs_no_approval_gate(tmp_path: Path) -> None:
    """A ``tool_wait`` batch's wake reaches the executor with a made-up "approved" payload and ``wake_only``: nothing was decided, so no human gate is selected and none runs."""
    rec = _Recorder({"user_x": "user"})
    run = _Run(tmp_path, providers={"rec": rec}, tools=[GATED], policies=[_policy("rec", "user_x")])
    checkpoint, _live, _seq, _yld = await run.park(_Model(_asks(GATED, ARGS)))

    _decision, repark, _seq2 = await resume_graph_from_checkpoint(
        executor=await run.executor(_Model(_says("a"))), checkpoint=checkpoint, payload={"decision": "approved"}, resumed_tcid=None, wake_only=True,
    )

    assert rec.calls == []
    assert repark is not None and [e["node_id"] for e in repark.graph_checkpoint["pending_agent_yields"]] == ["A"]


@pytest.mark.asyncio
async def test_a_rejection_that_names_no_gate_ends_every_gate_it_selects(tmp_path: Path) -> None:
    """The no-gate rule is for approvals: a rejection (the deadline sweeper's timeout names no gate either) answers every entry it selects with the rejection, so no gate waits for ever."""
    run, rec, checkpoint, _a, _b = await _two_parked_gates(tmp_path)
    model = _Model(_says("a"), _says("b"))
    executor = await run.executor(model)

    async for _ev in executor.resume_from_checkpoint(checkpoint, toolcall_payload={"decision": "rejected", "reason": "no"}, approved=False):
        pass

    assert rec.calls == []
    assert [(p.id, p.error) for p in _read(model)] == [("call-1", True), ("call-1", True)]


# ---- the answer is recorded under the call row ------------------------------------------------------------------------------------------------------------------


def _results(records: list[dict]) -> list[dict]:
    return [r for r in records if r.get("kind") == "tool_result"]


def _call_row(live: list[dict]) -> dict:
    (call,) = [r for r in live if r["kind"] == "tool_call"]
    return call


@pytest.mark.asyncio
async def test_the_approved_tools_answer_is_recorded_under_the_scoped_id_of_its_call_row(tmp_path: Path) -> None:
    rec = _Recorder({"user_x": "user"})
    run = _Run(tmp_path, providers={"rec": rec}, tools=[GATED], policies=[_policy("rec", "user_x")])
    checkpoint, live, seq, _yld = await run.park(_Model(_asks(GATED, ARGS)))
    entry = _only_entry(checkpoint)
    assert entry["scoped_tool_call_id"] == _call_row(live)["payload"]["id"], "the park stashed the scoped id of the call row"

    resumed = await run.resume(_Model(_says("done")), checkpoint, entry, {"decision": "approved"}, seq=seq, with_tap=True)

    assert len(_results(resumed.records)) == 1, f"the answer of the call was not recorded: {resumed.records}"
    (result,) = _results(resumed.records)
    assert (result["payload"]["call_id"], result["payload"]["error"], result["node_id"]) == (_call_row(live)["payload"]["id"], False, "A")
    assert json.loads(result["payload"]["output"]) == {"ran": "user_x"}


@pytest.mark.asyncio
async def test_a_rejection_is_recorded_as_an_error_under_the_scoped_id_of_its_call_row(tmp_path: Path) -> None:
    rec = _Recorder({"user_x": "user"})
    run = _Run(tmp_path, providers={"rec": rec}, tools=[GATED], policies=[_policy("rec", "user_x")])
    checkpoint, live, seq, _yld = await run.park(_Model(_asks(GATED, ARGS)))
    entry = _only_entry(checkpoint)

    resumed = await run.resume(_Model(_says("done")), checkpoint, entry, {"decision": "rejected", "reason": "no"}, seq=seq, with_tap=True)

    assert len(_results(resumed.records)) == 1, f"the rejection of the call was not recorded: {resumed.records}"
    (result,) = _results(resumed.records)
    assert (result["payload"]["call_id"], result["payload"]["error"], result["node_id"]) == (_call_row(live)["payload"]["id"], True, "A")
    assert json.loads(result["payload"]["output"])["rejected"] is True


@pytest.mark.asyncio
async def test_the_node_parked_again_by_the_approved_tool_keeps_the_scoped_id_of_its_call_row(tmp_path: Path) -> None:
    """The call row stays open while the approved tool waits for its own event; the answer that closes it is written under the SAME scoped id, so the re-parked entry carries the id of the row."""
    pausing = _PausingTool()
    run = _Run(tmp_path, providers={"y": pausing}, tools=["y__wait"], policies=[_policy("y", "wait")])
    checkpoint, live, seq, _yld = await run.park(_Model(_asks("y__wait", {})))
    entry = _only_entry(checkpoint)

    resumed = await run.resume(_Model(_says("the pause was skipped")), checkpoint, entry, {"decision": "approved"}, seq=seq, with_tap=True)

    assert resumed.repark is not None and _results(resumed.records) == [], "no answer yet: the call is still waiting"
    assert _only_entry(resumed.repark.graph_checkpoint)["scoped_tool_call_id"] == _call_row(live)["payload"]["id"]


# ---- a child graph (invoke_graph) --------------------------------------------------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_agent_node_of_a_child_graph_runs_an_approved_tool(tmp_path: Path) -> None:
    """``invoke_graph`` resumes its child graph through ``resume_invoke_graph``, which hands the executor the decision and no answer: the child's agent node is answered the same way."""
    rec = _Recorder({"user_x": "user"})
    run = _Run(tmp_path, providers={"rec": rec}, tools=[GATED], policies=[_policy("rec", "user_x")])
    checkpoint, _live, _seq, _yld = await run.park(_Model(_asks(GATED, ARGS)))
    entry = _only_entry(checkpoint)

    model = _Model(_says("done"))
    text, repark = await resume_invoke_graph(
        child=await run.executor(model), checkpoint=checkpoint, payload={"decision": "approved"}, resumed_tcid=entry["tool_call_id"], resumed_event_key=entry["event_key"],
    )

    assert repark is None and text == "done"
    assert [c[0] for c in rec.calls] == ["user_x"]
    (result,) = _read(model)
    assert json.loads(result.output) == {"ran": "user_x"}


@pytest.mark.asyncio
async def test_the_agent_node_of_a_child_graph_reads_a_rejection(tmp_path: Path) -> None:
    rec = _Recorder({"user_x": "user"})
    run = _Run(tmp_path, providers={"rec": rec}, tools=[GATED], policies=[_policy("rec", "user_x")])
    checkpoint, _live, _seq, _yld = await run.park(_Model(_asks(GATED, ARGS)))
    entry = _only_entry(checkpoint)

    model = _Model(_says("understood"))
    try:
        await resume_invoke_graph(
            child=await run.executor(model), checkpoint=checkpoint, payload={"decision": "rejected", "reason": "no"}, resumed_tcid=entry["tool_call_id"],
            resumed_event_key=entry["event_key"],
        )
    except ChildGraphFailed:                    # the agent node itself completes; a failed child would not have asked the model anything
        pytest.fail("a rejected gate of an agent node is an answer the model reads, not a failure of the child graph")

    assert rec.calls == []
    (result,) = _read(model)
    assert (result.id, result.error) == ("call-1", True) and json.loads(result.output)["rejected"] is True


# ---- the real engine ----------------------------------------------------------------------------------------------------------------------------------------------


class _AgentNodePool(EngineFakePool):
    async def _graph_agent_tool_result(self, checkpoint: dict, tcid: str, payload: Any, *, session_id: str, event_key: str | None = None) -> None:
        return None            # an approval gate has no hook-built answer, as on the real pool (``graph_agent_tool_result`` returns ``None`` for ``_approval``)


@pytest.mark.asyncio
@pytest.mark.parametrize("approved", [True, False], ids=["approved", "rejected"])
async def test_the_real_graph_resume_applies_the_decision_to_an_agent_nodes_gate(tmp_path: Path, approved: bool) -> None:
    """``resume_graph_engine`` is the worker's entry for a graph park. It drains the reply, writes the approval record, and hands the executor the decision: the tool runs on an approval and not on a rejection."""
    rec = _Recorder({"user_x": "user"})
    run = _Run(tmp_path, providers={"rec": rec}, tools=[GATED], policies=[_policy("rec", "user_x")])
    checkpoint, _live, _seq, yld = await run.park(_Model(_asks(GATED, ARGS)))
    entry = _only_entry(checkpoint)
    model = _Model(_says("done"))

    async def factory() -> WorkspaceGraphExecutor:
        return await run.executor(model)

    pool = _AgentNodePool(storage=EngineStorageProvider(), workspace_io=NullWorkspaceIO(), executor_factory=factory)
    payload = with_wake_gate({"decision": "approved" if approved else "rejected", "decided_by": "alice"}, gate_id_of(entry["resume_metadata"]))
    session = waiting_graph_session()
    session.parked_state = {"resume_event_payloads": {"k": {"event_key": entry["event_key"], "payload": payload}}}
    parked = ParkedState(
        yielded=yld.yielded, llm_messages=[], turn_no=0, started_at=datetime.now(UTC), tool_call_id=yld.tool_call_id, resume_event_payload=payload, graph_checkpoint=checkpoint,
    )

    outcome = await graph_resume_coordinator.resume_graph_engine(pool, session, parked)

    assert outcome == "ENDED:completed" and pool.repark_calls == []
    assert pool.approval_record_event_keys == [entry["event_key"]], "the decision was recorded once, on the gate it named"
    (result,) = _read(model)
    if approved:
        assert [c[0] for c in rec.calls] == ["user_x"] and result.error is False and json.loads(result.output) == {"ran": "user_x"}
    else:
        assert rec.calls == [] and result.error is True and json.loads(result.output)["rejected"] is True
