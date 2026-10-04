"""Hitting ``max_tool_turns`` must leave a history a provider will accept.

The loop appends the assistant message (with its tool_use) BEFORE checking the cap, and used to return
at the cap without dispatching it, so ``_run_loop`` persisted a history ending in a tool_use that no
tool_result answers. Anthropic then rejects every later request with a 400 (``tool_use`` ids without
matching ``tool_result`` blocks) and OpenAI chat rejects an assistant ``tool_calls`` that no tool message
follows: the session is wedged, and since it is not an overflow nothing recovers.

The undispatched calls are now answered with a synthetic ERROR tool_result ("not executed: tool-turn cap
reached"). That keeps the model informed (the alternative, dropping the tool_use, would let it believe
it never asked), and the same result is yielded as an event, so the durable log is paired too.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

import pytest

from primer.agent.loop import run_agent_turn
from primer.model.agent import Agent, AgentModel
from primer.model.chat import (
    Done,
    ExtendedEvent,
    Message,
    StreamEvent,
    TextDelta,
    TextPart,
    ToolCallEnd,
    ToolCallPart,
    ToolCallStart,
    ToolResultPart,
    _ExecutorToolResult,
)
from primer.model.except_ import BadRequestError
from primer.model.model_profile import ModelProfileConfig
from primer.model_profile import ResolvedModel
from tests._support.provider_history import assert_anthropic_valid, assert_openai_valid

MODEL = ResolvedModel(
    profile_id="p", provider_id="prov", model_name="m", context_length=4096, config=ModelProfileConfig(),
)
CAP_TEXT = "not executed: tool-turn cap reached"


def _agent(cap: int = 3) -> Agent:
    return Agent(id="ag", description="x", model=AgentModel(profile_id="p--m"), max_tool_turns=cap)


class _AlwaysToolLLM:
    """Every round asks for ``per_round`` tool calls; never a plain stop."""

    def __init__(self, per_round: int = 1) -> None:
        self.per_round = per_round
        self.calls = 0

    def stream(self, **_kwargs) -> AsyncIterator[StreamEvent]:
        self.calls += 1
        n = self.calls

        async def gen() -> AsyncIterator[StreamEvent]:
            for i in range(self.per_round):
                call_id = f"tc{n}-{i}"
                yield ToolCallStart(id=call_id, name="loop_tool", index=i)
                yield ToolCallEnd(id=call_id, arguments={}, index=i)
            yield Done(stop_reason="tool_use", raw_reason="tool_use")

        return gen()


class _Manager:
    def __init__(self) -> None:
        self.executed: list[str] = []

    def is_notifying(self, tool_name: str) -> bool:
        return False

    async def list_tools(self, *, principal=None):
        return []

    async def execute(self, call, *, principal=None):
        self.executed.append(call.id)
        return ToolResultPart(id=call.id, output="ok", error=False)


async def _run_to_the_cap(per_round: int = 1, cap: int = 3):
    llm, manager = _AlwaysToolLLM(per_round), _Manager()
    messages_out: list[Message] = []
    events: list[StreamEvent] = []

    async def drive() -> None:
        async for ev in run_agent_turn(
            agent=_agent(cap), llm=llm, llm_model=MODEL, tool_manager=manager,
            prompt=[Message(role="user", parts=[TextPart(text="go")])], messages_out=messages_out,
        ):
            events.append(ev)

    await asyncio.wait_for(drive(), 5.0)
    return llm, manager, messages_out, events


def _tool_use_ids(messages: list[Message]) -> list[str]:
    return [p.id for m in messages for p in m.parts if isinstance(p, ToolCallPart)]


def _tool_result_ids(messages: list[Message]) -> list[str]:
    return [p.id for m in messages for p in m.parts if isinstance(p, ToolResultPart)]


async def test_every_tool_call_in_the_capped_round_is_answered() -> None:
    llm, manager, messages_out, _ = await _run_to_the_cap()

    assert llm.calls == 3 and manager.executed == ["tc1-0", "tc2-0"], "rounds 1 and 2 ran; round 3 was capped"
    assert sorted(_tool_result_ids(messages_out)) == sorted(_tool_use_ids(messages_out))
    capped = [p for m in messages_out for p in m.parts if isinstance(p, ToolResultPart) and p.id == "tc3-0"]
    assert len(capped) == 1 and capped[0].error is True and capped[0].output == CAP_TEXT


async def test_a_capped_round_with_parallel_calls_answers_all_of_them_in_one_tool_message() -> None:
    _, _, messages_out, _ = await _run_to_the_cap(per_round=3)

    last = messages_out[-1]
    assert last.role == "tool"
    assert [p.id for p in last.parts] == ["tc3-0", "tc3-1", "tc3-2"]
    assert all(p.error and p.output == CAP_TEXT for p in last.parts)


async def test_the_synthetic_results_are_yielded_so_the_durable_log_is_paired_too() -> None:
    _, _, _, events = await _run_to_the_cap()

    results = [
        e.extended for e in events
        if isinstance(e, ExtendedEvent) and isinstance(e.extended, _ExecutorToolResult)
    ]
    assert [r.call_id for r in results] == ["tc1-0", "tc2-0", "tc3-0"]
    assert results[-1].error is True and results[-1].output == CAP_TEXT


async def test_the_persisted_history_is_valid_for_anthropic_and_openai_chat() -> None:
    _, _, messages_out, _ = await _run_to_the_cap(per_round=2)
    history = [Message(role="user", parts=[TextPart(text="go")]), *messages_out]

    assert_anthropic_valid(history)
    assert_openai_valid(history)


class _ValidatingLLM:
    """A provider that does what Anthropic and OpenAI do: it 400s on a history with an unanswered tool call."""

    def __init__(self) -> None:
        self.calls = 0

    def stream(self, *, messages, **_kwargs) -> AsyncIterator[StreamEvent]:
        self.calls += 1
        try:
            assert_anthropic_valid(messages)
            assert_openai_valid(messages)
        except AssertionError as exc:
            raise BadRequestError(f"invalid request: {exc}", status_code=400) from exc

        async def gen() -> AsyncIterator[StreamEvent]:
            yield TextDelta(text="all good", index=0)
            yield Done(stop_reason="stop", raw_reason="stop")

        return gen()


async def test_the_next_turn_after_a_cap_trip_succeeds() -> None:
    _, _, messages_out, _ = await _run_to_the_cap()
    history = [Message(role="user", parts=[TextPart(text="go")]), *messages_out]
    provider = _ValidatingLLM()
    texts: list[str] = []

    async for ev in run_agent_turn(
        agent=_agent(), llm=provider, llm_model=MODEL, tool_manager=_Manager(),
        prompt=[*history, Message(role="user", parts=[TextPart(text="continue please")])],
    ):
        if isinstance(ev, TextDelta):
            texts.append(ev.text)

    assert provider.calls == 1 and "".join(texts) == "all good"


async def test_a_turn_that_does_not_hit_the_cap_is_unchanged() -> None:
    class _TwoThenStop:
        calls = 0

        def stream(self, **_kwargs):
            _TwoThenStop.calls += 1
            n = _TwoThenStop.calls

            async def gen():
                if n == 1:
                    yield ToolCallStart(id="only", name="loop_tool", index=0)
                    yield ToolCallEnd(id="only", arguments={}, index=0)
                    yield Done(stop_reason="tool_use", raw_reason="tool_use")
                else:
                    yield TextDelta(text="done", index=0)
                    yield Done(stop_reason="stop", raw_reason="stop")

            return gen()

    messages_out: list[Message] = []
    async for _ in run_agent_turn(
        agent=_agent(cap=5), llm=_TwoThenStop(), llm_model=MODEL, tool_manager=_Manager(),
        prompt=[Message(role="user", parts=[TextPart(text="go")])], messages_out=messages_out,
    ):
        pass

    assert [m.role for m in messages_out] == ["assistant", "tool", "assistant"]
    assert CAP_TEXT not in [getattr(p, "output", None) for m in messages_out for p in m.parts]


# --- the shared helper, `_answer_undispatched`, which the Stop check and the cap both use -----------------
#
# It does BOTH halves of answering a round the loop will not run, so a caller cannot do one and forget the
# other: the history message (the half that wedges a session if it is missing) and the durable-log events.


def _calls(*ids: str) -> list[ToolCallPart]:
    return [ToolCallPart(id=i, name="loop_tool", arguments={}) for i in ids]


def test_the_helper_appends_one_tool_message_and_yields_one_event_per_call() -> None:
    from primer.agent.loop import _answer_undispatched

    messages_out: list[Message] = []

    events = list(_answer_undispatched(_calls("a", "b", "c"), "refused: because", messages_out))

    assert [m.role for m in messages_out] == ["tool"], "exactly one tool message"
    assert [p.id for p in messages_out[0].parts] == ["a", "b", "c"]
    assert all(p.error is True and p.output == "refused: because" for p in messages_out[0].parts)
    results = [e.extended for e in events if isinstance(e, ExtendedEvent)]
    assert [(r.call_id, r.output, r.error) for r in results] == [
        ("a", "refused: because", True), ("b", "refused: because", True), ("c", "refused: because", True),
    ]


def test_the_helper_appends_the_history_before_it_yields_anything() -> None:
    """Closing the generator after its first event (a consumer that goes away) must not leave the history
    with an unanswered tool_use: the append comes first."""
    from primer.agent.loop import _answer_undispatched

    messages_out: list[Message] = []
    gen = _answer_undispatched(_calls("a", "b"), "refused", messages_out)

    next(gen)
    gen.close()

    assert [p.id for p in messages_out[0].parts] == ["a", "b"], "the whole round was answered in the history"


def test_the_helper_with_no_history_still_yields_the_events() -> None:
    from primer.agent.loop import _answer_undispatched

    events = list(_answer_undispatched(_calls("a"), "refused", None))

    assert [e.extended.call_id for e in events] == ["a"]


async def test_a_stop_and_the_cap_landing_on_the_same_round_are_a_stop() -> None:
    """The Stop check comes first: the round is answered 'not run: stopped by user', the tool never runs and
    the turn reports it was interrupted, rather than being counted as a cap trip."""
    interrupt = asyncio.Event()
    manager, messages_out, interrupted = _Manager(), [], []

    class _StopsAsTheCappedRoundFinishes:
        def stream(self, **_kwargs):
            async def gen() -> AsyncIterator[StreamEvent]:
                yield ToolCallStart(id="tc1-0", name="loop_tool", index=0)
                yield ToolCallEnd(id="tc1-0", arguments={}, index=0)
                interrupt.set()
                yield Done(stop_reason="tool_use", raw_reason="tool_use")

            return gen()

    async def drive() -> None:
        # The cap is 1 and the first round is the capped one: both conditions hold at once.
        async for _ in run_agent_turn(
            agent=_agent(cap=1), llm=_StopsAsTheCappedRoundFinishes(), llm_model=MODEL, tool_manager=manager,
            prompt=[Message(role="user", parts=[TextPart(text="go")])],
            messages_out=messages_out, interrupt=interrupt, interrupted_out=interrupted,
        ):
            pass

    await asyncio.wait_for(drive(), 5.0)

    assert manager.executed == []
    assert interrupted == [True], "a Stop must be reported as one, not swallowed by the cap path"
    assert [p.output for m in messages_out for p in m.parts if isinstance(p, ToolResultPart)] == [
        "not run: stopped by user"
    ]
