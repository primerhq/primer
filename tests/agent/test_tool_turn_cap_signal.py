"""A tool-turn cap trip is SIGNALLED out of the loop and the executor, not left looking like "the model wants a tool".

When ``max_tool_turns`` trips, the model's last event was ``Done(stop_reason="tool_use")``, so everything downstream
read the turn as "a tool round the executor itself will continue": the dispatch mapped ``tool_use`` to RUNNING and
released the lease, leaving a RUNNING row nothing re-arms. Boot recovery re-arms every RUNNING row, so after the cap
fix made the persisted history valid, a restart resumed the model with NO user input, ran up to ``max_tool_turns - 1``
more (possibly destructive) rounds, tripped the cap again and rested RUNNING again, on every restart.

``run_agent_turn`` now reports the trip through ``capped_out`` (the same output-parameter shape as ``interrupted_out``),
and ``WorkspaceAgentExecutor`` publishes it as ``last_done_reason == "tool_turn_cap"``.
"""

from __future__ import annotations

import asyncio
import shutil
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from primer.agent.loop import run_agent_turn
from primer.agent.tool_manager import ToolExecutionManager
from primer.agent.workspace_executor import WorkspaceAgentExecutor
from primer.model.agent import Agent, AgentModel
from primer.model.chat import (
    Done,
    Message,
    StreamEvent,
    TextDelta,
    TextPart,
    ToolCallEnd,
    ToolCallStart,
    ToolResultPart,
)
from primer.model.model_profile import ModelProfileConfig
from primer.model.workspace_session import SessionStatus
from primer.model_profile import ResolvedModel
from tests.agent.test_workspace_executor import _build_session, _drain, _model

MODEL = ResolvedModel(
    profile_id="p", provider_id="prov", model_name="m", context_length=4096, config=ModelProfileConfig(),
)


def _agent(cap: int) -> Agent:
    return Agent(id="researcher", description="x", model=AgentModel(profile_id="p--m"), max_tool_turns=cap)


class _AlwaysToolLLM:
    """Every call asks for one tool call; never a plain stop."""

    def __init__(self) -> None:
        self.calls = 0

    def stream(self, **_kwargs) -> AsyncIterator[StreamEvent]:
        self.calls += 1
        n = self.calls

        async def gen() -> AsyncIterator[StreamEvent]:
            yield ToolCallStart(id=f"tc{n}", name="loop_tool", index=0)
            yield ToolCallEnd(id=f"tc{n}", arguments={}, index=0)
            yield Done(stop_reason="tool_use", raw_reason="tool_use")

        return gen()

    async def list_models(self):
        return ["m"]


class _Manager:
    def is_notifying(self, tool_name: str) -> bool:
        return False

    async def list_tools(self, *, principal=None):
        return []

    async def execute(self, call, *, principal=None):
        return ToolResultPart(id=call.id, output="ok", error=False)


async def _loop(llm, *, cap: int, interrupt: asyncio.Event | None = None):
    capped: list[bool] = []
    interrupted: list[bool] = []
    async for _ in run_agent_turn(
        agent=_agent(cap), llm=llm, llm_model=MODEL, tool_manager=_Manager(),
        prompt=[Message(role="user", parts=[TextPart(text="go")])],
        interrupt=interrupt, interrupted_out=interrupted, capped_out=capped,
    ):
        pass
    return capped, interrupted


async def test_the_loop_reports_a_cap_trip() -> None:
    capped, interrupted = await asyncio.wait_for(_loop(_AlwaysToolLLM(), cap=3), 5.0)

    assert capped == [True] and interrupted == []


async def test_the_loop_does_not_report_a_turn_that_stops_before_the_cap() -> None:
    class _OneToolThenStop(_AlwaysToolLLM):
        def stream(self, **kwargs):
            if self.calls == 0:
                return super().stream(**kwargs)
            self.calls += 1

            async def gen() -> AsyncIterator[StreamEvent]:
                yield TextDelta(text="done", index=0)
                yield Done(stop_reason="stop", raw_reason="stop")

            return gen()

    capped, interrupted = await asyncio.wait_for(_loop(_OneToolThenStop(), cap=10), 5.0)

    assert capped == [] and interrupted == []


async def test_a_stop_that_lands_on_the_capped_round_is_a_stop_not_a_cap_trip() -> None:
    interrupt = asyncio.Event()

    class _SetsTheStopBeforeDone(_AlwaysToolLLM):
        def stream(self, **kwargs):
            inner = super().stream(**kwargs)

            async def gen() -> AsyncIterator[StreamEvent]:
                async for ev in inner:
                    if isinstance(ev, Done):
                        interrupt.set()
                    yield ev

            return gen()

    capped, interrupted = await asyncio.wait_for(_loop(_SetsTheStopBeforeDone(), cap=1, interrupt=interrupt), 5.0)

    assert interrupted == [True] and capped == []


pytestmark_git = pytest.mark.skipif(shutil.which("git") is None, reason="git CLI not available on PATH")


@pytestmark_git
async def test_the_workspace_executor_publishes_a_cap_trip_as_its_last_done_reason(tmp_path: Path) -> None:
    backend, _, session = await _build_session(tmp_path)
    try:
        mgr = ToolExecutionManager.for_workspace(toolset_providers={}, session=session)
        executor = WorkspaceAgentExecutor(
            agent=_agent(2), llm=_AlwaysToolLLM(),  # type: ignore[arg-type]
            llm_model=_model(), tool_manager=mgr, session=session,
        )

        await _drain(executor.invoke([Message(role="user", parts=[TextPart(text="hi")])]))

        assert executor.last_done_reason == "tool_turn_cap"
    finally:
        await session.aclose()
        await backend.aclose()


@pytestmark_git
async def test_a_cap_trip_is_not_read_as_a_question_to_the_user(tmp_path: Path) -> None:
    """The executor treats a final assistant text that ends in a question mark as "waiting for the user". A capped
    turn's last text can end in one too ("Should I keep going?"), and it must not be read that way: the status
    mapper, not that heuristic, decides what a cap trip rests as."""

    class _AsksWhileCallingTools(_AlwaysToolLLM):
        def stream(self, **kwargs):
            inner = super().stream(**kwargs)

            async def gen() -> AsyncIterator[StreamEvent]:
                yield TextDelta(text="Should I keep going?", index=0)
                async for ev in inner:
                    yield ev

            return gen()

    backend, _, session = await _build_session(tmp_path)
    try:
        mgr = ToolExecutionManager.for_workspace(toolset_providers={}, session=session)
        executor = WorkspaceAgentExecutor(
            agent=_agent(2), llm=_AsksWhileCallingTools(),  # type: ignore[arg-type]
            llm_model=_model(), tool_manager=mgr, session=session,
        )

        await _drain(executor.invoke([Message(role="user", parts=[TextPart(text="hi")])]))

        assert executor.last_done_reason == "tool_turn_cap"
        assert await session.status() != SessionStatus.WAITING, "the cap trip was read as a question to the user"
    finally:
        await session.aclose()
        await backend.aclose()


@pytestmark_git
async def test_the_workspace_executor_still_reports_a_clean_stop_as_one(tmp_path: Path) -> None:
    class _Stops:
        def stream(self, **_kwargs):
            async def gen() -> AsyncIterator[StreamEvent]:
                yield TextDelta(text="all done", index=0)
                yield Done(stop_reason="stop", raw_reason="stop")

            return gen()

        async def list_models(self):
            return ["m"]

    backend, _, session = await _build_session(tmp_path)
    try:
        mgr = ToolExecutionManager.for_workspace(toolset_providers={}, session=session)
        executor = WorkspaceAgentExecutor(
            agent=_agent(2), llm=_Stops(),  # type: ignore[arg-type]
            llm_model=_model(), tool_manager=mgr, session=session,
        )

        await _drain(executor.invoke([Message(role="user", parts=[TextPart(text="hi")])]))

        assert executor.last_done_reason == "stop"
    finally:
        await session.aclose()
        await backend.aclose()
