"""The tool round a park leaves unfinished, and the helper every resume finishes it with (ticket 01a15327).

A model asks for several tool calls in ONE round. The loop runs them one after another, and when one of them asks to wait (an approval gate, ``ask_user``)
the park leaves in the middle of the round: the calls before it ran, the calls after it did not. These cases pin the three pieces the agent session and the
graph agent node share:

* ``_dispatch_tool_calls`` says, as the park leaves the batch, what finished before the call that asked to wait (``completed_results``) and which calls
  come after it (``remaining_calls``);
* ``run_agent_turn`` reports what finished (the client action of a delivered notifying call, the result of every finished call) before it lets the
  park go, as the batch would have reported it had it finished, and keeps the round out of ``messages_out`` (the round is not answered yet);
* ``finish_round`` runs the remaining calls in order with the resumed run's manager (each meets its own role floor and approval gate) and returns
  every answer of the round once, in order; a call that pauses leaves with the round as it now stands.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

import pytest

from primer.agent.loop import _dispatch_tool_calls, run_agent_turn
from primer.agent.parked_round import ParkedRound, finish_round
from primer.agent.tool_manager import ToolExecutionManager
from primer.int.toolset import ToolsetProvider
from primer.model.agent import Agent, AgentModel
from primer.model.chat import (
    NOTIFYING_TOOL_RESULT,
    Done,
    ExtendedEvent,
    Message,
    StreamEvent,
    TextDelta,
    TextPart,
    Tool,
    ToolCallEnd,
    ToolCallPart,
    ToolCallResult,
    ToolCallStart,
    ToolResultPart,
    _ClientAction,
    _ExecutorToolResult,
)
from primer.model.except_ import AuthRequiredError
from primer.model.model_profile import ModelProfileConfig
from primer.model.tool_approval import RequiredApprovalConfig, ToolApprovalPolicy
from primer.model.yield_ import Yielded, YieldToWorker
from primer.model_profile import ResolvedModel
from tests.agent.test_inner_dispatch import USER, _Recorder, _Session, _Yielding
from tests.agent.test_tool_manager_approval_gate import _PoliciesOnlyResolver

GATED = "rec__user_x"
SECOND_GATE = "rec__user_y"
_SCHEMA = {"type": "object", "properties": {}, "additionalProperties": True}


def _policy(tool_name: str) -> ToolApprovalPolicy:
    return ToolApprovalPolicy(id=f"p-{tool_name}", toolset_id="rec", tool_name=tool_name, enabled=True, approval=RequiredApprovalConfig())


class _Notifier(ToolsetProvider):
    """Toolset ``ui`` with one NOTIFYING tool, ``show``: the runner answers it itself and delivers it as a client action."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def required_role(self, tool_name: str) -> str:
        return "user"

    async def list_tools(self, *, principal: str | None = None) -> AsyncIterator[Tool]:
        yield Tool(id="show", description="shows a thing", toolset_id="ui", args_schema=_SCHEMA, tool_class="notifying")

    async def call(self, *, tool_name, arguments, principal=None, ctx=None) -> ToolCallResult:
        self.calls.append(ctx.tool_call_id)
        return ToolCallResult(output="ignored", is_error=False)


class _Raising(ToolsetProvider):
    """Toolset ``z``: ``auth`` needs a sign-in (``AuthRequiredError``), ``bug`` raises an exception that is not a ``PrimerError``."""

    def required_role(self, tool_name: str) -> str:
        return "user"

    async def list_tools(self, *, principal: str | None = None) -> AsyncIterator[Tool]:
        yield Tool(id="auth", description="needs a sign-in", toolset_id="z", args_schema=_SCHEMA)
        yield Tool(id="bug", description="has a bug", toolset_id="z", args_schema=_SCHEMA)

    async def call(self, *, tool_name, arguments, principal=None, ctx=None) -> ToolCallResult:
        if tool_name == "auth":
            raise AuthRequiredError("sign in to z first", auth_url="https://z.example/auth", state="s")
        raise ValueError("the tool has a bug")


def _rec() -> _Recorder:
    return _Recorder({"user_x": "user", "user_y": "user", "user_a": "user", "user_b": "user"})


def _manager(rec: _Recorder, *, policies: tuple[str, ...] = ("user_x", "user_y"), **providers: Any) -> ToolExecutionManager:
    tools = [GATED, SECOND_GATE, "rec__user_a", "rec__user_b", "ui__show", "y__wait", "z__auth", "z__bug"]
    return ToolExecutionManager.for_workspace(
        toolset_providers={"rec": rec, **providers}, session=_Session("s-1", "ws-1"), tools=tools, initiated_by=USER,
        approval_resolver=_PoliciesOnlyResolver([_policy(name) for name in policies]),
    )


def _call(call_id: str, name: str) -> ToolCallPart:
    return ToolCallPart(id=call_id, name=name, arguments={"n": call_id})


def _round(gate_at: int) -> list[ToolCallPart]:
    """``call-a`` and ``call-b`` (ungated) with the gated ``call-g`` at ``gate_at``."""
    calls = [_call("call-a", "rec__user_a"), _call("call-b", "rec__user_b")]
    calls.insert(gate_at, _call("call-g", GATED))
    return calls


def _ran(rec: _Recorder) -> list[str]:
    return [call[3].tool_call_id for call in rec.calls]


def _answer(call_id: str, output: str = "answered") -> ToolResultPart:
    return ToolResultPart(id=call_id, output=output)


def _shown(events: list[Any]) -> list[tuple[str, str]]:
    """``(kind, call id)`` of every client action and tool result among ``events``, in order."""
    out: list[tuple[str, str]] = []
    for event in events:
        extended = event.extended if isinstance(event, ExtendedEvent) else None
        if isinstance(extended, _ClientAction):
            out.append(("client_action", extended.call_id))
        elif isinstance(extended, _ExecutorToolResult):
            out.append(("tool_result", extended.call_id))
    return out


async def _finish(parked_round: ParkedRound, answers: list[ToolResultPart], manager: Any) -> tuple[list[Any], list[ToolResultPart]]:
    events: list[Any] = []
    results: list[ToolResultPart] = []
    async for event in finish_round(parked_round, answers, tool_manager=manager, principal=None, results_out=results):
        events.append(event)
    return events, results


# ---- the park says where the round stopped -------------------------------------------------------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("gate_at", [0, 1, 2], ids=["gate-first", "gate-middle", "gate-last"])
async def test_the_park_says_which_calls_of_the_round_finished_and_which_did_not_run(gate_at: int) -> None:
    rec = _rec()
    calls = _round(gate_at)
    ids = [c.id for c in calls]

    with pytest.raises(YieldToWorker) as parked:
        await _dispatch_tool_calls(calls, tool_manager=_manager(rec), principal=None, actions_out=[])

    park = parked.value
    assert park.tool_call_id == "call-g" and park.batch_index == gate_at
    assert [(p.id, p.error) for p in park.completed_results] == [(i, False) for i in ids[:gate_at]], "the calls before the gate ran and keep their results"
    assert [c.id for c in park.remaining_calls] == ids[gate_at + 1:], "the calls after the gate did not run, and the park names them"
    assert _ran(rec) == ids[:gate_at]


@pytest.mark.asyncio
async def test_a_round_that_parks_reports_what_finished_before_it_and_keeps_the_round_out_of_the_history() -> None:
    """What finished before the gate is reported as the round parks (it was dropped): the delivered client action first, then the result of every finished
    call, in order; the gate and the call after it have no result yet, and ``messages_out`` holds the round's assistant message and no partial answer."""
    rec, notifier = _rec(), _Notifier()
    manager = _manager(rec, ui=notifier)
    reply: list[StreamEvent] = [TextDelta(text="calling the tools", index=0)]
    for i, (call_id, name) in enumerate([("call-a", "rec__user_a"), ("call-n", "ui__show"), ("call-g", GATED), ("call-b", "rec__user_b")], start=1):
        reply += [ToolCallStart(id=call_id, name=name, index=i), ToolCallEnd(id=call_id, arguments={}, index=i)]
    reply.append(Done(stop_reason="tool_use", raw_reason="tool_use"))

    class _LLM:
        async def list_models(self) -> list[str]:
            return ["m"]

        def stream(self, **_kw: Any) -> AsyncIterator[StreamEvent]:
            async def events() -> AsyncIterator[StreamEvent]:
                for event in reply:
                    yield event

            return events()

    agent = Agent(id="x", description="x", model=AgentModel(profile_id="p--m"), tools=[GATED, "rec__user_a", "rec__user_b", "ui__show"])
    model = ResolvedModel(profile_id="p", provider_id="prov", model_name="m", context_length=128_000, config=ModelProfileConfig())
    produced: list[Message] = []
    events: list[Any] = []
    with pytest.raises(YieldToWorker) as parked:
        async for event in run_agent_turn(
            agent=agent, llm=_LLM(), llm_model=model, tool_manager=manager, prompt=[Message(role="user", parts=[TextPart(text="go")])], messages_out=produced,
        ):
            events.append(event)

    assert _shown(events) == [("client_action", "call-n"), ("tool_result", "call-a"), ("tool_result", "call-n")]
    results = {e.extended.call_id: e.extended for e in events if isinstance(e, ExtendedEvent) and isinstance(e.extended, _ExecutorToolResult)}
    assert json.loads(results["call-a"].output) == {"ran": "user_a"} and results["call-n"].output == NOTIFYING_TOOL_RESULT
    assert [m.role for m in produced] == ["assistant"], "the round is not answered yet: no partial tool message reaches the history"
    park = parked.value
    assert park.tool_call_id == "call-g" and [p.id for p in park.completed_results] == ["call-a", "call-n"]
    assert [c.id for c in park.remaining_calls] == ["call-b"]
    assert _ran(rec) == ["call-a"] and notifier.calls == ["call-n"]


# ---- the round as a park record stores it ---------------------------------------------------------------------------------------------------------------------------


def _a_park() -> YieldToWorker:
    return YieldToWorker(Yielded(tool_name="_approval", event_key="tool_approval:s-1:call-g"), tool_call_id="call-g")


def test_the_round_round_trips_through_its_stored_form() -> None:
    park = _a_park()
    park.completed_results = [_answer("call-a", "a"), ToolResultPart(id="call-n", output="failed", error=True, metadata={"k": 1})]
    park.remaining_calls = [_call("call-b", "rec__user_b")]

    parked_round = ParkedRound.of_park(park)
    stored = json.loads(json.dumps({"completed": parked_round.completed_jsonable(), "remaining": parked_round.remaining_jsonable()}))
    back = ParkedRound.from_jsonable(stored["completed"], stored["remaining"])

    assert back == parked_round and not back.is_empty
    assert [(p.id, p.output, p.error, p.metadata) for p in back.completed] == [("call-a", "a", False, None), ("call-n", "failed", True, {"k": 1})]
    assert [(c.id, c.name, c.arguments) for c in back.remaining] == [("call-b", "rec__user_b", {"n": "call-b"})]


def test_a_round_a_resume_cannot_read_is_empty_so_the_resume_still_answers_the_parked_call() -> None:
    damaged = [{"output": "no id"}]
    with pytest.raises(ValueError):
        ParkedRound.from_jsonable(damaged, None)

    assert ParkedRound.from_record(damaged, None).is_empty
    assert ParkedRound.from_record(None, "not a list of calls").is_empty
    assert ParkedRound.from_record([_answer("call-a").model_dump(mode="json")], None).completed == (_answer("call-a"),)


def test_a_park_that_stored_no_round_has_an_empty_one() -> None:
    """A park written before rounds were carried (no stored fields), and a park nothing stamped, finish nothing: the parked call alone is answered."""
    assert ParkedRound.from_jsonable(None, None).is_empty
    assert ParkedRound.from_jsonable([], []).is_empty
    assert ParkedRound.of_park(_a_park()).is_empty


# ---- the resume finishes the round ----------------------------------------------------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_an_empty_round_is_the_parked_calls_answer_alone() -> None:
    rec = _rec()
    events, results = await _finish(ParkedRound(), [_answer("call-g")], _manager(rec))

    assert events == [] and rec.calls == []
    assert [(p.id, p.output) for p in results] == [("call-g", "answered")]


@pytest.mark.asyncio
async def test_the_remaining_calls_run_in_order_and_every_call_is_answered_once_in_order() -> None:
    rec, notifier = _rec(), _Notifier()
    parked_round = ParkedRound(completed=(_answer("call-a", "a"),), remaining=(_call("call-n", "ui__show"), _call("call-b", "rec__user_b")))

    events, results = await _finish(parked_round, [_answer("call-g", "g")], _manager(rec, ui=notifier))

    assert [p.id for p in results] == ["call-a", "call-g", "call-n", "call-b"], "the completed answers, the parked call's, then one per remaining call"
    assert [(p.output, p.error) for p in results[:3]] == [("a", False), ("g", False), (NOTIFYING_TOOL_RESULT, False)]
    assert json.loads(results[3].output) == {"ran": "user_b"}
    assert _shown(events) == [("client_action", "call-n"), ("tool_result", "call-n"), ("tool_result", "call-b")], (
        "each call that runs now is reported as the live batch reports it: delivered, then answered; the answers given before are not reported again"
    )
    assert _ran(rec) == ["call-b"] and notifier.calls == ["call-n"]


@pytest.mark.asyncio
async def test_a_remaining_call_meets_its_own_gate_and_leaves_with_the_round_as_it_now_stands() -> None:
    rec = _rec()
    parked_round = ParkedRound(
        completed=(_answer("call-a", "a"),), remaining=(_call("call-b", "rec__user_b"), _call("call-h", SECOND_GATE), _call("call-c", "rec__user_a")),
    )
    results: list[ToolResultPart] = []
    events: list[Any] = []

    with pytest.raises(YieldToWorker) as parked:
        async for event in finish_round(parked_round, [_answer("call-g", "g")], tool_manager=_manager(rec), principal=None, results_out=results):
            events.append(event)

    park = parked.value
    assert (park.tool_call_id, park.yielded.tool_name) == ("call-h", "_approval"), "a later call of the round is gated like any call: it is not approved"
    assert [p.id for p in park.completed_results] == ["call-a", "call-g", "call-b"], "everything answered before it rides on the park"
    assert [c.id for c in park.remaining_calls] == ["call-c"] and park.batch_index == 3
    assert _shown(events) == [("tool_result", "call-b")] and _ran(rec) == ["call-b"]
    assert results == [], "nothing is handed back for a round that is not finished"


@pytest.mark.asyncio
async def test_a_remaining_call_that_pauses_leaves_with_the_round_as_it_now_stands() -> None:
    rec = _rec()
    parked_round = ParkedRound(remaining=(_call("call-w", "y__wait"), _call("call-b", "rec__user_b")))

    with pytest.raises(YieldToWorker) as parked:
        await _finish(parked_round, [_answer("call-g", "g")], _manager(rec, y=_Yielding()))

    park = parked.value
    assert (park.tool_call_id, park.yielded.event_key) == ("call-w", "k:call-w")
    assert [p.id for p in park.completed_results] == ["call-g"] and [c.id for c in park.remaining_calls] == ["call-b"] and park.batch_index == 1
    assert rec.calls == [], "the call after the pause has not run"


@pytest.mark.asyncio
async def test_a_remaining_call_that_fails_is_answered_with_an_error_and_the_round_goes_on() -> None:
    """A call the manager cannot route is the error result the live batch gives; a call that needs a sign-in, or raises anything else, is an error
    result too (a resume cannot move the session to WAITING in the middle of a round): every call is answered and the calls after it still run."""
    rec = _rec()
    parked_round = ParkedRound(remaining=(_call("call-u", "rec__gone"), _call("call-s", "z__auth"), _call("call-x", "z__bug"), _call("call-b", "rec__user_b")))

    events, results = await _finish(parked_round, [_answer("call-g")], _manager(rec, z=_Raising()))

    assert [(p.id, p.error) for p in results] == [("call-g", False), ("call-u", True), ("call-s", True), ("call-x", True), ("call-b", False)]
    assert "rec__gone" in results[1].output and "sign in to z first" in results[2].output and "the tool has a bug" in results[3].output
    assert _shown(events) == [("tool_result", "call-u"), ("tool_result", "call-s"), ("tool_result", "call-x"), ("tool_result", "call-b")]
    assert _ran(rec) == ["call-b"]


@pytest.mark.asyncio
async def test_with_no_manager_to_run_them_the_remaining_calls_are_answered_not_run() -> None:
    parked_round = ParkedRound(completed=(_answer("call-a"),), remaining=(_call("call-b", "rec__user_b"),))

    events, results = await _finish(parked_round, [_answer("call-g")], None)

    assert [(p.id, p.error) for p in results] == [("call-a", False), ("call-g", False), ("call-b", True)]
    assert results[2].output.startswith("not run")
    assert _shown(events) == [("tool_result", "call-b")]
