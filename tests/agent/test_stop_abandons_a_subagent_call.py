"""A Stop that gives up on a subagent call must not let it, or anything it started, append to the parent log afterwards.

``invoke_agent`` runs a REAL subagent inside the delegating turn and feeds every event it emits to the turn's
``DelegationRecorder``, stamped with the id of the call that asked for it. When a Stop cancels the call but the subagent
does not unwind in time (here: it swallows the cancel and carries on), ``run_stoppable`` abandons the call.

A subagent is now given the turn's Stop event (``current_interrupt``), and one that is given it ends at its next check
whatever its cancel did (``tests/agent/test_subagent_stop.py``), so it would not carry on emitting here. This file is about
what the PARENT does with a call that does not stop, whatever the reason (a subagent resumed after a park still gets no
event), so the Stop-path tests WITHHOLD the event from the subagent on purpose (``_stop_a_blocked_subagent``). Abandonment
follows the call's TASK TREE, not one id: a subagent that itself delegated (``invoke_agent`` inside ``invoke_agent``) tags
its events with the INNER call's id, and those must be dropped too, or the parent log would show the subagent continuing
after the Stop's answer.

These tests drive the real ``run_subagent`` (through a real ``ToolExecutionManager`` built internally from tiny fakes, as
``tests/agent/test_run_subagent_yield.py`` does), the real ``run_agent_turn`` loop and the real ``DelegationRecorder``.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any

import pytest

import primer.agent.invoke as invoke_module
import primer.agent.stoppable_call as stoppable_call
from primer.agent.call_scope import CallScope
from primer.agent.invoke import run_subagent
from primer.agent.loop import run_agent_turn
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
from primer.model.model_profile import ModelProfile, ModelProfileConfig
from primer.model_profile import ResolvedModel
from primer.session.delegation import DelegationRecorder, reset_delegation_sink, set_delegation_sink
from tests._support.provider_history import assert_anthropic_valid, assert_openai_valid

PARENT_MODEL = ResolvedModel(
    profile_id="p", provider_id="prov", model_name="m", context_length=4096, config=ModelProfileConfig(),
)
PARENT = Agent(id="parent", description="x", model=AgentModel(profile_id="p--m"), max_tool_turns=10)
CALL_ID = "sub-call-1"
INNER_CALL_ID = "inner-1"
INTERRUPTED = "interrupted: stopped by user (the call may have run, and its result was not recorded)"


# --- the subagents' world: toolsets, storage, a provider registry, an LLM that will not stop ------------------------------


class _Store:
    def __init__(self, objs: dict[str, Any]) -> None:
        self._objs = objs

    async def get(self, key: str) -> Any:
        return self._objs.get(key)


class _Storage:
    """Serves the rows ``run_subagent`` resolves, by id, so two agents can have two different models."""

    def __init__(self, agents: dict[str, Agent], profiles: dict[str, ModelProfile]) -> None:
        self._agents = agents
        self._profiles = profiles

    async def get_system_state(self):
        from primer.model.system_state import SystemState

        return SystemState()

    def get_storage(self, cls: type) -> _Store:
        from primer.model.provider import LLMProvider

        if cls is Agent:
            return _Store(self._agents)
        if cls is ModelProfile:
            return _Store(self._profiles)
        if cls is LLMProvider:
            return _Store({p.provider_id: object() for p in self._profiles.values()})
        return _Store({})


def _profile(profile_id: str, provider_id: str, model_name: str) -> ModelProfile:
    return ModelProfile(
        id=profile_id, description="Test profile.", provider_id=provider_id, model_name=model_name, context_length=128_000,
    )


def _agent(agent_id: str, profile_id: str, tools: list[str]) -> Agent:
    return Agent(
        id=agent_id, description="subagent", model=AgentModel(profile_id=profile_id),
        system_prompt=["you are a subagent"], tools=tools,
    )


class _Registry:
    def __init__(self, llms: dict[str, Any], toolsets: dict[str, Any]) -> None:
        self._llms = llms
        self._toolsets = toolsets

    async def get_llm(self, provider_id: str) -> Any:
        return self._llms[provider_id]

    async def get_toolset(self, toolset_id: str) -> Any:
        return self._toolsets[toolset_id]


class _PlainToolset:
    async def list_tools(self, *, principal: str | None = None) -> AsyncIterator[Tool]:
        yield Tool(id="do_it", description="d", toolset_id="t1", args_schema={"type": "object", "properties": {}})

    def is_yielding(self, tool_name: str) -> bool:
        return False

    def required_role(self, tool_name: str) -> str:
        return "admin"

    async def call(self, *, tool_name, arguments, principal=None, ctx=None) -> ToolCallResult:
        return ToolCallResult(output="done", is_error=False)


class _UnresponsiveSubagentLLM:
    """Says something, then blocks; a cancel of that wait is SWALLOWED (an unresponsive subagent), and once released
    it carries on emitting: another delta and a Done, which is what the recorder would turn into records."""

    def __init__(self) -> None:
        self.release = asyncio.Event()
        self.started = asyncio.Event()
        self.cancels_swallowed = 0

    def stream(self, *, model, messages, **kwargs):  # noqa: ANN001
        async def gen():
            yield StreamStart(model="m1")
            yield TextDelta(index=0, text="working on it")
            self.started.set()
            while not self.release.is_set():
                try:
                    await self.release.wait()
                except asyncio.CancelledError:
                    self.cancels_swallowed += 1            # does not unwind
            yield TextDelta(index=0, text="and now it carries on after the Stop")
            yield Done(stop_reason="stop", raw_reason="stop")

        return gen()


class _DelegatingSubagentLLM:
    """A subagent that delegates: its first round asks for the ``t1__spawn`` tool (which runs another subagent); any later
    round just ends."""

    def __init__(self) -> None:
        self.rounds = 0

    def stream(self, *, model, messages, **kwargs):  # noqa: ANN001
        self.rounds += 1
        first = self.rounds == 1

        async def gen():
            yield StreamStart(model="m1")
            if first:
                yield ToolCallStart(id=INNER_CALL_ID, name="t1__spawn", index=0)
                yield ToolCallEnd(id=INNER_CALL_ID, arguments={}, index=0)
                yield Done(stop_reason="tool_use", raw_reason="tool_use")
            else:
                yield TextDelta(index=0, text="subagent A is done")
                yield Done(stop_reason="stop", raw_reason="stop")

        return gen()


class _SpawnToolset(_PlainToolset):
    """The delegating subagent's ``spawn`` tool: it runs ANOTHER real subagent, stamped with its own call id."""

    def __init__(self) -> None:
        self.storage: _Storage | None = None
        self.registry: _Registry | None = None

    async def list_tools(self, *, principal: str | None = None) -> AsyncIterator[Tool]:
        yield Tool(id="spawn", description="d", toolset_id="t1", args_schema={"type": "object", "properties": {}})

    async def call(self, *, tool_name, arguments, principal=None, ctx=None) -> ToolCallResult:
        text = await run_subagent(
            agent_id="agent-b", prompt="do the inner thing", storage_provider=self.storage, provider_registry=self.registry,
            principal="user-1", session_id="sess-parent", workspace_id="ws-parent", chat_id=None,
            invoke_tool_call_id=ctx.tool_call_id, turn_no=1,
        )
        return ToolCallResult(output=text, is_error=False)


# --- the parent turn ---------------------------------------------------------------------------------------------------


class _ParentLLM:
    """One round: call the subagent tool; the turn never gets to a second model call (the Stop ends it)."""

    def stream(self, **_kwargs):
        async def gen():
            yield ToolCallStart(id=CALL_ID, name="invoke_sub", index=0)
            yield ToolCallEnd(id=CALL_ID, arguments={}, index=0)
            yield Done(stop_reason="tool_use", raw_reason="tool_use")

        return gen()


class _ParentManager:
    """The parent's tool manager: its one tool runs the REAL ``run_subagent`` for ``agent_id``, stamped with the call's id."""

    def __init__(self, agent_id: str, storage: _Storage, registry: _Registry) -> None:
        self._agent_id, self._storage, self._registry = agent_id, storage, registry

    def is_notifying(self, tool_name: str) -> bool:
        return False

    def is_interruptible(self, tool_name: str) -> bool:
        return True                                          # invoke_agent is interruptible: cancelling unwinds the subagent

    async def list_tools(self, *, principal=None):
        return []

    async def execute(self, call, *, principal=None) -> ToolResultPart:
        text = await run_subagent(
            agent_id=self._agent_id, prompt="do it", storage_provider=self._storage, provider_registry=self._registry,
            principal="user-1", session_id="sess-parent", workspace_id="ws-parent", chat_id=None,
            invoke_tool_call_id=call.id, turn_no=1,
        )
        return ToolResultPart(id=call.id, output=text, error=False)


class _Writer:
    def __init__(self) -> None:
        self.records: list[Any] = []

    async def append(self, rec) -> int:
        self.records.append(rec)
        return len(self.records)


class _Bus:
    async def publish(self, key, payload) -> None:
        return None


def _world(*, nested: bool):
    """(storage, registry, manager, unresponsive_llm): a parent calling one subagent, or a subagent that delegates to a
    second one (the unresponsive one)."""
    stuck = _UnresponsiveSubagentLLM()
    spawn = _SpawnToolset()
    if nested:
        storage = _Storage(
            agents={
                "agent-a": _agent("agent-a", "prov-1--m1", ["t1__spawn"]),
                "agent-b": _agent("agent-b", "prov-2--m2", ["t1__do_it"]),
            },
            profiles={"prov-1--m1": _profile("prov-1--m1", "prov-1", "m1"), "prov-2--m2": _profile("prov-2--m2", "prov-2", "m2")},
        )
        registry = _Registry(llms={"prov-1": _DelegatingSubagentLLM(), "prov-2": stuck}, toolsets={"t1": spawn})
        spawn.storage, spawn.registry = storage, registry
        agent_id = "agent-a"
    else:
        storage = _Storage(
            agents={"agent-b": _agent("agent-b", "prov-2--m2", ["t1__do_it"])},
            profiles={"prov-2--m2": _profile("prov-2--m2", "prov-2", "m2")},
        )
        registry = _Registry(llms={"prov-2": stuck}, toolsets={"t1": _PlainToolset()})
        agent_id = "agent-b"
    return storage, registry, _ParentManager(agent_id, storage, registry), stuck


async def _stop_a_blocked_subagent(monkeypatch, *, nested: bool, with_the_abandon: bool, how: str = "stop"):
    """Run the parent turn, end it while the (innermost) subagent is blocked, and return what the log held at that point and
    what it held after the unresponsive subagent was finally let go. ``how`` is ``"stop"`` (press Stop: the turn ends
    cleanly with the call answered "interrupted") or ``"cancel"`` (a worker's hard Cancel: the turn task is cancelled and the
    ``CancelledError`` leaves)."""
    monkeypatch.setattr(stoppable_call, "UNWIND_BOUND_S", 0.1)
    monkeypatch.setattr(invoke_module, "current_interrupt", lambda: None)  # a subagent that is NOT given the Stop event
    if not with_the_abandon:
        monkeypatch.setattr(CallScope, "abandon", lambda self: None)       # the control: nobody tells the recorder
    storage, registry, manager, stuck = _world(nested=nested)
    writer = _Writer()
    recorder = DelegationRecorder(writer=writer, event_bus=_Bus(), session_id="sess-parent")
    token = set_delegation_sink(recorder)
    interrupt = asyncio.Event()
    messages_out: list[Message] = []
    interrupted: list[bool] = []

    async def drive() -> None:
        async for _ in run_agent_turn(
            agent=PARENT, llm=_ParentLLM(), llm_model=PARENT_MODEL, tool_manager=manager,
            prompt=[Message(role="user", parts=[TextPart(text="go")])], messages_out=messages_out,
            interrupt=interrupt, interrupted_out=interrupted,
        ):
            pass

    async def press_stop() -> None:
        await asyncio.wait_for(stuck.started.wait(), 3.0)
        interrupt.set()

    try:
        if how == "stop":
            stopper = asyncio.create_task(press_stop())
            await asyncio.wait_for(drive(), 5.0)
            await stopper
        else:
            turn = asyncio.create_task(drive())
            await asyncio.wait_for(stuck.started.wait(), 3.0)
            turn.cancel()                                    # the worker's hard Cancel
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(turn, 5.0)
        at_the_answer = len(writer.records)

        stuck.release.set()                                  # the unresponsive subagent finally carries on
        # An abandoned call may now END CANCELLED (a subagent given the Stop stops at its next check and re-raises the
        # cancel; its own tool call is cancelled through its own ``run_stoppable``), so wait for it without re-raising.
        abandoned = list(stoppable_call._ABANDONED)
        if abandoned:
            _, unfinished = await asyncio.wait(abandoned, timeout=3.0)
            assert not unfinished, f"an abandoned call never finished after the release: {unfinished}"
        for _ in range(5):
            await asyncio.sleep(0)
        late = writer.records[at_the_answer:]
        return stuck, messages_out, interrupted, at_the_answer, len(writer.records), late
    finally:
        stuck.release.set()                                  # a failing run must not leave the stubborn subagent hanging
        reset_delegation_sink(token)


def _tagged(records) -> set[str]:
    return {r.payload["delegate_tool_call_id"] for r in records}


async def test_a_subagent_that_will_not_unwind_is_abandoned_and_records_nothing_after_the_answer(monkeypatch) -> None:
    stuck, messages_out, interrupted, at_the_answer, at_the_end, _ = await _stop_a_blocked_subagent(
        monkeypatch, nested=False, with_the_abandon=True,
    )

    assert stuck.cancels_swallowed >= 1, "the subagent was not cancelled: the test is not in the situation it is about"
    assert interrupted == [True]
    assert [(p.id, p.output, p.error) for m in messages_out for p in m.parts if isinstance(p, ToolResultPart)] == [
        (CALL_ID, INTERRUPTED, True),
    ]
    assert at_the_end == at_the_answer, "an abandoned subagent kept appending to the parent log after the Stop's answer"


async def test_the_same_scenario_without_the_abandon_would_append_after_the_answer(monkeypatch) -> None:
    """The control that makes the test above mean something: with no abandon, what the unresponsive subagent emits once
    it carries on DOES become records, after the answer."""
    _, _, interrupted, at_the_answer, at_the_end, _ = await _stop_a_blocked_subagent(
        monkeypatch, nested=False, with_the_abandon=False,
    )

    assert interrupted == [True]
    assert at_the_end > at_the_answer, "the control produced no late records: the scenario does not exercise the recorder"


async def test_a_nested_subagent_of_an_abandoned_call_records_nothing_after_the_answer(monkeypatch) -> None:
    """Subagent A (called by the parent) delegates to subagent B, which is the one that will not unwind. B's events are
    stamped with the INNER call's id, not the id of the call the Stop abandoned: abandonment has to follow the call's task
    tree, or B (and A, once B returns) keep writing to the parent log after the answer."""
    stuck, _, interrupted, at_the_answer, at_the_end, late = await _stop_a_blocked_subagent(
        monkeypatch, nested=True, with_the_abandon=True,
    )

    assert stuck.cancels_swallowed >= 1, "the inner subagent was not cancelled: the test is not in the situation it is about"
    assert interrupted == [True]
    assert at_the_end == at_the_answer, f"nested subagents kept appending after the Stop's answer: {_tagged(late)}"


async def test_the_nested_control_without_the_abandon_shows_late_records_tagged_with_the_inner_call(monkeypatch) -> None:
    """The control for the nested case: without the abandon the inner subagent's late records ARE appended, tagged with the
    INNER call id (the id an exact-match abandon of the outer call would never have covered)."""
    _, _, _, at_the_answer, at_the_end, late = await _stop_a_blocked_subagent(
        monkeypatch, nested=True, with_the_abandon=False,
    )

    assert at_the_end > at_the_answer
    assert INNER_CALL_ID in _tagged(late), f"no late record carried the inner call id: {_tagged(late)}"


async def test_a_hard_cancel_abandons_a_nested_subagent_too_and_nothing_is_recorded_after_it(monkeypatch) -> None:
    """The production hard-Cancel path (the worker cancels the turn task; no Stop is involved): the call that does not unwind
    within the bound is abandoned through its scope, so neither the subagent nor the one it delegated to appends to the
    parent log after the cancelled turn's ``CancelledError``."""
    stuck, _, interrupted, at_the_end_of_the_turn, at_the_end, late = await _stop_a_blocked_subagent(
        monkeypatch, nested=True, with_the_abandon=True, how="cancel",
    )

    assert stuck.cancels_swallowed >= 1, "the inner subagent was not cancelled: the test is not in the situation it is about"
    assert interrupted == [], "a hard Cancel is not a Stop: the turn must not report an interruption"
    assert at_the_end == at_the_end_of_the_turn, f"an abandoned subagent wrote after the cancelled turn: {_tagged(late)}"


async def test_the_hard_cancel_control_without_the_abandon_shows_late_records(monkeypatch) -> None:
    """The control for the test above: with nobody abandoning, what the unresponsive subagents emit once they carry on IS
    appended after the cancelled turn."""
    _, _, _, at_the_end_of_the_turn, at_the_end, late = await _stop_a_blocked_subagent(
        monkeypatch, nested=True, with_the_abandon=False, how="cancel",
    )

    assert at_the_end > at_the_end_of_the_turn
    assert INNER_CALL_ID in _tagged(late), f"no late record carried the inner call id: {_tagged(late)}"


async def test_the_history_after_a_stopped_subagent_call_is_valid_for_both_providers(monkeypatch) -> None:
    _, messages_out, _, _, _, _ = await _stop_a_blocked_subagent(monkeypatch, nested=True, with_the_abandon=True)
    history = [Message(role="user", parts=[TextPart(text="go")]), *messages_out]

    assert_anthropic_valid(history)
    assert_openai_valid(history)
