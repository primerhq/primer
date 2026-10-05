"""A Stop that gives up on a subagent call must not let it append to the parent log afterwards (stop slice B1).

``invoke_agent`` runs a REAL subagent inside the delegating turn and feeds every event it emits to the turn's
``DelegationRecorder``, stamped with the parent call's id. When a Stop cancels the call but the subagent does not
unwind in time (here: it swallows the cancel and carries on), the loop abandons the call and tells the recorder to drop
what it still emits, BEFORE the call's synthetic "interrupted" result is recorded. Otherwise the parent log would show
the subagent continuing after the Stop's answer.

These tests drive the real ``run_subagent`` (through a real ``ToolExecutionManager`` built internally from tiny fakes, as
``tests/agent/test_run_subagent_yield.py`` does), the real ``run_agent_turn`` loop and the real ``DelegationRecorder``.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any

import pytest

import primer.agent.loop as loop_module
import primer.agent.stoppable_call as stoppable_call
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
INTERRUPTED = "interrupted: stopped by user (the call may have run, and its result was not recorded)"


# --- the subagent's world: a toolset, storage, a provider registry, an LLM that will not stop -------------------------


class _Toolset:
    async def list_tools(self, *, principal: str | None = None) -> AsyncIterator[Tool]:
        yield Tool(id="do_it", description="d", toolset_id="t1", args_schema={"type": "object", "properties": {}})

    def is_yielding(self, tool_name: str) -> bool:
        return False

    def required_role(self, tool_name: str) -> str:
        return "admin"

    async def call(self, *, tool_name, arguments, principal=None, ctx=None) -> ToolCallResult:
        return ToolCallResult(output="done", is_error=False)


class _Store:
    def __init__(self, obj: Any) -> None:
        self._obj = obj

    async def get(self, _id: str) -> Any:
        return self._obj


class _StorageProvider:
    def __init__(self, agent: Agent) -> None:
        self._agent = agent
        self._profile = ModelProfile(
            id="prov-1--m1", description="Test profile.", provider_id="prov-1", model_name="m1", context_length=128_000,
        )

    async def get_system_state(self):
        from primer.model.system_state import SystemState

        return SystemState()

    def get_storage(self, cls: type) -> _Store:
        from primer.model.provider import LLMProvider

        if cls is Agent:
            return _Store(self._agent)
        if cls is ModelProfile:
            return _Store(self._profile)
        if cls is LLMProvider:
            return _Store(object())
        return _Store(None)


class _Registry:
    def __init__(self, llm: Any) -> None:
        self._llm = llm

    async def get_llm(self, _provider_id: str) -> Any:
        return self._llm

    async def get_toolset(self, _toolset_id: str) -> _Toolset:
        return _Toolset()


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
    """The parent's tool manager: its one tool runs the REAL ``run_subagent``, stamped with the call's own id."""

    def __init__(self, sub_llm: _UnresponsiveSubagentLLM) -> None:
        self._sub_llm = sub_llm

    def is_notifying(self, tool_name: str) -> bool:
        return False

    def is_interruptible(self, tool_name: str) -> bool:
        return True                                          # invoke_agent is interruptible: cancelling unwinds the subagent

    async def list_tools(self, *, principal=None):
        return []

    async def execute(self, call, *, principal=None) -> ToolResultPart:
        text = await run_subagent(
            agent_id="agent-sub", prompt="do it", storage_provider=_StorageProvider(_sub_agent()),
            provider_registry=_Registry(self._sub_llm), principal="user-1", session_id="sess-parent",
            workspace_id="ws-parent", chat_id=None, invoke_tool_call_id=call.id, turn_no=1,
        )
        return ToolResultPart(id=call.id, output=text, error=False)


def _sub_agent() -> Agent:
    return Agent(
        id="agent-sub", description="subagent", model=AgentModel(profile_id="prov-1--m1"),
        system_prompt=["you are a subagent"], tools=["t1__do_it"],
    )


class _Writer:
    def __init__(self) -> None:
        self.records: list[Any] = []

    async def append(self, rec) -> int:
        self.records.append(rec)
        return len(self.records)


class _Bus:
    async def publish(self, key, payload) -> None:
        return None


async def _stop_a_blocked_subagent(monkeypatch, *, with_the_abandon_hook: bool):
    """Run the parent turn, press Stop while the subagent is blocked, and return what the log held at the answer and what
    it held after the unresponsive subagent was finally let go."""
    monkeypatch.setattr(stoppable_call, "UNWIND_BOUND_S", 0.1)
    if not with_the_abandon_hook:
        monkeypatch.setattr(loop_module, "_abandon_hook", lambda call: None)
    writer = _Writer()
    recorder = DelegationRecorder(writer=writer, event_bus=_Bus(), session_id="sess-parent")
    token = set_delegation_sink(recorder)
    sub_llm = _UnresponsiveSubagentLLM()
    interrupt = asyncio.Event()
    messages_out: list[Message] = []
    interrupted: list[bool] = []

    async def drive() -> None:
        async for _ in run_agent_turn(
            agent=PARENT, llm=_ParentLLM(), llm_model=PARENT_MODEL, tool_manager=_ParentManager(sub_llm),
            prompt=[Message(role="user", parts=[TextPart(text="go")])], messages_out=messages_out,
            interrupt=interrupt, interrupted_out=interrupted,
        ):
            pass

    async def press_stop() -> None:
        await asyncio.wait_for(sub_llm.started.wait(), 3.0)
        interrupt.set()

    try:
        stopper = asyncio.create_task(press_stop())
        await asyncio.wait_for(drive(), 5.0)
        await stopper
        at_the_answer = len(writer.records)

        sub_llm.release.set()                                # the unresponsive subagent finally carries on
        for task in list(stoppable_call._ABANDONED):
            await asyncio.wait_for(asyncio.shield(task), 3.0)
        for _ in range(5):
            await asyncio.sleep(0)
        return sub_llm, messages_out, interrupted, at_the_answer, len(writer.records)
    finally:
        reset_delegation_sink(token)


async def test_a_subagent_that_will_not_unwind_is_abandoned_and_records_nothing_after_the_answer(monkeypatch) -> None:
    sub_llm, messages_out, interrupted, at_the_answer, at_the_end = await _stop_a_blocked_subagent(
        monkeypatch, with_the_abandon_hook=True,
    )

    assert sub_llm.cancels_swallowed >= 1, "the subagent was not cancelled: the test is not in the situation it is about"
    assert interrupted == [True]
    assert [(p.id, p.output, p.error) for m in messages_out for p in m.parts if isinstance(p, ToolResultPart)] == [
        (CALL_ID, INTERRUPTED, True),
    ]
    assert at_the_end == at_the_answer, "an abandoned subagent kept appending to the parent log after the Stop's answer"


async def test_the_same_scenario_without_the_abandon_would_append_after_the_answer(monkeypatch) -> None:
    """The control that makes the test above mean something: with no abandon, what the unresponsive subagent emits once
    it carries on DOES become records, after the answer."""
    _, _, interrupted, at_the_answer, at_the_end = await _stop_a_blocked_subagent(monkeypatch, with_the_abandon_hook=False)

    assert interrupted == [True]
    assert at_the_end > at_the_answer, "the control produced no late records: the scenario does not exercise the recorder"


async def test_the_history_after_a_stopped_subagent_call_is_valid_for_both_providers(monkeypatch) -> None:
    _, messages_out, _, _, _ = await _stop_a_blocked_subagent(monkeypatch, with_the_abandon_hook=True)
    history = [Message(role="user", parts=[TextPart(text="go")]), *messages_out]

    assert_anthropic_valid(history)
    assert_openai_valid(history)
