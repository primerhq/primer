"""Stop reaches the agent loop's LLM wait: the first token, and between chunks.

Before this, ``run_agent_turn`` could be stopped only between the events its stream yielded, so
a model that had not produced its first token (a cold load has no timeout by default) kept the
turn running however many times the operator pressed Stop. The loop now races the interrupt
event against every ``stream.__anext__()``.

A tool call that is already RUNNING when a Stop lands (slice B1): an interruptible call is CANCELLED and answered
"interrupted: stopped by user ..." (``TestAStopInterruptsARunningCall``); one that is not (a file write) is waited for
and keeps its real result, or is abandoned if it outlasts the grace (``TestANonInterruptibleCallIsWaitedFor``). A call
that finishes first, or in the same wake-up as the Stop, always keeps its real result. The calls of the same batch that
have not STARTED are refused ("not run: stopped by user") instead of running, and the turn stops at the next LLM wait.
``TestAStopDuringACallStopsTheRestOfTheBatch`` and ``TestWhatAStopMeansForTheCallsAfterTheRunningOne`` pin the rest.

What the caller gets back: the loop returns CLEANLY (it does not raise), appends True to
``interrupted_out``, and leaves ``messages_out`` holding only COMPLETED rounds: the interrupted
round's partial assistant text is never appended, so it never reaches the model's history.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

import pytest

import primer.agent.stoppable_call as stoppable_call
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
    ToolCallPart,
    ToolCallStart,
    ToolResultPart,
    _ClientAction,
)
from primer.model.except_ import AuthRequiredError
from primer.model.model_profile import ModelProfileConfig
from primer.model.yield_ import Yielded, YieldToWorker
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

    def __init__(
        self, gate: asyncio.Event | None = None, on_start=None, parks: frozenset[str] = frozenset(),
        notifying: frozenset[str] = frozenset(), park_key_prefix: str = "timer:",
        park_tool_call_ids: dict[str, str] | None = None,
        uninterruptible: frozenset[str] = frozenset(), cleanup_s: float = 0.0,
        auth_required: frozenset[str] = frozenset(),
    ) -> None:
        self.gate = gate
        self.uninterruptible = uninterruptible   # tool NAMES a Stop must not cancel (a file write)
        self.cleanup_s = cleanup_s               # how long a cancelled call takes to unwind (a process-group kill)
        self.auth_required = auth_required       # call ids whose execution raises AuthRequiredError
        self.cancelled = 0                       # calls that were cancelled while blocked on the gate
        self.cleaned = 0                         # ... and whose cleanup then ran to its end
        self.on_start = on_start
        self.parks = parks                       # call ids whose execution parks the session (YieldToWorker)
        # A nested yield (invoke_agent / invoke_graph) re-raises the INNER call's YieldToWorker, so its tool_call_id is
        # the inner call's raw provider id, which restarts every stream and can equal an earlier OUTER id.
        self.park_tool_call_ids = park_tool_call_ids or {}
        self.park_key_prefix = park_key_prefix   # what the parking call waits on: a timer, an approval, an answer
        self.notifying = notifying               # tool NAMES the runner answers itself (client actions)
        self.started = asyncio.Event()
        self.finished = 0
        self.executed: list[str] = []
        self.delivered: list[str] = []

    def is_notifying(self, tool_name: str) -> bool:
        return tool_name in self.notifying

    def is_interruptible(self, tool_name: str) -> bool:
        return tool_name not in self.uninterruptible

    def is_interruptible_call(self, call) -> bool:
        return self.is_interruptible(call.name)

    async def list_tools(self, *, principal=None):
        return [TOOL]

    async def deliver_notifying(self, call, *, principal=None):
        self.delivered.append(call.id)
        if self.on_start is not None:
            self.on_start(call)
        return ToolResultPart(id=call.id, output="delivered", error=False)

    async def execute(self, call, *, principal=None):
        self.started.set()
        self.executed.append(call.id)
        if self.on_start is not None:
            self.on_start(call)
        if call.id in self.parks:
            raise YieldToWorker(
                Yielded(tool_name=call.name, event_key=f"{self.park_key_prefix}{call.id}", resume_metadata={}),
                tool_call_id=self.park_tool_call_ids.get(call.id, call.id),
            )
        if call.id in self.auth_required:
            raise AuthRequiredError("consent needed", auth_url="https://auth.example/x", state="s")
        if self.gate is not None:
            try:
                await self.gate.wait()
            except asyncio.CancelledError:
                self.cancelled += 1
                if self.cleanup_s:
                    await asyncio.sleep(self.cleanup_s)      # the call's own cleanup, e.g. killing a process group
                self.cleaned += 1
                raise
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
    async def test_a_stop_during_a_non_interruptible_tool_waits_for_it_then_stops_at_the_next_llm_wait(self) -> None:
        """A tool that is not interruptible (a file write) is waited for, not cancelled (see
        ``TestAStopInterruptsARunningCall`` for every other tool). The tool finishes, its real result is yielded and
        appended (the log stays paired), and the loop stops BEFORE starting the next model call."""
        gate = asyncio.Event()
        manager = _Manager(gate, uninterruptible=frozenset({"loop_tool"}))
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

    async def test_a_stop_during_a_non_interruptible_tool_lets_the_running_call_finish(self) -> None:
        """A call that has already STARTED and is not interruptible is not cancelled: its real result is recorded."""
        gate = asyncio.Event()
        manager = _Manager(gate, uninterruptible=frozenset({"loop_tool"}))
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
    """Calls of a batch run one after another. A Stop that lands while call 1 runs does not cancel a call that is not
    interruptible (it finishes with its real result), and calls 2..N have not started: running a destructive one now
    would make Stop a lie, exactly as when the Stop lands before the batch begins. They are answered ``not run:
    stopped by user`` instead, and the turn ends. (An interruptible call 1 is cancelled instead:
    ``TestAStopInterruptsARunningCall``.)"""

    async def _stop_during_the_first_call(self, n_calls: int):
        gate = asyncio.Event()
        interrupt = asyncio.Event()
        manager = _Manager(
            gate, on_start=lambda call: interrupt.set() if call.id == "tcA-0" else None,
            uninterruptible=frozenset({"loop_tool"}),
        )
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


class TestAStopInterruptsARunningCall:
    """Stop slice B1: a call that is RUNNING when the Stop lands is cancelled (an exec's process group dies, a subagent
    unwinds), unless the tool says it must not be (see ``TestANonInterruptibleCallIsWaitedFor``). It is answered with the
    same "interrupted" wording a Stop at a park uses (the call may have had effects and its result was not recorded), the
    rest of the batch is refused, and the turn ends as a Stop before the next model call."""

    async def _stop_while_the_first_call_blocks(self, n_calls: int = 1, *, manager: _Manager | None = None):
        interrupt = asyncio.Event()
        manager = manager or _Manager(asyncio.Event())     # a gate nobody opens: the call blocks until it is cancelled
        llm = _ScriptedLLM(_calls_round(n_calls), _AFTER)

        async def stop_during_the_call() -> None:
            await manager.started.wait()
            interrupt.set()

        asyncio.get_running_loop().create_task(stop_during_the_call())
        events, messages_out, interrupted = await _drive(llm, interrupt=interrupt, manager=manager, stop_after=None)
        return manager, llm, events, messages_out, interrupted

    async def test_the_call_is_cancelled_and_answered_as_interrupted(self) -> None:
        manager, llm, _, messages_out, interrupted = await self._stop_while_the_first_call_blocks()

        assert manager.cancelled == 1 and manager.finished == 0
        assert [(p.id, p.output, p.error) for p in _tool_results(messages_out)] == [("tcA-0", PARKED_STOP, True)]
        assert [m.role for m in messages_out] == ["assistant", "tool"], "one paired, completed round"
        assert interrupted == [True] and llm.calls == 1, "the turn did not end as a Stop before the next model call"

    async def test_the_rest_of_the_batch_is_refused_in_order(self) -> None:
        manager, _, _, messages_out, _ = await self._stop_while_the_first_call_blocks(3)

        assert manager.executed == ["tcA-0"], f"a call started after the Stop: {manager.executed}"
        assert [(p.id, p.output, p.error) for p in _tool_results(messages_out)] == [
            ("tcA-0", PARKED_STOP, True), ("tcA-1", STOPPED, True), ("tcA-2", STOPPED, True),
        ]

    async def test_every_result_is_yielded_so_the_durable_log_is_paired_too(self) -> None:
        _, _, events, _, _ = await self._stop_while_the_first_call_blocks(2)

        results = [e.extended for e in events if isinstance(e, ExtendedEvent) and hasattr(e.extended, "call_id")]
        assert [(r.call_id, r.output, r.error) for r in results] == [
            ("tcA-0", PARKED_STOP, True), ("tcA-1", STOPPED, True),
        ]

    async def test_the_persisted_history_is_valid_for_both_providers(self) -> None:
        _, _, _, messages_out, _ = await self._stop_while_the_first_call_blocks(3)
        history = [Message(role="user", parts=[TextPart(text="go")]), *messages_out]

        assert_anthropic_valid(history)
        assert_openai_valid(history)

    async def test_the_calls_own_cleanup_has_run_before_the_answer_is_recorded(self) -> None:
        """What kills an exec's process group runs in the cancelled call's cleanup: the turn must not move on before."""
        manager, _, _, _, _ = await self._stop_while_the_first_call_blocks(manager=_Manager(asyncio.Event(), cleanup_s=0.2))

        assert manager.cancelled == 1 and manager.cleaned == 1

    async def test_a_call_that_finishes_in_the_same_wake_up_as_the_stop_keeps_its_real_result(self) -> None:
        interrupt = asyncio.Event()
        manager = _Manager(on_start=lambda call: interrupt.set())     # no gate: it sets the Stop and returns at once
        llm = _ScriptedLLM(_calls_round(1), _AFTER)

        _, messages_out, interrupted = await _drive(llm, interrupt=interrupt, manager=manager, stop_after=None)

        assert [(p.output, p.error) for p in _tool_results(messages_out)] == [("ok", False)]
        assert interrupted == [True]

    async def test_an_auth_error_raised_after_the_stop_is_answered_as_a_stop(self) -> None:
        """Consistent with a park after a Stop: the consent page is no use to a turn that was stopped."""
        interrupt = asyncio.Event()
        manager = _Manager(on_start=lambda call: interrupt.set(), auth_required=frozenset({"tcA-0"}))
        llm = _ScriptedLLM(_calls_round(2), _AFTER)

        _, messages_out, interrupted = await _drive(llm, interrupt=interrupt, manager=manager, stop_after=None)

        assert [(p.id, p.output) for p in _tool_results(messages_out)] == [("tcA-0", PARKED_STOP), ("tcA-1", STOPPED)]
        assert interrupted == [True]

    async def test_an_auth_error_without_a_stop_still_propagates(self) -> None:
        manager = _Manager(auth_required=frozenset({"tcA-0"}))
        llm = _ScriptedLLM(_calls_round(1), _AFTER)

        with pytest.raises(AuthRequiredError):
            await _drive(llm, interrupt=asyncio.Event(), manager=manager, stop_after=None)

    async def test_a_hard_cancel_of_the_turn_cancels_the_call_and_waits_for_its_cleanup(self) -> None:
        """The worker's Cancel is not a Stop: the CancelledError must never be swallowed, and the call's cleanup (the
        process-group kill) must have run when it leaves the turn."""
        manager = _Manager(asyncio.Event(), cleanup_s=0.2)
        llm = _ScriptedLLM(_calls_round(1), _AFTER)

        async def consume() -> None:
            async for _ in run_agent_turn(
                agent=AGENT, llm=llm, llm_model=MODEL, tool_manager=manager,
                prompt=[Message(role="user", parts=[TextPart(text="go")])], interrupt=asyncio.Event(),
            ):
                pass

        turn = asyncio.create_task(consume())
        await asyncio.wait_for(manager.started.wait(), 3.0)
        turn.cancel()

        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(turn, 3.0)
        assert manager.cancelled == 1 and manager.cleaned == 1, "the cancellation left before the call's cleanup had run"


class TestANonInterruptibleCallIsWaitedFor:
    """A file write is not cancelled (cancelling the await releases the scope lock while its thread still writes): a Stop
    waits for it up to the grace and records the REAL result (``TestARunningToolIsNotInterruptedHere``). Past the grace it
    is abandoned: the loop moves on with the "interrupted" answer and the call runs on to its end on its own."""

    async def test_a_call_that_outlasts_the_grace_is_answered_as_interrupted_and_left_to_finish(self, monkeypatch) -> None:
        monkeypatch.setattr(stoppable_call, "NON_INTERRUPTIBLE_GRACE_S", 0.1)
        gate = asyncio.Event()
        manager = _Manager(gate, uninterruptible=frozenset({"loop_tool"}))
        interrupt = asyncio.Event()
        llm = _ScriptedLLM(_calls_round(1), _AFTER)

        async def stop_during_the_call() -> None:
            await manager.started.wait()
            interrupt.set()

        asyncio.get_running_loop().create_task(stop_during_the_call())
        try:
            _, messages_out, interrupted = await _drive(llm, interrupt=interrupt, manager=manager, stop_after=None)

            assert [(p.id, p.output, p.error) for p in _tool_results(messages_out)] == [("tcA-0", PARKED_STOP, True)]
            assert interrupted == [True]
            assert manager.cancelled == 0, "a call that is not interruptible was cancelled"
            assert len(stoppable_call._ABANDONED) == 1

            gate.set()                              # the write completes after the answer: nothing is torn
            for _ in range(5):
                await asyncio.sleep(0)
            assert manager.finished == 1 and not stoppable_call._ABANDONED, "the abandoned call was not left to finish"
        finally:
            gate.set()                              # a failing run must not leave the abandoned call waiting
            for task in list(stoppable_call._ABANDONED):
                task.cancel()
            stoppable_call._ABANDONED.clear()


def _round_of(*calls: tuple[str, str]) -> list[StreamEvent]:
    """One model round asking for ``(id, tool name)`` calls, in order."""
    events: list[StreamEvent] = []
    for i, (call_id, name) in enumerate(calls):
        events += [ToolCallStart(id=call_id, name=name, index=i), ToolCallEnd(id=call_id, arguments={}, index=i)]
    return [*events, Done(stop_reason="tool_use", raw_reason="tool_use")]


def _client_actions(events: list[StreamEvent]) -> list[str]:
    return [e.extended.call_id for e in events if isinstance(e, ExtendedEvent) and isinstance(e.extended, _ClientAction)]


class TestWhatAStopMeansForTheCallsAfterTheRunningOne:
    """Once the Stop is set, a call that has not started neither parks the session nor tells the browser to act, even
    when it is the kind of call that would. Only the call that is RUNNING when the Stop lands can still ask to park (a
    timer, a ``tool_wait``, an approval or an answer gate); what the loop does with that is
    ``TestAStopThatLandsWhileACallAsksToPark`` below."""

    async def test_a_later_call_that_would_park_is_refused_not_parked(self) -> None:
        interrupt = asyncio.Event()
        manager = _Manager(on_start=lambda c: interrupt.set() if c.id == "a" else None, parks=frozenset({"b"}))
        llm = _ScriptedLLM(_round_of(("a", "loop_tool"), ("b", "loop_tool")), _AFTER)

        _, messages_out, interrupted = await _drive(llm, interrupt=interrupt, manager=manager, stop_after=None)

        assert manager.executed == ["a"], "the call that would park was started"
        assert [(p.id, p.output) for p in _tool_results(messages_out)] == [("a", "ok"), ("b", STOPPED)]
        assert interrupted == [True]

    async def test_a_notifying_call_after_the_stop_emits_no_client_action(self) -> None:
        interrupt = asyncio.Event()
        manager = _Manager(
            on_start=lambda c: interrupt.set() if c.id == "a" else None, notifying=frozenset({"notify_tool"}),
        )
        llm = _ScriptedLLM(_round_of(("a", "loop_tool"), ("n", "notify_tool")), _AFTER)

        events, messages_out, _ = await _drive(llm, interrupt=interrupt, manager=manager, stop_after=None)

        assert _client_actions(events) == [], "the browser was told to act after the Stop"
        assert manager.delivered == []
        assert [(p.id, p.output) for p in _tool_results(messages_out)] == [("a", "ok"), ("n", STOPPED)]

    async def test_a_notifying_call_before_the_stop_still_emits_its_client_action(self) -> None:
        interrupt = asyncio.Event()
        manager = _Manager(
            on_start=lambda c: interrupt.set() if c.id == "a" else None, notifying=frozenset({"notify_tool"}),
        )
        llm = _ScriptedLLM(_round_of(("n", "notify_tool"), ("a", "loop_tool"), ("b", "loop_tool")), _AFTER)

        events, messages_out, _ = await _drive(llm, interrupt=interrupt, manager=manager, stop_after=None)

        assert _client_actions(events) == ["n"]
        assert [(p.id, p.output) for p in _tool_results(messages_out)] == [
            ("n", "delivered"), ("a", "ok"), ("b", STOPPED),
        ]


PARKED_STOP = "interrupted: stopped by user (the call may have run, and its result was not recorded)"


class TestAStopThatLandsWhileACallAsksToPark:
    """The call that is RUNNING when the Stop lands cannot be cancelled here (slice B), and it may then ask the session
    to park (a timer, a trigger, a claimed ``tool_wait`` batch). The park exception used to leave the loop with the Stop
    unseen: the session parked, the console then offered no Stop, and the timer later ran the work the user had tried
    to stop. A park that waits on no human decision is now ended as a Stop instead: every call of the round is answered
    (so the history stays valid), the loop returns cleanly and reports the interruption. A park that asks a PERSON
    (an approval, an answer) still parks: what they answer later wins over the earlier Stop."""

    async def _drive_parking(
        self, calls, parking: str, *, key_prefix: str = "timer:", stop: bool = True,
        notifying: frozenset[str] = frozenset(), inner_ids: dict[str, str] | None = None,
    ):
        interrupt = asyncio.Event()
        manager = _Manager(
            on_start=(lambda c: interrupt.set() if c.id == parking else None) if stop else None,
            parks=frozenset({parking}), park_key_prefix=key_prefix, notifying=notifying, park_tool_call_ids=inner_ids,
        )
        llm = _ScriptedLLM(_round_of(*calls), _AFTER)
        events, messages_out, interrupted = await _drive(llm, interrupt=interrupt, manager=manager, stop_after=None)
        return manager, llm, events, messages_out, interrupted

    async def test_a_timer_park_ends_the_turn_as_a_stop_instead_of_raising(self) -> None:
        manager, llm, _, _, interrupted = await self._drive_parking(
            [("a", "loop_tool"), ("b", "loop_tool")], parking="a",
        )

        assert manager.executed == ["a"]
        assert interrupted == [True] and llm.calls == 1, "the turn did not end as a Stop before the next model call"

    async def test_the_calls_before_the_yielding_one_keep_their_real_results(self) -> None:
        """Each call is answered with what holds for it. The calls before the one that asked to wait FINISHED: their real
        results are still in the dispatch's frame when the park exception leaves it, so they are kept (a delivered
        notifying call, a completed write); only the call that asked to wait may have had effects it never reported, and
        the calls after it never started."""
        manager, _, events, messages_out, _ = await self._drive_parking(
            [("n", "notify_tool"), ("x", "loop_tool"), ("a", "loop_tool"), ("b", "loop_tool")], parking="a",
            notifying=frozenset({"notify_tool"}),
        )

        assert manager.executed == ["x", "a"] and manager.delivered == ["n"]
        assert [(p.id, p.output, p.error) for p in _tool_results(messages_out)] == [
            ("n", "delivered", False), ("x", "ok", False), ("a", PARKED_STOP, True), ("b", STOPPED, True),
        ]
        assert [m.role for m in messages_out] == ["assistant", "tool"], "one paired, completed round"
        assert _client_actions(events) == ["n"], "the delivered notifying call's client action was lost"
        history = [Message(role="user", parts=[TextPart(text="go")]), *messages_out]
        assert_anthropic_valid(history)
        assert_openai_valid(history)

    async def test_a_nested_yield_whose_inner_id_equals_an_earlier_outer_id_is_matched_by_position(self) -> None:
        """invoke_agent / invoke_graph re-raise the INNER call's YieldToWorker, so ``tool_call_id`` is the inner call's
        raw provider id, and raw ids restart every stream ('call_0' again). Matching on that id answered the outer call
        that RAN the whole subagent 'not run' and the earlier call 'may have run'. The yielding call is the one at the
        batch position the dispatch stamped."""
        manager, _, _, messages_out, _ = await self._drive_parking(
            [("call_0", "loop_tool"), ("call_1", "loop_tool")], parking="call_1", inner_ids={"call_1": "call_0"},
        )

        assert manager.executed == ["call_0", "call_1"]
        assert [(p.id, p.output, p.error) for p in _tool_results(messages_out)] == [
            ("call_0", "ok", False), ("call_1", PARKED_STOP, True),
        ]

    async def test_a_real_nested_yield_is_stamped_by_the_outer_batch_last(self) -> None:
        """The outermost loop stamps LAST. Outer round [call_0 finishes, call_1 runs a SUBAGENT, call_2]; the subagent's
        own first call (raw id 'call_0' again) parks. The inner loop stamps its own position first (index 0, nothing
        finished) and the outer batch must overwrite it: keeping the inner stamp would answer the finished call_0
        'may have run' (losing its real result) and the call that ran the whole subagent 'not run'."""
        interrupt = asyncio.Event()
        subagent_manager = _Manager(parks=frozenset({"call_0"}))

        class _RunsASubagent(_Manager):
            async def execute(self, call, *, principal=None):
                if call.id != "call_1":
                    return await super().execute(call, principal=principal)
                self.executed.append(call.id)
                interrupt.set()                          # the Stop lands while the subagent runs
                async for _ in run_agent_turn(           # like run_subagent: its own loop, no Stop event
                    agent=AGENT, llm=_ScriptedLLM(_round_of(("call_0", "loop_tool")), _AFTER), llm_model=MODEL,
                    tool_manager=subagent_manager, prompt=[Message(role="user", parts=[TextPart(text="go")])],
                ):
                    pass
                raise AssertionError("the subagent was expected to park")

        manager = _RunsASubagent()
        llm = _ScriptedLLM(_round_of(("call_0", "loop_tool"), ("call_1", "loop_tool"), ("call_2", "loop_tool")), _AFTER)

        _, messages_out, interrupted = await _drive(llm, interrupt=interrupt, manager=manager, stop_after=None)

        assert manager.executed == ["call_0", "call_1"] and subagent_manager.executed == ["call_0"]
        assert [(p.id, p.output, p.error) for p in _tool_results(messages_out)] == [
            ("call_0", "ok", False), ("call_1", PARKED_STOP, True), ("call_2", STOPPED, True),
        ]
        assert interrupted == [True]

    async def test_a_yield_that_arrives_already_stamped_is_restamped_by_the_outer_batch(self) -> None:
        """The same overwrite with the inner stamp supplied directly: an exception that left an inner batch with
        ``batch_index=0, completed_results=[]`` must not keep them once the outer batch has stamped its own."""
        interrupt = asyncio.Event()

        class _RaisesAStampedYield(_Manager):
            async def execute(self, call, *, principal=None):
                if call.id != "call_1":
                    return await super().execute(call, principal=principal)
                self.executed.append(call.id)
                interrupt.set()
                park = YieldToWorker(Yielded(tool_name=call.name, event_key="timer:inner"), tool_call_id="call_0")
                park.batch_index, park.completed_results = 0, []
                raise park

        manager = _RaisesAStampedYield()
        llm = _ScriptedLLM(_round_of(("call_0", "loop_tool"), ("call_1", "loop_tool"), ("call_2", "loop_tool")), _AFTER)

        _, messages_out, _ = await _drive(llm, interrupt=interrupt, manager=manager, stop_after=None)

        assert [(p.id, p.output, p.error) for p in _tool_results(messages_out)] == [
            ("call_0", "ok", False), ("call_1", PARKED_STOP, True), ("call_2", STOPPED, True),
        ]

    async def test_a_nested_yield_with_a_distinct_inner_id_leaves_the_later_calls_not_run(self) -> None:
        """Nothing matches by id here, and the call after the yielding one still never started."""
        manager, _, _, messages_out, _ = await self._drive_parking(
            [("a", "loop_tool"), ("b", "loop_tool")], parking="a", inner_ids={"a": "inner1"},
        )

        assert manager.executed == ["a"]
        assert [(p.id, p.output) for p in _tool_results(messages_out)] == [("a", PARKED_STOP), ("b", STOPPED)]

    def test_a_park_whose_position_is_unknown_is_answered_may_have_run_for_every_call_never_not_run(self) -> None:
        """No stamp (a yield that did not leave the in-process dispatch): the honest answer for each call is 'may have
        run'. Claiming 'not run' for a call that might have run is the false statement to avoid."""
        from primer.agent.loop import _answer_a_stopped_park

        park = YieldToWorker(Yielded(tool_name="t", event_key="timer:x"), tool_call_id="x")
        calls = [ToolCallPart(id="a", name="loop_tool", arguments={}), ToolCallPart(id="b", name="loop_tool", arguments={})]
        messages_out: list[Message] = []

        list(_answer_a_stopped_park(park, calls, [], messages_out))

        assert [(p.id, p.output) for p in _tool_results(messages_out)] == [("a", PARKED_STOP), ("b", PARKED_STOP)]

    async def test_the_stopped_park_is_handed_to_the_caller(self) -> None:
        """Dispatch needs it (a stopped external_tool park leaves a pending row to cancel); a park that still parks, or no
        Stop at all, hands over nothing."""
        interrupt = asyncio.Event()
        manager = _Manager(on_start=lambda c: interrupt.set(), parks=frozenset({"a"}))
        stopped: list = []

        async def drive(manager, interrupt, stopped_park_out):
            async for _ in run_agent_turn(
                agent=AGENT, llm=_ScriptedLLM(_round_of(("a", "loop_tool")), _AFTER), llm_model=MODEL,
                tool_manager=manager, prompt=[Message(role="user", parts=[TextPart(text="go")])],
                interrupt=interrupt, interrupted_out=[], stopped_park_out=stopped_park_out,
            ):
                pass

        await asyncio.wait_for(drive(manager, interrupt, stopped), 3.0)

        assert len(stopped) == 1 and isinstance(stopped[0], YieldToWorker) and stopped[0].yielded.event_key == "timer:a"

        gated: list = []
        interrupt = asyncio.Event()
        manager = _Manager(
            on_start=lambda c: interrupt.set(), parks=frozenset({"a"}), park_key_prefix="tool_approval:",
        )
        with pytest.raises(YieldToWorker):
            await asyncio.wait_for(drive(manager, interrupt, gated), 3.0)
        assert gated == [], "a park that asks a person is not a stopped park"

    async def test_every_answer_is_yielded_so_the_durable_log_is_paired_too(self) -> None:
        _, _, events, _, _ = await self._drive_parking([("a", "loop_tool"), ("b", "loop_tool")], parking="a")

        results = [e.extended for e in events if isinstance(e, ExtendedEvent) and hasattr(e.extended, "call_id")]
        assert [(r.call_id, r.output, r.error) for r in results] == [("a", PARKED_STOP, True), ("b", STOPPED, True)]

    async def test_the_persisted_history_is_valid_for_both_providers(self) -> None:
        _, _, _, messages_out, _ = await self._drive_parking([("a", "loop_tool"), ("b", "loop_tool")], parking="a")
        history = [Message(role="user", parts=[TextPart(text="go")]), *messages_out]

        assert_anthropic_valid(history)
        assert_openai_valid(history)

    @pytest.mark.parametrize("key_prefix", ["tool_approval:", "ask_user:"])
    async def test_a_park_that_asks_a_person_still_parks(self, key_prefix: str) -> None:
        with pytest.raises(YieldToWorker) as parked:
            await self._drive_parking([("a", "loop_tool")], parking="a", key_prefix=key_prefix)

        assert parked.value.yielded.event_key == f"{key_prefix}a"

    async def test_without_a_stop_a_timer_park_still_parks(self) -> None:
        with pytest.raises(YieldToWorker):
            await self._drive_parking([("a", "loop_tool")], parking="a", stop=False)

    async def test_a_claimed_batch_ends_the_turn_as_a_stop_and_keeps_the_notifying_results(self) -> None:
        """With tool_calls_as_claims on, the batch is parked as ToolWaitPark. A Stop that lands after the loop's check
        (here: while the dispatch barrier is awaited) must not park it either. The notifying call already RAN (its
        client action goes out and its real result is kept); the claimable one never started."""
        interrupt = asyncio.Event()
        resolved: list[str] = []

        def resolver(call_id: str):
            resolved.append(call_id)
            return (f"x:tool:0:{len(resolved)}", len(resolved))

        async def barrier() -> None:
            interrupt.set()

        manager = _Manager(notifying=frozenset({"notify_tool"}))
        llm = _ScriptedLLM(_round_of(("n", "notify_tool"), ("a", "loop_tool")), _AFTER)
        events: list[StreamEvent] = []
        messages_out: list[Message] = []
        interrupted: list[bool] = []

        async def drive() -> None:
            async for ev in run_agent_turn(
                agent=AGENT, llm=llm, llm_model=MODEL, tool_manager=manager,
                prompt=[Message(role="user", parts=[TextPart(text="go")])],
                messages_out=messages_out, interrupt=interrupt, interrupted_out=interrupted,
                tool_calls_as_claims_enabled=True, resolve_scoped_call=resolver, await_dispatch_barrier=barrier,
            ):
                events.append(ev)

        await asyncio.wait_for(drive(), 3.0)   # a ToolWaitPark would be raised out of here

        assert interrupted == [True] and llm.calls == 1
        assert manager.executed == [], "the claimable call was started in-process"
        assert _client_actions(events) == ["n"], "the delivered notifying call's client action was lost"
        assert [(p.id, p.output, p.error) for p in _tool_results(messages_out)] == [
            ("n", "delivered", False), ("a", STOPPED, True),
        ]
        history = [Message(role="user", parts=[TextPart(text="go")]), *messages_out]
        assert_anthropic_valid(history)
        assert_openai_valid(history)

    def test_the_loop_and_the_turn_log_agree_on_what_a_human_gate_is(self) -> None:
        """One table (``YIELD_KIND_PREFIXES``): the loop's Stop handling and dispatch's yield-kind classifier."""
        from primer.model.yield_ import YIELD_KIND_PREFIXES, asks_a_person
        from primer.session.dispatch import _classify_yield_kind

        def park(key: str) -> YieldToWorker:
            return YieldToWorker(Yielded(tool_name="t", event_key=key), tool_call_id="c")

        for prefix, kind in YIELD_KIND_PREFIXES:
            assert asks_a_person(park(f"{prefix}x").yielded) is True
            assert _classify_yield_kind(park(f"{prefix}x")) == kind
        # The producers' own keys: timers, triggers, MCP tasks, watches, an invoker's external tool (the invoking system
        # answers it, not the session's user: ruled a no-person park), an events wait, and a claimed tool_wait batch.
        for key in ("timer:t1", "trigger:abc", "mcp_task:1", "watch:w", "external_tool:s:c", "evwait:s:c", "tool_wait:x"):
            assert asks_a_person(park(key).yielded) is False
            assert _classify_yield_kind(park(key)) == "subscribe_to_trigger"
