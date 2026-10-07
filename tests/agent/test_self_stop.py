"""A session that stops ITSELF through ``interrupt_workspace_session`` (task 01a10871, ruling D4).

The tool can be called by the very turn it stops, and by a subagent running inside that turn (a subagent shares the outer session's id).
What each does is the loop's, not the tool's, so this drives the REAL pieces from the tool down: the real ``workspaces`` toolset and its
real interrupt tool, the real shared helper over an in-memory session row, the real ``ToolExecutionManager`` (role floor, allowlist,
``ToolContext``), ``run_stoppable`` and ``run_agent_turn``; for the subagent case also the real ``run_subagent``. Only the model is
scripted, and the bus stands in for the worker's ``_cancel_watcher``: publishing ``session:{sid}:cancel`` sets the turn's Stop event, which is
all the watcher does with that key.

What it pins: the interrupt call keeps its REAL result (the Stop never throws a real result away), the calls after it in the same round
are answered "not run: stopped by user" and never run, the turn ends as a Stop before the next model call, the history stays valid for both
providers; from inside a subagent, no later call of the subagent runs either, and the PARENT's ``invoke_agent`` call is answered
"interrupted: stopped by user" (the subagent ended on the Stop and said so).
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from datetime import datetime, timezone
from typing import Any

import pytest

import primer.agent.stoppable_call as stoppable_call
import primer.workspace  # noqa: F401  (makes ToolCallContext.model_rebuild() run)
from primer.agent.invoke import _SubagentSession
from primer.agent.loop import _STOPPED_REFUSAL, run_agent_turn
from primer.agent.tool_manager import ToolExecutionManager
from primer.model.agent import Agent, AgentModel
from primer.model.chat import (
    Done,
    Message,
    StreamStart,
    TextDelta,
    TextPart,
    Tool,
    ToolCallEnd,
    ToolCallResult,
    ToolCallStart,
    ToolResultPart,
)
from primer.model.model_profile import ModelProfileConfig
from primer.model.principal import PrincipalRef
from primer.model.workspace_session import AgentSessionBinding, SessionStatus, WorkspaceSession
from primer.model_profile import ResolvedModel
from primer.toolset.workspaces import build_workspaces_toolset
from tests._support.provider_history import assert_anthropic_valid, assert_openai_valid
from tests.agent.test_stop_abandons_a_subagent_call import (
    CALL_ID,
    INTERRUPTED,
    PARENT,
    PARENT_MODEL,
    _agent,
    _ParentManager,
    _profile,
    _Registry,
    _Storage,
)

WID, SID = "ws-parent", "sess-parent"      # what the subagent harness's _ParentManager runs its subagent under
INTERRUPT = "workspaces__interrupt_workspace_session"
MODEL = ResolvedModel(profile_id="p", provider_id="prov", model_name="m", context_length=4096, config=ModelProfileConfig())
AGENT = Agent(id="ag", description="x", model=AgentModel(profile_id="p--m"), max_tool_turns=10)
_SCHEMA = {"type": "object", "properties": {}, "additionalProperties": False}


class _WatcherBus:
    """The worker's ``_cancel_watcher`` reduced to what it does with the cancel key: set the turn's Stop event.

    ``watching=False`` is the CONTROL: a bus that records the publish but never reaches the turn, so the same scripts run with no
    Stop in force and the assertions below can be seen to be about the Stop and not about the scripts."""

    def __init__(self, stop: asyncio.Event, *, watching: bool = True) -> None:
        self.stop = stop
        self.watching = watching
        self.keys: list[str] = []

    async def publish(self, key: str, payload: Any) -> None:
        self.keys.append(key)
        if self.watching and key == f"session:{SID}:cancel":
            self.stop.set()


class _Recorder:
    """A toolset whose one tool records that it ran: the side effect that must not happen after the Stop."""

    def __init__(self) -> None:
        self.executed: list[str] = []

    def required_role(self, tool_name: str) -> str:
        return "user"

    def is_yielding(self, tool_name: str) -> bool:
        return False

    async def list_tools(self, *, principal: str | None = None) -> AsyncIterator[Tool]:
        yield Tool(id="exec", description="runs a command", toolset_id="rec", args_schema=_SCHEMA)

    async def call(self, *, tool_name: str, arguments: dict[str, Any], principal: str | None = None, ctx=None):
        self.executed.append(tool_name)
        return ToolCallResult(output="ran", is_error=False)


class _OneRoundLlm:
    """Round 1 asks for ``calls`` (id, scoped name, arguments); any later round would just answer (a Stop must prevent it)."""

    def __init__(self, calls: list[tuple[str, str, dict]]) -> None:
        self.calls = calls
        self.requests = 0

    def stream(self, *, model=None, messages=None, **_kwargs):
        self.requests += 1
        first = self.requests == 1

        async def gen():
            yield StreamStart(model="m1")
            if first:
                for index, (call_id, name, arguments) in enumerate(self.calls):
                    yield ToolCallStart(id=call_id, name=name, index=index)
                    yield ToolCallEnd(id=call_id, arguments=arguments, index=index)
                yield Done(stop_reason="tool_use", raw_reason="tool_use")
            else:
                yield TextDelta(index=0, text="the subagent's answer")
                yield Done(stop_reason="stop", raw_reason="stop")

        return gen()


async def _seed_running_session(storage_provider) -> None:
    now = datetime(2026, 10, 7, 10, 0, 0, tzinfo=timezone.utc)
    await storage_provider.get_storage(WorkspaceSession).create(WorkspaceSession(
        id=SID, workspace_id=WID, binding=AgentSessionBinding(agent_id="ag1"), status=SessionStatus.RUNNING,
        last_seq=1, turn_status="running", created_at=now,
    ))


def _tool_results(messages: list[Message]) -> list[ToolResultPart]:
    return [part for m in messages if m.role == "tool" for part in m.parts if isinstance(part, ToolResultPart)]


@pytest.fixture(autouse=True)
async def _no_abandoned_leftovers():
    yield
    abandoned = list(stoppable_call._ABANDONED)
    for task in abandoned:
        task.cancel()
    if abandoned:
        await asyncio.wait(abandoned, timeout=3.0)
    stoppable_call._ABANDONED.clear()


async def _run_the_self_stopping_turn(fake_storage_provider, *, watching: bool):
    await _seed_running_session(fake_storage_provider)
    stop = asyncio.Event()
    bus = _WatcherBus(stop, watching=watching)
    recorder = _Recorder()
    toolset = build_workspaces_toolset(
        storage_provider=fake_storage_provider, workspace_registry=object(), scheduler=None, claim_engine=None, event_bus=bus,
    )
    manager = ToolExecutionManager(
        toolset_providers={"workspaces": toolset, "rec": recorder},
        workspace_session=_SubagentSession(session_id=SID, workspace_id=WID, agent_id="ag"),
        tools=[INTERRUPT, "rec__exec"],
        initiated_by=PrincipalRef.system(),
    )
    llm = _OneRoundLlm([
        ("c1", INTERRUPT, {"workspace_id": WID, "session_id": SID}),
        ("c2", "rec__exec", {}),
        ("c3", "rec__exec", {}),
    ])
    messages_out: list[Message] = []
    interrupted: list[bool] = []

    async def drive() -> None:
        async for _ in run_agent_turn(
            agent=AGENT, llm=llm, llm_model=MODEL, tool_manager=manager,
            prompt=[Message(role="user", parts=[TextPart(text="go")])],
            messages_out=messages_out, interrupt=stop, interrupted_out=interrupted,
        ):
            pass

    await asyncio.wait_for(drive(), 5.0)
    return bus, recorder, llm, messages_out, interrupted


async def test_a_turn_that_stops_itself_keeps_the_stop_calls_result_and_runs_nothing_after_it(fake_storage_provider) -> None:
    """The handler returns right after its publish with no further suspension, so the call finishes in the SAME wake-up as the Stop:
    its real result is kept (``run_stoppable``: the Stop never throws a real result away)."""
    bus, recorder, llm, messages_out, interrupted = await _run_the_self_stopping_turn(fake_storage_provider, watching=True)

    results = _tool_results(messages_out)
    assert [r.id for r in results] == ["c1", "c2", "c3"]
    first = json.loads(results[0].output)
    assert results[0].error is False and first["id"] == SID and first["interrupt_requested"] is True, results[0]
    assert [(r.output, r.error) for r in results[1:]] == [(_STOPPED_REFUSAL, True)] * 2, "the later calls are refused, not run"
    assert recorder.executed == [], "a call after the Stop ran"
    assert bus.keys == [f"session:{SID}:cancel"]
    assert (await fake_storage_provider.get_storage(WorkspaceSession).get(SID)).interrupt_requested is True
    assert interrupted == [True] and llm.requests == 1, "the turn must end before the next model call"
    assert [m.role for m in messages_out] == ["assistant", "tool"], "one completed, paired round"
    history = [Message(role="user", parts=[TextPart(text="go")]), *messages_out]
    assert_anthropic_valid(history)
    assert_openai_valid(history)


async def test_control_without_a_watcher_the_same_turn_runs_its_later_calls_and_asks_the_model_again(fake_storage_provider) -> None:
    """The Stop is recorded on the row and published, but nothing sets the turn's Stop event (no watcher): the later calls RUN and a
    second model call is made, so the refusals and the early end above are the Stop's doing."""
    bus, recorder, llm, messages_out, interrupted = await _run_the_self_stopping_turn(fake_storage_provider, watching=False)

    assert bus.keys == [f"session:{SID}:cancel"]
    assert recorder.executed == ["exec", "exec"]
    assert llm.requests == 2 and interrupted == []
    assert _STOPPED_REFUSAL not in [r.output for r in _tool_results(messages_out)]


class _ParentOnceLlm:
    """Round 1 calls the subagent; a second round (which a Stop must prevent) just answers. (The harness's own ``_ParentLLM`` asks for
    the subagent on EVERY round, which a control with no Stop would follow to the tool-turn cap.)"""

    def __init__(self) -> None:
        self.requests = 0

    def stream(self, **_kwargs):
        self.requests += 1
        first = self.requests == 1

        async def gen():
            if first:
                yield ToolCallStart(id=CALL_ID, name="invoke_sub", index=0)
                yield ToolCallEnd(id=CALL_ID, arguments={}, index=0)
                yield Done(stop_reason="tool_use", raw_reason="tool_use")
            else:
                yield TextDelta(index=0, text="parent done")
                yield Done(stop_reason="stop", raw_reason="stop")

        return gen()


class _SelfStoppingSubagentLlm:
    """The subagent's first round stops its own (outer) session, then asks for a command that must never run."""

    def __init__(self) -> None:
        self.requests = 0

    def stream(self, *, model=None, messages=None, **_kwargs):
        self.requests += 1
        first = self.requests == 1

        async def gen():
            yield StreamStart(model="m1")
            if first:
                yield ToolCallStart(id="sub-1", name=INTERRUPT, index=0)
                yield ToolCallEnd(id="sub-1", arguments={"workspace_id": WID, "session_id": SID}, index=0)
                yield ToolCallStart(id="sub-2", name="rec__exec", index=1)
                yield ToolCallEnd(id="sub-2", arguments={}, index=1)
                yield Done(stop_reason="tool_use", raw_reason="tool_use")
            else:
                yield TextDelta(index=0, text="the subagent's answer")
                yield Done(stop_reason="stop", raw_reason="stop")

        return gen()


async def _run_the_self_stopping_subagent(fake_storage_provider, *, watching: bool):
    await _seed_running_session(fake_storage_provider)
    stop = asyncio.Event()
    bus = _WatcherBus(stop, watching=watching)
    recorder = _Recorder()
    toolset = build_workspaces_toolset(
        storage_provider=fake_storage_provider, workspace_registry=object(), scheduler=None, claim_engine=None, event_bus=bus,
    )
    sub_llm = _SelfStoppingSubagentLlm()
    storage = _Storage(
        agents={"agent-s": _agent("agent-s", "prov-1--m1", [INTERRUPT, "rec__exec"])},
        profiles={"prov-1--m1": _profile("prov-1--m1", "prov-1", "m1")},
    )
    registry = _Registry(llms={"prov-1": sub_llm}, toolsets={"workspaces": toolset, "rec": recorder})
    parent_manager = _ParentManager("agent-s", storage, registry)
    parent_llm = _ParentOnceLlm()
    messages_out: list[Message] = []
    interrupted: list[bool] = []

    async def drive() -> None:
        async for _ in run_agent_turn(
            agent=PARENT, llm=parent_llm, llm_model=PARENT_MODEL, tool_manager=parent_manager,
            prompt=[Message(role="user", parts=[TextPart(text="go")])], messages_out=messages_out,
            interrupt=stop, interrupted_out=interrupted,
        ):
            pass

    await asyncio.wait_for(drive(), 5.0)
    return bus, recorder, sub_llm, parent_llm, messages_out, interrupted


async def test_a_subagent_that_stops_the_session_it_runs_in_ends_and_the_parents_call_is_answered_interrupted(
    fake_storage_provider,
) -> None:
    """Task 01a10871 ruling D4: the self-stop from inside an invoke_agent subagent. The subagent shares the outer session id, so the
    row it stops is the parent's; it holds the parent turn's Stop event, so its own later calls do not run, and the parent's call to it
    is answered "interrupted: stopped by user" because it ended on the Stop."""
    bus, recorder, sub_llm, parent_llm, messages_out, interrupted = await _run_the_self_stopping_subagent(
        fake_storage_provider, watching=True,
    )

    assert (await fake_storage_provider.get_storage(WorkspaceSession).get(SID)).interrupt_requested is True, "the Stop was recorded"
    assert bus.keys == [f"session:{SID}:cancel"]
    assert recorder.executed == [], "the subagent ran a call after it had stopped the session"
    assert sub_llm.requests == 1, "the subagent asked its model again after the Stop"
    assert [(r.output, r.error) for r in _tool_results(messages_out)] == [(INTERRUPTED, True)]
    assert interrupted == [True] and parent_llm.requests == 1, "the parent asked its model again after the Stop"
    assert [m.role for m in messages_out] == ["assistant", "tool"]
    history = [Message(role="user", parts=[TextPart(text="go")]), *messages_out]
    assert_anthropic_valid(history)
    assert_openai_valid(history)


async def test_control_without_a_watcher_the_subagent_runs_on_and_returns_its_answer(fake_storage_provider) -> None:
    """The same subagent with no Stop in force: it runs its second call, asks its model again, and the parent gets its real answer."""
    bus, recorder, sub_llm, parent_llm, messages_out, interrupted = await _run_the_self_stopping_subagent(
        fake_storage_provider, watching=False,
    )

    assert recorder.executed == ["exec"] and sub_llm.requests == 2 and interrupted == []
    assert parent_llm.requests == 2
    assert [(r.output, r.error) for r in _tool_results(messages_out)] == [("the subagent's answer", False)]
