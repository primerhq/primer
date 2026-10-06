"""The record of a turn that stopped at ``max_tool_turns`` says so (ticket 01a1095e, sub-point 2).

The capped round's terminal event was the MODEL's own ``Done(stop_reason="tool_use")``: the loop yielded it before it
knew the round would cap, so the durable ``done`` record was the one a mid-chain tool round writes. Nothing in the log
said "capped", the console hid it (a ``tool_use`` done is a tool round, not a turn), and ``closes_turn`` did not count it
as a turn boundary, so the turn's window ran on into the NEXT turn and every later turn's ordinal pointed at another
turn's envelope.

The loop now yields ``Done(stop_reason="tool_turn_cap", raw_reason=<the model's own>)`` for the capped round, decided
before the Done is delivered and honoured after it (a Stop that lands while the consumer holds the Done does not
contradict the record). Every earlier round keeps ``tool_use``.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Callable

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
    ToolCallStart,
    ToolResultPart,
)
from primer.model.model_profile import ModelProfileConfig
from primer.model_profile import ResolvedModel
from primer.session.persistence import _CoalesceState, translate_stream_event
from primer.session.timeline import closes_turn, turn_windows

MODEL = ResolvedModel(
    profile_id="p", provider_id="prov", model_name="m", context_length=4096, config=ModelProfileConfig(),
)


def _agent(cap: int) -> Agent:
    return Agent(id="researcher", description="x", model=AgentModel(profile_id="p--m"), max_tool_turns=cap)


class _ToolRoundsThenStop:
    """Every call asks for one tool, until ``stop_after`` calls have been made; then it answers and stops."""

    def __init__(self, stop_after: int | None = None) -> None:
        self.calls = 0
        self.stop_after = stop_after

    def stream(self, **_kwargs) -> AsyncIterator[StreamEvent]:
        self.calls += 1
        n = self.calls
        stops = self.stop_after is not None and n > self.stop_after

        async def gen() -> AsyncIterator[StreamEvent]:
            yield TextDelta(text=f"round {n} text", index=0)
            if stops:
                yield Done(stop_reason="stop", raw_reason="end_turn")
                return
            yield ToolCallStart(id=f"tc{n}", name="loop_tool", index=1)
            yield ToolCallEnd(id=f"tc{n}", arguments={}, index=1)
            yield Done(stop_reason="tool_use", raw_reason="tool_use")

        return gen()


class _Manager:
    def is_notifying(self, tool_name: str) -> bool:
        return False

    async def list_tools(self, *, principal=None):
        return []

    async def execute(self, call, *, principal=None):
        return ToolResultPart(id=call.id, output="ok", error=False)


class _Turn:
    def __init__(self) -> None:
        self.events: list[StreamEvent] = []
        self.capped: list[bool] = []
        self.interrupted: list[bool] = []

    @property
    def dones(self) -> list[Done]:
        return [e for e in self.events if isinstance(e, Done)]


async def _turn(
    llm, *, cap: int, interrupt: asyncio.Event | None = None, stop_when: Callable[[StreamEvent], bool] | None = None,
) -> _Turn:
    """Drive the real loop. ``stop_when`` is a consumer that sets the Stop on receiving the first event it matches: a
    Stop that lands while that event is being delivered (the persistence writer awaits storage there)."""
    turn = _Turn()

    async def drive() -> None:
        async for ev in run_agent_turn(
            agent=_agent(cap), llm=llm, llm_model=MODEL, tool_manager=_Manager(),
            prompt=[Message(role="user", parts=[TextPart(text="go")])],
            interrupt=interrupt, interrupted_out=turn.interrupted, capped_out=turn.capped,
        ):
            turn.events.append(ev)
            if stop_when is not None and interrupt is not None and not interrupt.is_set() and stop_when(ev):
                interrupt.set()

    await asyncio.wait_for(drive(), 5.0)
    return turn


async def test_the_capped_rounds_done_says_tool_turn_cap_and_keeps_the_models_own_reason() -> None:
    turn = await _turn(_ToolRoundsThenStop(), cap=3)

    assert turn.capped == [True]
    assert [(d.stop_reason, d.raw_reason) for d in turn.dones] == [
        ("tool_use", "tool_use"), ("tool_use", "tool_use"), ("tool_turn_cap", "tool_use"),
    ], "only the round that capped is marked; the earlier tool rounds are ordinary tool_use rounds"


async def test_a_cap_of_one_marks_the_only_round() -> None:
    turn = await _turn(_ToolRoundsThenStop(), cap=1)

    assert turn.capped == [True]
    assert [d.stop_reason for d in turn.dones] == ["tool_turn_cap"]


async def test_the_capped_done_is_the_last_event_of_its_turn_after_the_refusal_results() -> None:
    """The ``done`` closes the turn's window, so the refusal results of the capped round (yielded by the loop after it
    decided to cap) must come BEFORE it, or they land in the NEXT turn's window, orphaned from their calls."""
    turn = await _turn(_ToolRoundsThenStop(), cap=2)

    kinds = [type(e).__name__ for e in turn.events]
    done_at = max(i for i, e in enumerate(turn.events) if isinstance(e, Done))
    assert turn.dones[-1].stop_reason == "tool_turn_cap"
    assert done_at == len(turn.events) - 1, f"events followed the capped done: {kinds[done_at:]}"
    refusal_at = [
        i for i, e in enumerate(turn.events)
        if isinstance(e, ExtendedEvent) and type(e.extended).__name__ == "_ExecutorToolResult"
        and "tool-turn cap reached" in str(e.extended.output)
    ]
    assert len(refusal_at) == 1, f"the capped round's one call was not answered with the refusal: {kinds}"
    assert refusal_at[0] < done_at, "the refusal result came after the capped done"


async def test_a_turn_that_stops_before_the_cap_keeps_its_own_stop_reason() -> None:
    turn = await _turn(_ToolRoundsThenStop(stop_after=1), cap=10)

    assert turn.capped == []
    assert [d.stop_reason for d in turn.dones] == ["tool_use", "stop"]


async def test_a_stop_that_landed_before_the_done_leaves_it_a_tool_use_done() -> None:
    """The Stop is set as the model finishes: it beats the cap (the loop's existing order), so the round is not capped."""
    interrupt = asyncio.Event()

    class _SetsTheStopBeforeDone(_ToolRoundsThenStop):
        def stream(self, **kwargs):
            inner = super().stream(**kwargs)

            async def gen() -> AsyncIterator[StreamEvent]:
                async for ev in inner:
                    if isinstance(ev, Done):
                        interrupt.set()
                    yield ev

            return gen()

    turn = await _turn(_SetsTheStopBeforeDone(), cap=1, interrupt=interrupt)

    assert turn.interrupted == [True] and turn.capped == []
    assert [d.stop_reason for d in turn.dones] == ["tool_use"]


async def test_a_stop_that_lands_after_the_cap_was_decided_does_not_contradict_the_done() -> None:
    """The ``llm_call`` event is the last suspension point before the cap is decided, so a Stop set while a consumer
    holds it is seen by the decision (the Stop wins, the round is not capped and its done stays ``tool_use``). This
    pins that the record and the outcome AGREE, whichever way the tiebreak goes; it does NOT exercise the
    ``and not will_cap`` guard on the interrupt check in the loop, which is defensive only (nothing suspends between
    the decision and that check, so a Stop cannot land there) and is not covered by a test."""
    interrupt = asyncio.Event()

    turn = await _turn(
        _ToolRoundsThenStop(), cap=1, interrupt=interrupt, stop_when=lambda ev: isinstance(ev, ExtendedEvent),
    )

    assert interrupt.is_set(), "the Stop never landed: the harness did not reach the llm_call event"
    says_capped = turn.dones[-1].stop_reason == "tool_turn_cap"
    assert says_capped == (turn.capped == [True]), (
        f"the done says {turn.dones[-1].stop_reason!r} but capped_out={turn.capped} interrupted_out={turn.interrupted}"
    )
    assert turn.capped != turn.interrupted, "the turn ended as exactly one of capped or stopped"


def _lines_of(turn: _Turn) -> list[str]:
    """The turn as the real translator writes it, as messages.jsonl lines (one record per line, seq in order)."""
    state = _CoalesceState()
    records = []
    for ev in turn.events:
        out = translate_stream_event(ev, state)
        records.extend(out if isinstance(out, list) else [out] if out is not None else [])
    return [
        json.dumps({"seq": i, "kind": r.kind.value, "payload": r.payload, "created_at": "2026-10-07T00:00:00+00:00"})
        for i, r in enumerate(records, start=1)
    ]


async def test_the_translator_writes_the_marker_and_the_capped_turn_is_its_own_window() -> None:
    turn = await _turn(_ToolRoundsThenStop(), cap=3)
    lines = _lines_of(turn)
    last = len(lines)
    lines += [
        json.dumps({"seq": last + 1, "kind": "user_input", "payload": {"text": "next"}, "created_at": "t"}),
        json.dumps({"seq": last + 2, "kind": "done", "payload": {"stop_reason": "stop"}, "created_at": "t"}),
    ]

    windows = turn_windows(lines)

    done_payloads = [json.loads(ln)["payload"] for ln in lines if json.loads(ln)["kind"] == "done"]
    assert [p["stop_reason"] for p in done_payloads] == ["tool_use", "tool_use", "tool_turn_cap", "stop"]
    assert len(windows) == 2, f"the capped turn ran on into the next one: {[w['terminal_seq'] for w in windows]}"
    assert windows[0]["terminal_seq"] == last, "the capped turn's window closes at its own done"
    assert [r["kind"] for r in windows[0]["records"]][-2:] == ["tool_result", "done"], (
        "the capped round's refusal result belongs to the capped turn's window, not the next one's"
    )
    assert windows[1]["terminal_seq"] == last + 2


def test_a_tool_turn_cap_done_closes_a_turn_and_a_tool_use_done_does_not() -> None:
    assert closes_turn({"kind": "done", "payload": {"stop_reason": "tool_turn_cap"}}) is True
    assert closes_turn({"kind": "done", "payload": {"stop_reason": "tool_use"}}) is False
