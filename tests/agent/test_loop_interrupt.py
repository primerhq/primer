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
from tests._support.provider_history import assert_anthropic_valid, assert_openai_valid

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


class _HungAfterDoneStream:
    """An SDK-style stream object (not an async generator): it delivers one complete tool round and
    then never ends. Cancelling a wait on it does not finish it, so the loop must close it."""

    def __init__(self, events) -> None:
        self._events = list(events)
        self.closed = 0

    def __aiter__(self):
        return self

    async def __anext__(self):
        if self._events:
            return self._events.pop(0)
        await _block()

    async def aclose(self) -> None:
        self.closed += 1


class _OneStreamLLM:
    def __init__(self, stream_obj) -> None:
        self.stream_obj = stream_obj
        self.calls = 0

    def stream(self, **_kwargs):
        self.calls += 1
        return self.stream_obj


class _Manager:
    """Tools run immediately unless ``gate`` is given, then they wait for it. ``on_start`` is called with each
    call as it STARTS (e.g. to set the Stop while that call runs); ``executed`` lists the ids that started."""

    def __init__(self, gate: asyncio.Event | None = None, on_start=None) -> None:
        self.gate = gate
        self.on_start = on_start
        self.started = asyncio.Event()
        self.finished = 0
        self.executed: list[str] = []

    def is_notifying(self, tool_name: str) -> bool:
        return False

    async def list_tools(self, *, principal=None):
        return [TOOL]

    async def execute(self, call, *, principal=None):
        self.started.set()
        self.executed.append(call.id)
        if self.on_start is not None:
            self.on_start(call)
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


class TestARunningToolIsNotInterruptedHere:
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

    async def test_a_stream_that_hangs_after_its_terminal_event_does_not_hold_a_stop(self) -> None:
        """The round is complete (Done and a tool call are in) but the provider never closes the
        stream. A Stop must not wait out the stall timeout (300s by default, then the turn FAILS): the
        stream is closed and the turn ends before the next model call. The round's tool call is NOT
        run (see TestAStopBeforeTheBatchStopsTheBatch)."""
        interrupt = asyncio.Event()
        manager = _Manager()
        llm = _ScriptedLLM(
            [*_tool_round(1), "BLOCK"],
            [TextDelta(text="after", index=0), Done(stop_reason="stop", raw_reason="stop")],
        )

        _, messages_out, interrupted = await _drive(llm, interrupt=interrupt, manager=manager, stop_after=0.1)

        assert manager.finished == 0, "a Stop did not stop the round's tool call"
        assert [m.role for m in messages_out] == ["assistant", "tool"], "the call must still be answered"
        assert interrupted == [True] and llm.calls == 1
        assert llm.closed == 1, "the hung provider stream was left open"

    async def test_a_provider_stream_object_that_hangs_after_done_is_closed_by_the_loop(self) -> None:
        stream = _HungAfterDoneStream(_tool_round(1))
        llm = _OneStreamLLM(stream)
        manager = _Manager()

        _, messages_out, interrupted = await _drive(llm, interrupt=asyncio.Event(), manager=manager, stop_after=0.1)

        assert manager.finished == 0 and [m.role for m in messages_out] == ["assistant", "tool"]
        assert interrupted == [True] and llm.calls == 1
        assert stream.closed == 1, "the hung SDK stream was left open after the Stop"

    async def test_completed_rounds_stay_and_only_the_interrupted_round_is_dropped(self) -> None:
        llm = _ScriptedLLM(_tool_round(1), [TextDelta(text="par", index=0), "BLOCK"])
        events, messages_out, interrupted = await _drive(llm, interrupt=asyncio.Event(), stop_after=0.1)

        assert interrupted == [True]
        assert [m.role for m in messages_out] == ["assistant", "tool"]
        assert [e.text for e in events if isinstance(e, TextDelta)] == ["par"]
        assert llm.calls == 2


STOPPED = "not run: stopped by user"


class _StopsAsTheModelFinishes:
    """Every call asks for ``per_round`` tool calls and sets the Stop right before its Done."""

    def __init__(self, interrupt: asyncio.Event, per_round: int = 1) -> None:
        self.interrupt, self.per_round, self.calls = interrupt, per_round, 0

    def stream(self, **_kwargs):
        self.calls += 1
        n = self.calls

        async def gen():
            for i in range(self.per_round):
                yield ToolCallStart(id=f"tc{n}-{i}", name="loop_tool", index=i)
                yield ToolCallEnd(id=f"tc{n}-{i}", arguments={}, index=i)
            self.interrupt.set()                          # the Stop lands as the model finishes
            yield Done(stop_reason="tool_use", raw_reason="tool_use")

        return gen()


def _tool_results(messages: list[Message]) -> list[ToolResultPart]:
    return [p for m in messages for p in m.parts if isinstance(p, ToolResultPart)]


class TestAStopBeforeTheBatchStopsTheBatch:
    """A Stop pressed on a destructive call must not see it execute. The model already asked, so the
    log holds the tool_use: each call is answered with a synthetic error result and the turn ends, so
    the history stays valid for the next request (an unanswered tool_use is a 400 on Anthropic)."""

    async def test_none_of_the_rounds_tool_calls_run(self) -> None:
        interrupt = asyncio.Event()
        manager = _Manager()
        llm = _StopsAsTheModelFinishes(interrupt, per_round=3)

        _, messages_out, interrupted = await _drive(llm, interrupt=interrupt, manager=manager, stop_after=None)

        assert manager.started.is_set() is False and manager.finished == 0, "a stopped call ran"
        assert interrupted == [True] and llm.calls == 1
        assert [m.role for m in messages_out] == ["assistant", "tool"]

    async def test_every_call_is_answered_with_a_synthetic_error_result_in_one_tool_message(self) -> None:
        interrupt = asyncio.Event()
        llm = _StopsAsTheModelFinishes(interrupt, per_round=3)

        _, messages_out, _ = await _drive(llm, interrupt=interrupt, stop_after=None)

        assert [p.id for p in messages_out[-1].parts] == ["tc1-0", "tc1-1", "tc1-2"]
        assert all(p.error is True and p.output == STOPPED for p in _tool_results(messages_out))

    async def test_the_results_are_yielded_so_the_durable_log_is_paired_too(self) -> None:
        interrupt = asyncio.Event()
        llm = _StopsAsTheModelFinishes(interrupt, per_round=2)

        events, _, _ = await _drive(llm, interrupt=interrupt, stop_after=None)

        results = [e.extended for e in events if isinstance(e, ExtendedEvent) and hasattr(e.extended, "call_id")]
        assert [r.call_id for r in results] == ["tc1-0", "tc1-1"]
        assert all(r.error is True and r.output == STOPPED for r in results)

    async def test_the_persisted_history_is_valid_for_both_providers(self) -> None:
        interrupt = asyncio.Event()
        llm = _StopsAsTheModelFinishes(interrupt, per_round=2)
        _, messages_out, _ = await _drive(llm, interrupt=interrupt, stop_after=None)
        history = [Message(role="user", parts=[TextPart(text="go")]), *messages_out]

        assert_anthropic_valid(history)
        assert_openai_valid(history)

    async def test_a_stop_during_a_tool_is_unchanged_the_running_call_finishes(self) -> None:
        """The boundary that stays slice B's: a call that has already STARTED is not cancelled."""
        gate = asyncio.Event()
        manager = _Manager(gate)
        interrupt = asyncio.Event()
        llm = _ScriptedLLM(_tool_round(1), [TextDelta(text="after", index=0), Done(stop_reason="stop", raw_reason="stop")])

        async def stop_during_the_tool() -> None:
            await manager.started.wait()
            interrupt.set()
            await asyncio.sleep(0.05)
            gate.set()

        asyncio.get_running_loop().create_task(stop_during_the_tool())
        _, messages_out, interrupted = await _drive(llm, interrupt=interrupt, manager=manager, stop_after=None)

        assert manager.finished == 1 and interrupted == [True]
        assert [p.output for p in _tool_results(messages_out)] == ["ok"], "the real result, not a refusal"

    async def test_the_claims_path_is_never_reached_by_a_stopped_round(self) -> None:
        """With tool_calls_as_claims on and a resolver bound, a batch of non-notifying calls is PARKED
        (ToolWaitPark, one claimable task per call). A Stop that landed before the batch must be checked before
        that branch too: no park, no task, no scoped id resolved, the calls answered."""
        interrupt = asyncio.Event()
        resolved: list[str] = []

        def resolver(call_id: str):
            resolved.append(call_id)
            return (f"x:tool:0:{len(resolved)}", len(resolved))

        llm = _StopsAsTheModelFinishes(interrupt, per_round=2)
        messages_out: list[Message] = []
        interrupted: list[bool] = []

        async def drive() -> None:
            async for _ in run_agent_turn(
                agent=AGENT, llm=llm, llm_model=MODEL, tool_manager=_Manager(),
                prompt=[Message(role="user", parts=[TextPart(text="go")])],
                messages_out=messages_out, interrupt=interrupt, interrupted_out=interrupted,
                tool_calls_as_claims_enabled=True, resolve_scoped_call=resolver,
            ):
                pass

        await asyncio.wait_for(drive(), 3.0)   # a ToolWaitPark would be raised out of here

        assert resolved == [], "the claims path resolved scoped ids for a round that was never going to run"
        assert interrupted == [True] and llm.calls == 1
        assert [p.output for p in _tool_results(messages_out)] == [STOPPED, STOPPED]

    async def test_without_a_stop_the_batch_runs_as_before(self) -> None:
        manager = _Manager()
        llm = _ScriptedLLM(_tool_round(1), [TextDelta(text="done", index=0), Done(stop_reason="stop", raw_reason="stop")])

        _, messages_out, interrupted = await _drive(llm, interrupt=asyncio.Event(), manager=manager, stop_after=None)

        assert manager.finished == 1 and interrupted == []
        assert STOPPED not in [p.output for p in _tool_results(messages_out)]


def _calls_round(n: int) -> list[StreamEvent]:
    """One model round that asks for ``n`` tool calls (``tcA-0`` .. ``tcA-{n-1}``)."""
    events: list[StreamEvent] = []
    for i in range(n):
        events += [ToolCallStart(id=f"tcA-{i}", name="loop_tool", index=i), ToolCallEnd(id=f"tcA-{i}", arguments={}, index=i)]
    return [*events, Done(stop_reason="tool_use", raw_reason="tool_use")]


_AFTER = [TextDelta(text="after", index=0), Done(stop_reason="stop", raw_reason="stop")]


class TestAStopDuringACallStopsTheRestOfTheBatch:
    """Calls of a batch run one after another. A Stop that lands while call 1 runs cannot cancel it (slice B), but
    calls 2..N have not started: running a destructive one now would make Stop a lie, exactly as when the Stop lands
    before the batch begins. They are answered ``not run: stopped by user`` instead, and the turn ends."""

    async def _stop_during_the_first_call(self, n_calls: int):
        gate = asyncio.Event()
        interrupt = asyncio.Event()
        manager = _Manager(gate, on_start=lambda call: interrupt.set() if call.id == "tcA-0" else None)
        llm = _ScriptedLLM(_calls_round(n_calls), _AFTER)

        async def release_the_first_call_later() -> None:
            await manager.started.wait()
            await asyncio.sleep(0.05)
            gate.set()

        asyncio.get_running_loop().create_task(release_the_first_call_later())
        events, messages_out, interrupted = await _drive(llm, interrupt=interrupt, manager=manager, stop_after=None)
        return manager, llm, events, messages_out, interrupted

    async def test_the_calls_after_the_running_one_do_not_start(self) -> None:
        manager, _, _, _, _ = await self._stop_during_the_first_call(3)

        assert manager.executed == ["tcA-0"], f"a call started after the Stop: {manager.executed}"
        assert manager.finished == 1

    async def test_the_running_call_keeps_its_real_result_and_the_rest_are_refused_in_order(self) -> None:
        _, _, _, messages_out, _ = await self._stop_during_the_first_call(3)

        assert [(p.id, p.output, p.error) for p in _tool_results(messages_out)] == [
            ("tcA-0", "ok", False), ("tcA-1", STOPPED, True), ("tcA-2", STOPPED, True),
        ]
        assert [m.role for m in messages_out] == ["assistant", "tool"], "one paired, completed round"

    async def test_every_result_is_yielded_so_the_durable_log_is_paired_too(self) -> None:
        _, _, events, _, _ = await self._stop_during_the_first_call(3)

        results = [e.extended for e in events if isinstance(e, ExtendedEvent) and hasattr(e.extended, "call_id")]
        assert [(r.call_id, r.output, r.error) for r in results] == [
            ("tcA-0", "ok", False), ("tcA-1", STOPPED, True), ("tcA-2", STOPPED, True),
        ]

    async def test_the_turn_ends_as_a_stop_before_the_next_model_call(self) -> None:
        _, llm, _, _, interrupted = await self._stop_during_the_first_call(3)

        assert interrupted == [True] and llm.calls == 1

    async def test_the_persisted_history_is_valid_for_both_providers(self) -> None:
        _, _, _, messages_out, _ = await self._stop_during_the_first_call(3)
        history = [Message(role="user", parts=[TextPart(text="go")]), *messages_out]

        assert_anthropic_valid(history)
        assert_openai_valid(history)

    async def test_a_stop_during_the_last_call_has_nothing_left_to_refuse(self) -> None:
        interrupt = asyncio.Event()
        manager = _Manager(on_start=lambda call: interrupt.set() if call.id == "tcA-2" else None)
        llm = _ScriptedLLM(_calls_round(3), _AFTER)

        _, messages_out, interrupted = await _drive(llm, interrupt=interrupt, manager=manager, stop_after=None)

        assert manager.executed == ["tcA-0", "tcA-1", "tcA-2"]
        assert [p.output for p in _tool_results(messages_out)] == ["ok", "ok", "ok"], "no call was refused"
        assert interrupted == [True] and llm.calls == 1

    async def test_without_a_stop_every_call_of_the_batch_runs(self) -> None:
        manager = _Manager()
        llm = _ScriptedLLM(_calls_round(3), _AFTER)

        _, messages_out, interrupted = await _drive(llm, interrupt=asyncio.Event(), manager=manager, stop_after=None)

        assert manager.executed == ["tcA-0", "tcA-1", "tcA-2"] and interrupted == []
        assert STOPPED not in [p.output for p in _tool_results(messages_out)]
