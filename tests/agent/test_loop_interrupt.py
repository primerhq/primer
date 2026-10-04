"""Stop reaches the agent loop's LLM wait: the first token, and between chunks.

Before this, ``run_agent_turn`` could be stopped only between the events its stream yielded, so
a model that had not produced its first token (a cold load has no timeout by default) kept the
turn running however many times the operator pressed Stop. The loop now races the interrupt
event against every ``stream.__anext__()``.

The TOOL BATCH is deliberately not interruptible here (slice B): a Stop that lands while a tool
runs lets the batch finish, yields its results (so the log stays paired), and stops at the next
LLM wait. ``test_a_stop_during_a_tool_waits_for_the_tool_...`` pins that boundary so slice B
changes it knowingly.

What the caller gets back: the loop returns CLEANLY (it does not raise), appends True to
``interrupted_out``, and leaves ``messages_out`` holding only COMPLETED rounds: the interrupted
round's partial assistant text is never appended, so it never reaches the model's history.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

import pytest

import primer.observability.metrics as metrics
from primer.agent.loop import run_agent_turn
from primer.model.agent import Agent, AgentModel
from primer.model.chat import (
    Done,
    ExtendedEvent,
    Message,
    StreamEvent,
    TextDelta,
    TextPart,
    Tool,
    ToolCallEnd,
    ToolCallStart,
    ToolResultPart,
)
from primer.model.model_profile import ModelProfileConfig
from primer.model_profile import ResolvedModel

MODEL = ResolvedModel(
    profile_id="p", provider_id="prov", model_name="m", context_length=4096,
    config=ModelProfileConfig(),
)
AGENT = Agent(id="ag", description="x", model=AgentModel(profile_id="p--m"), max_tool_turns=10)
TOOL = Tool(id="loop_tool", description="d", toolset_id="t",
            args_schema={"type": "object", "properties": {}})


@pytest.fixture(autouse=True)
def _reset_metrics():
    metrics.reset_for_test()
    yield
    metrics.reset_for_test()


async def _block() -> None:
    await asyncio.Event().wait()


def _tool_round(n: int) -> list[StreamEvent]:
    return [
        ToolCallStart(id=f"tc{n}", name="loop_tool", index=0),
        ToolCallEnd(id=f"tc{n}", arguments={}, index=0),
        Done(stop_reason="tool_use", raw_reason="tool_use"),
    ]


class _ScriptedLLM:
    """One script per ``stream()`` call. A script is a list of events, optionally ending in
    ``"BLOCK"`` (the stream then waits forever: a cold model load, or a stalled provider)."""

    def __init__(self, *scripts: list) -> None:
        self.scripts = list(scripts)
        self.calls = 0
        self.closed = 0

    def stream(self, **_kwargs) -> AsyncIterator[StreamEvent]:
        script = self.scripts[self.calls]
        self.calls += 1

        async def gen() -> AsyncIterator[StreamEvent]:
            try:
                for item in script:
                    if item == "BLOCK":
                        await _block()
                    else:
                        yield item
            finally:
                self.closed += 1

        return gen()


class _ClassStream:
    """A provider stream that is NOT an async generator (an SDK stream object): cancelling a wait on
    it does not finish it, so the loop itself must close it."""

    def __init__(self) -> None:
        self.closed = 0

    def __aiter__(self):
        return self

    async def __anext__(self):
        await _block()

    async def aclose(self) -> None:
        self.closed += 1


class _ClassStreamLLM:
    def __init__(self) -> None:
        self.stream_obj = _ClassStream()

    def stream(self, **_kwargs):
        return self.stream_obj


class _Manager:
    """Tools run immediately unless ``gate`` is given, then they wait for it."""

    def __init__(self, gate: asyncio.Event | None = None) -> None:
        self.gate = gate
        self.started = asyncio.Event()
        self.finished = 0

    def is_notifying(self, tool_name: str) -> bool:
        return False

    async def list_tools(self, *, principal=None):
        return [TOOL]

    async def execute(self, call, *, principal=None):
        self.started.set()
        if self.gate is not None:
            await self.gate.wait()
        self.finished += 1
        return ToolResultPart(id=call.id, output="ok", error=False)


async def _drive(llm, *, interrupt, manager=None, stop_after: float | None = 0.05):
    """Run one turn; set ``interrupt`` after ``stop_after`` seconds (None: the caller does)."""
    events: list[StreamEvent] = []
    messages_out: list[Message] = []
    interrupted_out: list[bool] = []
    if stop_after is not None:
        asyncio.get_running_loop().call_later(stop_after, interrupt.set)

    async def consume() -> None:
        async for ev in run_agent_turn(
            agent=AGENT, llm=llm, llm_model=MODEL, tool_manager=manager or _Manager(),
            prompt=[Message(role="user", parts=[TextPart(text="go")])],
            messages_out=messages_out, interrupt=interrupt, interrupted_out=interrupted_out,
        ):
            events.append(ev)

    await asyncio.wait_for(consume(), 3.0)
    return events, messages_out, interrupted_out


class TestTheLlmWait:
    async def test_a_stop_before_the_first_token_ends_the_turn_cleanly(self) -> None:
        llm = _ScriptedLLM(["BLOCK"])
        events, messages_out, interrupted = await _drive(llm, interrupt=asyncio.Event())

        assert interrupted == [True]
        assert events == [] and messages_out == []
        assert llm.closed == 1, "the provider stream was left open"

    async def test_a_stop_between_chunks_keeps_what_was_streamed_and_drops_it_from_history(self) -> None:
        llm = _ScriptedLLM([TextDelta(text="he", index=0), "BLOCK"])
        events, messages_out, interrupted = await _drive(llm, interrupt=asyncio.Event())

        assert interrupted == [True]
        assert [e.text for e in events if isinstance(e, TextDelta)] == ["he"], "the live view had it"
        assert messages_out == [], "a partial assistant message must not reach the model's history"

    async def test_a_provider_stream_that_is_not_a_generator_is_closed_by_the_loop(self) -> None:
        llm = _ClassStreamLLM()

        _, _, interrupted = await _drive(llm, interrupt=asyncio.Event())

        assert interrupted == [True]
        assert llm.stream_obj.closed == 1, "an SDK stream object was left open after the Stop"

    async def test_an_event_already_set_never_starts_a_model_call(self) -> None:
        event = asyncio.Event()
        event.set()
        llm = _ScriptedLLM([TextDelta(text="never", index=0)])

        events, _, interrupted = await _drive(llm, interrupt=event, stop_after=None)

        assert interrupted == [True]
        assert llm.calls == 0 and events == []

    async def test_a_stall_timeout_from_the_stream_is_still_an_error_not_a_stop(self) -> None:
        class _Stalling:
            def stream(self, **_kwargs):
                async def gen():
                    raise TimeoutError("no event within the stall window")
                    yield  # pragma: no cover

                return gen()

        interrupted: list[bool] = []
        with pytest.raises(TimeoutError):
            async for _ in run_agent_turn(
                agent=AGENT, llm=_Stalling(), llm_model=MODEL, tool_manager=_Manager(),
                prompt=[Message(role="user", parts=[TextPart(text="go")])],
                interrupt=asyncio.Event(), interrupted_out=interrupted,
            ):
                pass

        assert interrupted == []
        assert metrics.llm_calls_total.labels("prov", "p", "error")._value.get() == 1.0

    async def test_an_interrupted_call_is_counted_as_interrupted_not_lost_or_ok(self) -> None:
        await _drive(_ScriptedLLM(["BLOCK"]), interrupt=asyncio.Event())

        assert metrics.llm_calls_total.labels("prov", "p", "interrupted")._value.get() == 1.0
        assert metrics.llm_calls_total.labels("prov", "p", "ok")._value.get() == 0.0


class TestTheToolBatchIsNotInterruptedHere:
    async def test_a_stop_during_a_tool_waits_for_the_tool_then_stops_at_the_next_llm_wait(self) -> None:
        """The boundary slice B owns. The tool finishes, its result is yielded and appended (the log
        stays paired), and the loop stops BEFORE starting the next model call."""
        gate = asyncio.Event()
        manager = _Manager(gate)
        llm = _ScriptedLLM(_tool_round(1), [TextDelta(text="after", index=0), Done(stop_reason="stop", raw_reason="stop")])
        interrupt = asyncio.Event()

        async def stop_during_the_tool() -> None:
            await manager.started.wait()
            interrupt.set()
            await asyncio.sleep(0.05)
            assert manager.finished == 0, "the tool was cancelled instead of finishing"
            gate.set()

        asyncio.get_running_loop().create_task(stop_during_the_tool())
        events, messages_out, interrupted = await _drive(
            llm, interrupt=interrupt, manager=manager, stop_after=None,
        )

        assert manager.finished == 1
        assert interrupted == [True]
        assert llm.calls == 1, "a second model call was started after the Stop"
        assert [m.role for m in messages_out] == ["assistant", "tool"], "a paired, completed round"
        assert any(isinstance(e, ExtendedEvent) for e in events), "the tool result was yielded"

    async def test_a_stop_that_lands_before_the_tools_are_dispatched_still_lets_them_run(self) -> None:
        """The model already asked for the call and the log already holds the tool_use: the batch is
        run and answered (slice B decides whether a Stop may cancel it), then the turn stops."""
        interrupt = asyncio.Event()
        manager = _Manager()

        class _StopsWhileAnswering:
            calls = 0

            def stream(self, **_kwargs):
                self.calls += 1

                async def gen():
                    yield ToolCallStart(id="tc1", name="loop_tool", index=0)
                    yield ToolCallEnd(id="tc1", arguments={}, index=0)
                    interrupt.set()                       # the Stop lands as the model finishes
                    yield Done(stop_reason="tool_use", raw_reason="tool_use")

                return gen()

        llm = _StopsWhileAnswering()
        _, messages_out, interrupted = await _drive(llm, interrupt=interrupt, manager=manager, stop_after=None)

        assert manager.finished == 1, "the requested tool call was dropped"
        assert [m.role for m in messages_out] == ["assistant", "tool"]
        assert interrupted == [True] and llm.calls == 1

    async def test_completed_rounds_stay_and_only_the_interrupted_round_is_dropped(self) -> None:
        llm = _ScriptedLLM(_tool_round(1), [TextDelta(text="par", index=0), "BLOCK"])
        events, messages_out, interrupted = await _drive(llm, interrupt=asyncio.Event(), stop_after=0.1)

        assert interrupted == [True]
        assert [m.role for m in messages_out] == ["assistant", "tool"]
        assert [e.text for e in events if isinstance(e, TextDelta)] == ["par"]
        assert llm.calls == 2
