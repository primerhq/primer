"""Context-overflow recovery on the real executor and a real workspace: what it did, and what it does now.

When a turn's own LLM call is rejected as a context overflow, ``invoke`` force-compacts and replays. The
characterisation tests of #318 pinned the old behaviour as limitations (tools run again, nothing recorded on a
second overflow, a summariser overflow failing the turn); these tests pin the recovery that replaced it:

* the replay CONTINUES the turn: the rounds the rejected attempt completed are folded into the history the
  compaction works on (reduced, with ALREADY RAN placeholders) and written by its marker, so no tool runs twice
  and the record shows each executed call once. A REACTIVE model (it emits a tool call only while no result for
  it is in its prompt) is what can tell this from a replay that starts over: a scripted one replays its list
  whatever it is asked;
* the turn keeps its ``max_tool_turns`` budget across the replay;
* ONE chokepoint persists a turn's completed rounds on every exit that is not a park or a normal finish, in the
  reduced form the model last saw: a second overflow, a replay that fails otherwise, a failure of the first
  attempt, the generator closed, a hard cancel. The raw output stays in the event log; persisting it raw would
  let the next turn overflow on it again;
* a second overflow ends the turn with ``ContextOverflowUnrecoverable`` (typed code and extensions);
* steers written while the compaction ran are in the replay's input;
* a compaction marker takes its seq from the turn's event-log writer when there is one.

(The overflow classifier, the cap results of ``max_tool_turns`` and the summariser's own recovery are other
units: tasks 01a108b1-2480, 01a108b1-47d7 and the A.4 follow-up.)
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from primer.agent.compaction import CompactionStrategy
from primer.model.chat import (
    Done, ExtendedEvent, Message, TextPart, Tool, ToolCallEnd, ToolCallPart, ToolCallStart, ToolResultPart,
)
from primer.model.except_ import BadRequestError, ContextOverflowUnrecoverable, ServerError
from primer.model.model_profile import ModelProfileConfig
from primer.model.yield_ import Yielded, YieldToWorker
from primer.model_profile import ResolvedModel
from tests._support.off_golden import (
    Events, FnLLM, Gate, ScriptedLLM, append_messages, assistant_message, make_executor, open_session, run_turn,
    text_events, user_message,
)

OVERFLOW = "This model's maximum context length is 100000 tokens, however you requested more"
POSIX = pytest.mark.skipif(not Path("/usr/bin/env").exists(), reason="needs a POSIX shell for the exec tool")
QUESTION = "now do the thing"


def _text(message) -> str:
    return "".join(p.text for p in message.parts if isinstance(p, TextPart))


def _call(call_id: str, label: str | None = None) -> list:
    label = label or call_id
    return [
        ToolCallStart(id=call_id, name="workspace__exec", index=0),
        ToolCallEnd(
            id=call_id, arguments={"command": f"echo {label} >> counter.txt", "description": "count executions"}, index=0,
        ),
        Done(stop_reason="tool_use", raw_reason="tool_use"),
    ]


def _has_result(messages, call_id: str) -> bool:
    return any(isinstance(p, ToolResultPart) and p.id == call_id for m in messages for p in m.parts)


def _is_summariser(kwargs) -> bool:
    return kwargs.get("max_output_tokens") == 4096


async def _seed(workspace, session, n: int = 6) -> None:
    """Answered exchanges, then the question the turn answers (the user input is a history line on the live path)."""
    for i in range(n):
        await append_messages(workspace, session, user_message(f"filler {i}: " + "f" * 500), assistant_message(f"reply {i}"))
    await append_messages(workspace, session, user_message(QUESTION))


def _lines(workspace, session) -> list[dict]:
    path = workspace.root / workspace.template.state_path / "sessions" / session.session_id / "messages.jsonl"
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _counter(workspace) -> list[str]:
    path = workspace.root / "counter.txt"
    return path.read_text().splitlines() if path.exists() else []


async def _reload(session) -> list:
    return await make_executor(session, ScriptedLLM())._read_messages_jsonl()  # noqa: SLF001 - the reader under test


def _tool_ids(messages) -> tuple[list[str], list[str]]:
    from primer.model.chat import ToolCallPart

    calls = [p.id for m in messages for p in m.parts if isinstance(p, ToolCallPart)]
    results = [p.id for m in messages for p in m.parts if isinstance(p, ToolResultPart)]
    return calls, results


def _reactive(*, overflow_after: str | None = "call_a", overflow_every_time: bool = False, more_rounds: tuple[str, ...] = ()):
    """A model that emits ``call_a`` while no result for it is in the prompt, overflows once its result is there
    (once, or on every call when ``overflow_every_time``), summarises when asked, and otherwise finishes."""
    state = {"overflowed": 0}

    def fn(n, messages, kwargs):
        if _is_summariser(kwargs):
            return text_events("SUMMARY")
        if not _has_result(messages, "call_a"):
            return _call("call_a", "a")
        if overflow_after and state["overflowed"] < (10**9 if overflow_every_time else 1) and not any(
            _has_result(messages, extra) for extra in more_rounds
        ):
            state["overflowed"] += 1
            return BadRequestError(OVERFLOW)
        for extra in more_rounds:
            if not _has_result(messages, extra):
                return _call(extra, extra[-1])
        return text_events("done")

    return fn


@POSIX
class TestTheReplayContinuesTheTurn:
    async def test_a_tool_the_rejected_attempt_already_ran_is_not_run_again_and_is_recorded_once(self, tmp_path) -> None:
        backend, workspace, session = await open_session(tmp_path)
        try:
            await _seed(workspace, session)
            llm = FnLLM(_reactive())
            await run_turn(session, llm)
            assert _counter(workspace) == ["a"], "the tool ran once: the replay continued from its result"
            shown = await _reload(session)
            texts = [_text(m) for m in shown]
            assert QUESTION in texts and texts[-1] == "done"
            calls, results = _tool_ids(shown)
            assert (calls, results) == (["call_a"], ["call_a"]), "the executed call and its result are in the history once"
            assert len(llm.calls) == 4, "the tool call, the rejected call, the summary, the replay"
        finally:
            await session.aclose()
            await backend.aclose()

    async def test_the_replay_is_built_on_the_compacted_history_that_holds_the_rounds_so_far(self, tmp_path) -> None:
        backend, workspace, session = await open_session(tmp_path)
        try:
            await _seed(workspace, session)
            llm = FnLLM(_reactive())
            await run_turn(session, llm)
            replay = llm.calls[3]["messages"]
            assert replay[0].role == "system"
            assert _text(replay[1]).endswith("SUMMARY") and replay[1].role == "assistant"
            assert [m.role for m in replay[-3:]] == ["user", "assistant", "tool"] and _text(replay[-3]) == QUESTION
        finally:
            await session.aclose()
            await backend.aclose()

    async def test_the_tool_turn_budget_is_the_turns_not_a_fresh_one(self, tmp_path) -> None:
        """``max_tool_turns=2``: one round ran before the overflow, so the replay's first tool call is the cap."""
        def cap(executor) -> None:
            executor._agent = executor._agent.model_copy(update={"max_tool_turns": 2})  # noqa: SLF001

        backend, workspace, session = await open_session(tmp_path)
        try:
            await _seed(workspace, session)
            llm = FnLLM(_reactive(more_rounds=("call_b",)))
            await run_turn(session, llm, configure=cap)
            assert _counter(workspace) == ["a"], "call_b was the second round of the turn: the cap, not dispatched"
        finally:
            await session.aclose()
            await backend.aclose()

    async def test_a_steer_deferred_while_the_compaction_ran_is_in_the_replay_input_and_the_history(self, tmp_path) -> None:
        backend, workspace, session = await open_session(tmp_path)
        try:
            await _seed(workspace, session)
            entered, release = asyncio.Event(), asyncio.Event()
            state = {"overflowed": False}

            def fn(n, messages, kwargs):
                if _is_summariser(kwargs):
                    return Gate(entered, release, text_events("SUMMARY"))
                if not _has_result(messages, "call_a"):
                    return _call("call_a", "a")
                if not state["overflowed"]:
                    state["overflowed"] = True
                    return BadRequestError(OVERFLOW)
                return text_events("done")

            llm = FnLLM(fn)
            task = asyncio.create_task(run_turn(session, llm))
            await asyncio.wait_for(entered.wait(), timeout=30)
            await session.append_instruction("STEER-WHILE-IT-COMPACTED")  # deferred by the compaction window
            release.set()
            await asyncio.wait_for(task, timeout=30)
            replay = llm.calls[-1]["messages"]
            assert "STEER-WHILE-IT-COMPACTED" in [_text(m) for m in replay], "the replay was not handed the steer"
            assert [_text(m) for m in await _reload(session)].count("STEER-WHILE-IT-COMPACTED") == 1
        finally:
            await session.aclose()
            await backend.aclose()

    async def test_a_steer_written_mid_turn_before_the_compaction_is_in_the_replay_input_too(self, tmp_path) -> None:
        backend, workspace, session = await open_session(tmp_path)
        try:
            await _seed(workspace, session)
            entered, release = asyncio.Event(), asyncio.Event()
            state = {"gated": False}

            def fn(n, messages, kwargs):
                if _is_summariser(kwargs):
                    return text_events("SUMMARY")
                if not _has_result(messages, "call_a"):
                    return _call("call_a", "a")
                if not state["gated"]:
                    state["gated"] = True
                    return Gate(entered, release, [], error=BadRequestError(OVERFLOW))
                return text_events("done")

            llm = FnLLM(fn)
            task = asyncio.create_task(run_turn(session, llm))
            await asyncio.wait_for(entered.wait(), timeout=30)
            await session.append_instruction("MID-TURN-STEER")  # written to the log, no window is open
            release.set()
            await asyncio.wait_for(task, timeout=30)
            assert "MID-TURN-STEER" in [_text(m) for m in llm.calls[-1]["messages"]]
            assert [_text(m) for m in await _reload(session)].count("MID-TURN-STEER") == 1
        finally:
            await session.aclose()
            await backend.aclose()


@POSIX
class TestASecondOverflow:
    async def test_after_a_tool_round_it_ends_the_turn_with_a_typed_failure_and_the_round_is_recorded_once(self, tmp_path) -> None:
        backend, workspace, session = await open_session(tmp_path)
        try:
            await _seed(workspace, session)
            llm = FnLLM(_reactive(overflow_every_time=True))
            with pytest.raises(ContextOverflowUnrecoverable) as failed:
                await run_turn(session, llm)
            error = failed.value
            assert error.code == "context_overflow_unrecoverable" and error.ended_detail_code == "context_overflow_unrecoverable"
            assert isinstance(error.__cause__, BadRequestError)
            assert error.problem_extensions == {
                "forced_compaction": True, "replay_attempted": True, "persisted_rounds": 1, "summarised_rounds": 0,
            }
            assert _counter(workspace) == ["a"]
            shown = await _reload(session)
            assert _tool_ids(shown) == (["call_a"], ["call_a"]) and [_text(m) for m in shown].count(QUESTION) == 1
            assert "done" not in [_text(m) for m in shown]
        finally:
            await session.aclose()
            await backend.aclose()

    async def test_with_nothing_run_yet_it_records_nothing_but_the_marker(self, tmp_path) -> None:
        backend, workspace, session = await open_session(tmp_path)
        try:
            await _seed(workspace, session)

            def fn(n, messages, kwargs):
                return text_events("SUMMARY") if _is_summariser(kwargs) else BadRequestError(OVERFLOW)

            with pytest.raises(ContextOverflowUnrecoverable) as failed:
                await run_turn(session, FnLLM(fn))
            assert failed.value.problem_extensions == {
                "forced_compaction": True, "replay_attempted": True, "persisted_rounds": 0, "summarised_rounds": 0,
            }
            assert _tool_ids(await _reload(session)) == ([], [])
        finally:
            await session.aclose()
            await backend.aclose()

    async def test_a_replay_that_runs_a_round_and_overflows_again_records_both_rounds_once(self, tmp_path) -> None:
        backend, workspace, session = await open_session(tmp_path)
        try:
            await _seed(workspace, session)
            state = {"first": True}

            def fn(n, messages, kwargs):
                if _is_summariser(kwargs):
                    return text_events("SUMMARY")
                if not _has_result(messages, "call_a"):
                    return _call("call_a", "a")
                if state["first"]:
                    state["first"] = False
                    return BadRequestError(OVERFLOW)           # attempt 1: rejected after round a
                if not _has_result(messages, "call_b"):
                    return _call("call_b", "b")                # the replay runs round b ...
                return BadRequestError(OVERFLOW)               # ... and is rejected again

            with pytest.raises(ContextOverflowUnrecoverable) as failed:
                await run_turn(session, FnLLM(fn))
            assert failed.value.persisted_rounds == 2
            assert _counter(workspace) == ["a", "b"], "each tool ran once"
            assert _tool_ids(await _reload(session)) == (["call_a", "call_b"], ["call_a", "call_b"])
        finally:
            await session.aclose()
            await backend.aclose()

    async def test_the_persisted_form_is_the_reduced_one_so_the_next_turn_does_not_overflow(self, tmp_path) -> None:
        """A 600k-character result rejected twice is persisted CUT, with the already-ran note: persisted raw it would
        overflow every later turn and wedge the session."""
        from primer.model.chat import ToolResultPart as Result

        backend, workspace, session = await open_session(tmp_path)
        try:
            await _seed(workspace, session)
            raw = "x" * 600_000

            class Big:
                def __init__(self, inner) -> None:
                    self._inner = inner

                def __getattr__(self, name):
                    return getattr(self._inner, name)

                async def execute(self, call, **kwargs):
                    if call.id == "call_a":
                        return Result(id=call.id, output=raw)
                    return await self._inner.execute(call, **kwargs)

            def fn(n, messages, kwargs):
                if _is_summariser(kwargs):
                    return text_events("SUMMARY")
                if not _has_result(messages, "call_a"):
                    return _call("call_a", "a")
                return BadRequestError(OVERFLOW)

            with pytest.raises(ContextOverflowUnrecoverable):
                await run_turn(session, FnLLM(fn), wrap_tools=Big)
            shown = await _reload(session)
            outputs = [p.output for m in shown for p in m.parts if isinstance(p, Result)]
            assert len(outputs) == 1 and len(outputs[0]) < 20_000, "persisted cut, not raw"
            assert "ALREADY RAN" in outputs[0] and "do NOT call it again" in outputs[0]

            # what the next turn is handed (the session ENDS on a failed turn and a new user message re-opens it,
            # so the reload is the history that turn starts from): small, not 150k tokens of raw output
            from primer.agent.compaction import CompactionStrategy

            assert CompactionStrategy._estimate_tokens(shown) < 30_000  # noqa: SLF001
        finally:
            await session.aclose()
            await backend.aclose()


    async def test_a_huge_result_the_replay_itself_produces_is_reduced_in_what_it_sends_and_recorded_raw(self, tmp_path) -> None:
        """The guard reduces the replay's OUTGOING prompt (its own rounds included), never what the turn records."""
        from primer.model.chat import ToolResultPart as Result

        backend, workspace, session = await open_session(tmp_path)
        try:
            await _seed(workspace, session)
            raw = "y" * 600_000
            state = {"overflowed": False}
            sizes: dict[int, int] = {}

            class Big:
                def __init__(self, inner) -> None:
                    self._inner = inner

                def __getattr__(self, name):
                    return getattr(self._inner, name)

                async def execute(self, call, **kwargs):
                    if call.id == "call_b":
                        return Result(id=call.id, output=raw)
                    return await self._inner.execute(call, **kwargs)

            def fn(n, messages, kwargs):
                sizes[n] = sum(len(p.output) for m in messages for p in m.parts if isinstance(p, Result))
                if _is_summariser(kwargs):
                    return text_events("SUMMARY")
                if not _has_result(messages, "call_a"):
                    return _call("call_a", "a")
                if not state["overflowed"]:
                    state["overflowed"] = True
                    return BadRequestError(OVERFLOW)
                if not _has_result(messages, "call_b"):
                    return _call("call_b", "b")
                return text_events("done")

            llm = FnLLM(fn)
            await run_turn(session, llm, wrap_tools=Big)
            after_b = max(sizes)  # the call that follows round b
            assert sizes[after_b] < 100_000, "the replay's own 600k-char result went out reduced"
            shown = await _reload(session)
            outputs = [p.output for m in shown for p in m.parts if isinstance(p, Result)]
            assert raw in outputs, "and the turn recorded it raw"
        finally:
            await session.aclose()
            await backend.aclose()


@POSIX
class TestEveryOtherWayATurnCanEnd:
    """The chokepoint: completed rounds are persisted whatever ended the turn, except a park."""

    async def test_a_failure_of_the_first_attempt_after_a_tool_round_records_the_round(self, tmp_path) -> None:
        backend, workspace, session = await open_session(tmp_path)
        try:
            await _seed(workspace, session)

            def fn(n, messages, kwargs):
                return _call("call_a", "a") if not _has_result(messages, "call_a") else ServerError("provider fell over")

            with pytest.raises(ServerError):
                await run_turn(session, FnLLM(fn))
            assert _tool_ids(await _reload(session)) == (["call_a"], ["call_a"])
            assert _counter(workspace) == ["a"]
        finally:
            await session.aclose()
            await backend.aclose()

    async def test_a_failed_turns_rounds_are_persisted_in_the_reduced_form_not_raw(self, tmp_path) -> None:
        from primer.model.chat import ToolResultPart as Result

        backend, workspace, session = await open_session(tmp_path)
        try:
            await _seed(workspace, session)

            class Big:
                def __init__(self, inner) -> None:
                    self._inner = inner

                def __getattr__(self, name):
                    return getattr(self._inner, name)

                async def execute(self, call, **kwargs):
                    return Result(id=call.id, output="z" * 600_000)

            def fn(n, messages, kwargs):
                return _call("call_a", "a") if not _has_result(messages, "call_a") else ServerError("provider fell over")

            with pytest.raises(ServerError):
                await run_turn(session, FnLLM(fn), wrap_tools=Big)
            outputs = [p.output for m in await _reload(session) for p in m.parts if isinstance(p, Result)]
            assert len(outputs) == 1 and len(outputs[0]) < 20_000 and "ALREADY RAN" in outputs[0]
        finally:
            await session.aclose()
            await backend.aclose()

    async def test_a_replay_that_fails_for_another_reason_records_the_rounds_it_ran(self, tmp_path) -> None:
        backend, workspace, session = await open_session(tmp_path)
        try:
            await _seed(workspace, session)
            state = {"first": True}

            def fn(n, messages, kwargs):
                if _is_summariser(kwargs):
                    return text_events("SUMMARY")
                if not _has_result(messages, "call_a"):
                    return _call("call_a", "a")
                if state["first"]:
                    state["first"] = False
                    return BadRequestError(OVERFLOW)
                return _call("call_b", "b") if not _has_result(messages, "call_b") else ServerError("provider fell over")

            with pytest.raises(ServerError):
                await run_turn(session, FnLLM(fn))
            assert _tool_ids(await _reload(session)) == (["call_a", "call_b"], ["call_a", "call_b"])
        finally:
            await session.aclose()
            await backend.aclose()

    async def test_closing_the_generator_records_the_rounds_completed_so_far(self, tmp_path) -> None:
        """What dispatch does on an error path (and what a Stop used to do): aclose() at a yield."""
        backend, workspace, session = await open_session(tmp_path)
        try:
            await _seed(workspace, session)
            llm = FnLLM(lambda n, m, k: _call("call_a", "a") if not _has_result(m, "call_a") else text_events("never"))
            executor = make_executor(session, llm)
            stream = executor.invoke([])
            async for event in stream:
                if isinstance(event, ExtendedEvent) and getattr(event.extended, "type", None) == "executor_tool_result":
                    break
            await stream.aclose()
            assert _tool_ids(await _reload(session)) == (["call_a"], ["call_a"])
            assert len(llm.calls) == 1, "the turn did not go on"
        finally:
            await session.aclose()
            await backend.aclose()

    async def test_a_hard_cancel_records_the_rounds_completed_so_far_under_a_shield(self, tmp_path) -> None:
        backend, workspace, session = await open_session(tmp_path)
        try:
            await _seed(workspace, session)
            entered, release = asyncio.Event(), asyncio.Event()

            def fn(n, messages, kwargs):
                if not _has_result(messages, "call_a"):
                    return _call("call_a", "a")
                return Gate(entered, release, text_events("never"))

            task = asyncio.create_task(run_turn(session, FnLLM(fn)))
            await asyncio.wait_for(entered.wait(), timeout=30)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert _tool_ids(await _reload(session)) == (["call_a"], ["call_a"])
        finally:
            await session.aclose()
            await backend.aclose()

    async def test_a_turn_that_finishes_normally_is_persisted_once_by_the_normal_path(self, tmp_path) -> None:
        backend, workspace, session = await open_session(tmp_path)
        try:
            await _seed(workspace, session)
            await run_turn(session, FnLLM(lambda n, m, k: _call("call_a", "a") if not _has_result(m, "call_a") else text_events("done")))
            shown = await _reload(session)
            assert _tool_ids(shown) == (["call_a"], ["call_a"]) and _text(shown[-1]) == "done"
            assert _counter(workspace) == ["a"]
        finally:
            await session.aclose()
            await backend.aclose()


@POSIX
class TestAStopDuringTheReplay:
    async def test_it_ends_the_replay_cleanly_and_the_rounds_before_it_are_in_the_history_once(self, tmp_path) -> None:
        backend, workspace, session = await open_session(tmp_path)
        try:
            await _seed(workspace, session)
            entered, release = asyncio.Event(), asyncio.Event()
            state = {"overflowed": False}

            def fn(n, messages, kwargs):
                if _is_summariser(kwargs):
                    return text_events("SUMMARY")
                if not _has_result(messages, "call_a"):
                    return _call("call_a", "a")
                if not state["overflowed"]:
                    state["overflowed"] = True
                    return BadRequestError(OVERFLOW)
                return Gate(entered, release, text_events("never streamed"))   # the replay's call

            stop = asyncio.Event()
            executor = make_executor(session, FnLLM(fn), configure=lambda ex: ex.bind_interrupt_event(stop))
            events: list = []

            async def drive() -> None:
                async for event in executor.invoke([]):
                    events.append(event)

            task = asyncio.create_task(drive())
            await asyncio.wait_for(entered.wait(), timeout=30)
            stop.set()
            await asyncio.wait_for(task, timeout=30)  # no exception: a Stop is a clean end
            assert executor.was_interrupted is True
            shown = await _reload(session)
            assert _tool_ids(shown) == (["call_a"], ["call_a"]) and _counter(workspace) == ["a"]
            assert "never streamed" not in [_text(m) for m in shown]
        finally:
            await session.aclose()
            await backend.aclose()


class TestTheMarkerTakesItsSeqFromTheEventLog:
    async def test_a_bound_writer_hands_out_the_marker_seq(self, tmp_path) -> None:
        class Log:
            def __init__(self) -> None:
                self.reserved = 0

            async def reserve_seq(self) -> int:
                self.reserved += 1
                return 5_000

        log = Log()
        backend, workspace, session = await open_session(tmp_path)
        try:
            for i in range(6):
                await append_messages(workspace, session, user_message(chr(65 + i) * 120_000), assistant_message(f"r{i}"))
            await append_messages(workspace, session, user_message("Q"))
            llm = ScriptedLLM()
            llm.session_id = session.session_id
            llm.extend([Events(text_events("SUMMARY")), Events(text_events("done"))])
            await run_turn(session, llm, configure=lambda executor: executor.bind_event_log(log))
            markers = [r for r in _lines(workspace, session) if r.get("kind") == "compaction_marker"]
            assert [m["seq"] for m in markers] == [5_000] and log.reserved == 1
        finally:
            await session.aclose()
            await backend.aclose()


def _persisted_round(i: int, chars: int) -> list[Message]:
    """A tool round as an earlier part of the SAME turn left it in the history (a park persisted it)."""
    return [
        Message(role="assistant", parts=[ToolCallPart(id=f"p{i}", name="workspace__exec", arguments={"command": "true"})]),
        Message(role="tool", parts=[ToolResultPart(id=f"p{i}", output="x" * chars)]),
    ]


def _answered_in(messages) -> bool:
    calls, results = _tool_ids(messages)
    return sorted(calls) == sorted(results)


def _markers(workspace, session) -> list[dict]:
    return [r for r in _lines(workspace, session) if r.get("kind") == "compaction_marker"]


class TestAResumedTurnThatOverflows:
    """A resumed turn is [question, k rounds a park persisted]. The proactive compaction leaves [question, summary 1,
    newest round]; an overflow of the turn's own call then makes the FORCED compaction the SECOND one of the turn,
    which used to fold the question into the summary ([S2, newest], no question)."""

    async def test_a_proactive_compaction_then_an_overflow_keeps_the_question_in_the_replay_and_in_marker_two(
        self, tmp_path,
    ) -> None:
        backend, workspace, session = await open_session(tmp_path)
        try:
            for i in range(3):
                await append_messages(workspace, session, user_message(f"old {i}"), assistant_message(f"old reply {i}"))
            await append_messages(
                workspace, session, user_message(QUESTION), *[m for i in range(30) for m in _persisted_round(i, 12_000)],
            )
            state = {"summaries": 0, "overflowed": False}

            def fn(n, messages, kwargs):
                if _is_summariser(kwargs):
                    state["summaries"] += 1
                    return text_events(f"SUMMARY-{state['summaries']}")
                if not state["overflowed"]:
                    state["overflowed"] = True
                    return BadRequestError(OVERFLOW)
                return text_events("done")

            llm = FnLLM(fn)
            await run_turn(session, llm)

            markers = _markers(workspace, session)
            assert len(markers) == 2, "the proactive compaction's marker and the forced one"
            second = markers[1]["payload"]
            kept = [_text(Message.model_validate(m)) for m in second["kept_tail_messages"]]
            assert kept[0] == QUESTION, "marker two folded the question into its summary"
            assert second["summary_after"] == 1 and second["summary"].endswith("SUMMARY-2")

            replay = [_text(m) for m in llm.calls[-1]["messages"] if m.role != "system"]
            assert replay[0] == QUESTION and replay[1].endswith("SUMMARY-2"), "the replay was sent the question first"
            assert not any("SUMMARY-1" in t for t in replay), "summary 1 is folded into summary 2"

            reloaded = [_text(m) for m in await _reload(session)]
            assert reloaded[0] == QUESTION and reloaded[1].endswith("SUMMARY-2") and reloaded[-1] == "done"
        finally:
            await session.aclose()
            await backend.aclose()


_FAT_TOOL = Tool(
    id="fat__schema", toolset_id="fat", description="d" * 38_000,
    args_schema={"type": "object", "properties": {"a": {"type": "string", "description": "x" * 38_000}}},
)


class _FatCatalogue:
    """The real tool manager with one more tool whose schema is large: a fixed part like the builder agent's."""

    def __init__(self, inner) -> None:
        self._inner = inner

    def __getattr__(self, name):
        return getattr(self._inner, name)

    async def list_tools(self, **kwargs):
        return [*await self._inner.list_tools(**kwargs), _FAT_TOOL]


def _big_call(call_id: str, chars: int = 20_000) -> list:
    """A call whose result is ``chars`` characters (the exec tool's own envelope caps it near 50 KiB)."""
    return [
        ToolCallStart(id=call_id, name="workspace__exec", index=0),
        ToolCallEnd(
            id=call_id,
            arguments={"command": f"echo a >> counter.txt; head -c {chars} /dev/zero | tr '\\0' x", "description": "big"},
            index=0,
        ),
        Done(stop_reason="tool_use", raw_reason="tool_use"),
    ]


@POSIX
class TestTheFixedPartInTheReplay:
    """The builder shape: a fixed part of about 22k tokens in a 32k window (budget 23,808). Whatever the turn reads,
    only about 1.8k tokens of messages can ever fit beside it, so the newest folded round has to be cut to its
    placeholders. The round here is about 5k tokens: under what folding the rounds leaves of any round (7.1k), so
    nothing but the cap cuts it; left whole it makes the forced compaction protected_over_budget at once, and a turn
    that could be continued fails."""

    WINDOW = ResolvedModel(
        profile_id="golden-profile", provider_id="golden-provider", model_name="golden-model",
        context_length=32_000, config=ModelProfileConfig(),
    )

    async def test_an_overflow_with_a_newest_round_the_fixed_part_leaves_no_room_for_is_continued_not_failed(self, tmp_path) -> None:
        backend, workspace, session = await open_session(tmp_path)
        try:
            for i in range(2):
                await append_messages(workspace, session, user_message(f"old {i}"), assistant_message(f"old reply {i}"))
            await append_messages(workspace, session, user_message(QUESTION))
            fixed = await make_executor(session, ScriptedLLM(), wrap_tools=_FatCatalogue).fixed_overhead_tokens()
            assert 21_000 <= fixed < 23_000, f"not the builder shape: a fixed part of {fixed} tokens"
            state = {"overflowed": False}

            def fn(n, messages, kwargs):
                if _is_summariser(kwargs):
                    return text_events("SUMMARY")
                if not _has_result(messages, "call_a"):
                    return _big_call("call_a")
                if not state["overflowed"]:
                    state["overflowed"] = True
                    return BadRequestError(OVERFLOW)
                return text_events("done")

            llm = FnLLM(fn)
            await run_turn(session, llm, llm_model=self.WINDOW, wrap_tools=_FatCatalogue)   # no ContextOverflowUnrecoverable

            assert _counter(workspace) == ["a"], "the tool ran once"
            replay = [m for m in llm.calls[-1]["messages"] if m.role != "system"]
            size = CompactionStrategy._estimate_tokens  # noqa: SLF001
            budget = CompactionStrategy()._effective_budget(self.WINDOW)  # noqa: SLF001
            assert fixed + size(replay) < budget, f"the replay is {fixed} + {size(replay)} tokens against a budget of {budget}"
            assert QUESTION in [_text(m) for m in replay]
            outputs = [p.output for m in replay for p in m.parts if isinstance(p, ToolResultPart)]
            assert len(outputs) == 1 and "ALREADY RAN" in outputs[0] and len(outputs[0]) < 2_000
            assert [m["payload"]["outcome"] for m in _markers(workspace, session)] in (["summarised"], ["insufficient"])
            assert [_text(m) for m in await _reload(session)][-1] == "done"
        finally:
            await session.aclose()
            await backend.aclose()


class _ParksOn:
    """The real tool manager, except that one call hands the turn back to the worker (a yielding tool)."""

    def __init__(self, inner, call_id: str) -> None:
        self._inner, self._call_id = inner, call_id

    def __getattr__(self, name):
        return getattr(self._inner, name)

    async def execute(self, call, **kwargs):
        if call.id == self._call_id:
            raise YieldToWorker(Yielded(tool_name=call.name, event_key="k", resume_metadata={}), tool_call_id=call.id)
        return await self._inner.execute(call, **kwargs)


@POSIX
class TestAReplayThatParks:
    """The replay is a normal turn: a tool in it can yield. The park stamps what the replay ran; the rounds the
    rejected attempt ran are in the compaction marker; the resume appends to the history after the marker."""

    @staticmethod
    def _model(state):
        def fn(n, messages, kwargs):
            if _is_summariser(kwargs):
                return text_events("SUMMARY")
            if not _has_result(messages, "call_a"):
                return _call("call_a", "a")
            if not state["overflowed"]:
                state["overflowed"] = True
                return BadRequestError(OVERFLOW)
            if not _has_result(messages, "call_park"):
                return _call("call_park", "park")
            return text_events("done")

        return fn

    async def _park(self, tmp_path, *, steer: str | None = None):
        backend, workspace, session = await open_session(tmp_path)
        await _seed(workspace, session)
        state = {"overflowed": False}
        fn = self._model(state)
        entered, release = asyncio.Event(), asyncio.Event()
        if steer is not None:
            plain = fn

            def fn(n, messages, kwargs):  # noqa: F811 - the summariser call waits for the steer to land
                if _is_summariser(kwargs):
                    return Gate(entered, release, text_events("SUMMARY"))
                return plain(n, messages, kwargs)

        llm = FnLLM(fn)
        task = asyncio.create_task(run_turn(session, llm, wrap_tools=lambda manager: _ParksOn(manager, "call_park")))
        if steer is not None:
            await asyncio.wait_for(entered.wait(), timeout=30)
            await session.append_instruction(steer)   # deferred by the compaction window, drained after the marker
            release.set()
        with pytest.raises(YieldToWorker) as parked:
            await asyncio.wait_for(task, timeout=30)
        return backend, workspace, session, parked.value

    async def test_the_park_stamps_only_the_replays_rounds_and_the_resumed_history_holds_every_call_once(
        self, tmp_path,
    ) -> None:
        backend, workspace, session, parked = await self._park(tmp_path, steer="STEER-DURING-THE-COMPACTION")
        try:
            assert _counter(workspace) == ["a"], "the parked call did not run, and call_a ran once"
            stamped = parked.llm_messages
            assert _tool_ids(stamped) == (["call_park"], []), "only the replay's own round: call_a is in the marker"

            result = Message(role="tool", parts=[ToolResultPart(id="call_park", output="resumed")])
            await make_executor(session, ScriptedLLM()).inject_resume_messages([*stamped, result])

            shown = await _reload(session)
            texts = [_text(m) for m in shown]
            steer = "STEER-DURING-THE-COMPACTION"

            def at(call_id: str, role: str) -> int:
                return next(
                    i for i, m in enumerate(shown)
                    if m.role == role and any(getattr(p, "id", None) == call_id for p in m.parts)
                )

            assert any(t.endswith("SUMMARY") and "earlier conversation compacted" in t for t in texts), "the marker's summary"
            assert _tool_ids(shown) == (["call_a", "call_park"], ["call_a", "call_park"]), "every call and result once"
            assert texts.count(steer) == 1 and texts.count(QUESTION) == 1
            assert (
                texts.index(QUESTION) < at("call_a", "assistant") < at("call_a", "tool") < texts.index(steer)
                < at("call_park", "assistant") < at("call_park", "tool") == len(shown) - 1
            ), "[question, the carried round, the steer, the stamped call, its result]: the steer landed after the marker"
        finally:
            await session.aclose()
            await backend.aclose()

    async def test_the_next_compaction_after_the_resume_keeps_the_question(self, tmp_path) -> None:
        """After the resume the history holds the marker's summary, the question, the carried round, the stamped call
        and its result. A compaction in the next turn (an overflow, or the trigger) is the turn's THIRD: it must still
        see the question as the unanswered input (not fold it) and fold the summary into the new one."""
        backend, workspace, session, parked = await self._park(tmp_path)
        try:
            result = Message(role="tool", parts=[ToolResultPart(id="call_park", output="resumed")])
            await make_executor(session, ScriptedLLM()).inject_resume_messages([*parked.llm_messages, result])
            history = await _reload(session)

            class Summariser:
                def stream(self, **kwargs):
                    return FnLLM(lambda n, m, k: text_events("SUMMARY-3")).stream(**kwargs)

            forced = await CompactionStrategy().force_compact(
                agent=make_executor(session, ScriptedLLM())._agent,  # noqa: SLF001 - the agent the executor was built with
                llm=Summariser(), model=make_executor(session, ScriptedLLM())._model,  # noqa: SLF001
                history=history,
            )
            out = [_text(m) for m in forced.new_messages]
            assert out.count(QUESTION) == 1, "the question stayed verbatim in what the compaction kept"
            summaries = [t for t in out if "earlier conversation compacted" in t]
            assert len(summaries) == 1 and summaries[0].endswith("SUMMARY-3"), "the old summary is folded into the new one"
            assert _tool_ids(forced.new_messages)[0][-1] == "call_park", "the newest round stays, whole"
            assert _answered_in(forced.new_messages), "no tool call without its result"
        finally:
            await session.aclose()
            await backend.aclose()


@POSIX
class TestACompactionThatCanChangeNothing:
    """``unreducible``: nothing could be summarised (here the fixed part fills the window, or there is nothing
    before the question). The replay would send the prompt that was just rejected, so the turn ends by name; the
    rounds it completed are not lost: no marker was written, so they stay in the record for the chokepoint."""

    TINY = ResolvedModel(
        profile_id="golden-profile", provider_id="golden-provider", model_name="golden-model",
        context_length=4_000, config=ModelProfileConfig(),
    )

    async def test_with_a_completed_round_it_fails_by_name_without_a_replay_and_the_round_is_recorded_once(
        self, tmp_path,
    ) -> None:
        backend, workspace, session = await open_session(tmp_path)
        try:
            await _seed(workspace, session, n=2)
            llm = FnLLM(_reactive())                                  # round a, then the next call overflows
            with pytest.raises(ContextOverflowUnrecoverable) as failed:
                await run_turn(session, llm, llm_model=self.TINY)
            error = failed.value
            assert "fixed_over_budget" in str(error) and isinstance(error.__cause__, BadRequestError)
            assert (error.forced_compaction, error.replay_attempted, error.persisted_rounds) == (False, False, 1)
            assert [c for c in llm.calls if _is_summariser(c["kwargs"])] == [], "nothing was summarised"
            assert len(llm.calls) == 2, "the tool call and the rejected call: no replay of the rejected prompt"
            assert _counter(workspace) == ["a"]
            assert _tool_ids(await _reload(session)) == (["call_a"], ["call_a"]), "recorded once, by the chokepoint"
        finally:
            await session.aclose()
            await backend.aclose()

    async def test_with_nothing_run_and_an_estimate_under_the_budget_it_still_does_not_replay_an_identical_prompt(
        self, tmp_path,
    ) -> None:
        """The question and nothing before it: empty_head. The provider rejected a prompt our estimate puts far under
        the budget (an image or a document is a flat guess), and the replay would be that same prompt."""
        backend, workspace, session = await open_session(tmp_path)
        try:
            await append_messages(workspace, session, user_message(QUESTION))
            llm = FnLLM(lambda n, messages, kwargs: BadRequestError(OVERFLOW))
            with pytest.raises(ContextOverflowUnrecoverable) as failed:
                await run_turn(session, llm)
            assert len(llm.calls) == 1, "no second call"
            assert "empty_head" in str(failed.value) and "undercounts" in str(failed.value)
        finally:
            await session.aclose()
            await backend.aclose()


@POSIX
class TestWhatTheTypedFailureSaysAboutTheRounds:
    """``persisted_rounds`` is the rounds in the history as messages, ``summarised_rounds`` those only in the
    compaction's summary: both are final once the chokepoint has written (or failed to write)."""

    async def test_rounds_the_compaction_summarised_are_not_counted_as_persisted(self, tmp_path) -> None:
        """Three rounds ran, then the call overflowed. With a fixed part like the builder agent's the forced compaction
        keeps only the newest round whole and summarises the other two; the replay is rejected too."""
        backend, workspace, session = await open_session(tmp_path)
        try:
            await _seed(workspace, session, n=2)
            state = {"overflowed": False}

            def fn(n, messages, kwargs):
                if _is_summariser(kwargs):
                    return text_events("SUMMARY")
                if state["overflowed"]:
                    return BadRequestError(OVERFLOW)          # the replay is rejected too
                for call_id in ("call_a", "call_b", "call_c"):
                    if not _has_result(messages, call_id):
                        return _call(call_id, call_id[-1])
                state["overflowed"] = True
                return BadRequestError(OVERFLOW)

            with pytest.raises(ContextOverflowUnrecoverable) as failed:
                await run_turn(
                    session, FnLLM(fn), llm_model=TestTheFixedPartInTheReplay.WINDOW, wrap_tools=_FatCatalogue,
                )
            assert _counter(workspace) == ["a", "b", "c"], "each tool ran once"
            error = failed.value
            assert (error.persisted_rounds, error.summarised_rounds) == (1, 2), error.problem_extensions
            assert _tool_ids(await _reload(session)) == (["call_c"], ["call_c"]), "only the newest round is a message"
        finally:
            await session.aclose()
            await backend.aclose()

    async def test_a_write_that_fails_is_not_counted(self, tmp_path) -> None:
        backend, workspace, session = await open_session(tmp_path)
        try:
            await _seed(workspace, session)
            state = {"first": True}

            def fn(n, messages, kwargs):
                if _is_summariser(kwargs):
                    return text_events("SUMMARY")
                if not _has_result(messages, "call_a"):
                    return _call("call_a", "a")
                if state["first"]:
                    state["first"] = False
                    return BadRequestError(OVERFLOW)
                if not _has_result(messages, "call_b"):
                    return _call("call_b", "b")
                return BadRequestError(OVERFLOW)

            def break_the_write(executor) -> None:
                async def refuse(messages):
                    raise OSError("the mount is gone")

                executor._persist_turn = refuse  # noqa: SLF001 - the write the chokepoint makes

            with pytest.raises(ContextOverflowUnrecoverable) as failed:
                await run_turn(session, FnLLM(fn), configure=break_the_write)
            assert failed.value.persisted_rounds == 1, "round a is in the marker; round b could not be written"
        finally:
            await session.aclose()
            await backend.aclose()


@POSIX
class TestTheChokepointAppliesTheGuardsReductionsOverTheReplaysHistory:
    async def test_the_recorded_prune_set_is_applied_to_the_history_it_was_recorded_against(self, tmp_path, monkeypatch) -> None:
        """What the guard reduced is keyed by occurrence in the prompt it saw (the replay's history and then its
        rounds), so the chokepoint has to apply it over that same history, not over the rounds alone."""
        import primer.agent.base as base

        seen: list[list] = []
        real = base.reduce_for_persist

        def spy(rounds, **kwargs):
            seen.append(list(kwargs.get("context", ())))
            return real(rounds, **kwargs)

        monkeypatch.setattr(base, "reduce_for_persist", spy)
        backend, workspace, session = await open_session(tmp_path)
        try:
            await _seed(workspace, session)
            state = {"first": True}

            def fn(n, messages, kwargs):
                if _is_summariser(kwargs):
                    return text_events("SUMMARY")
                if not _has_result(messages, "call_a"):
                    return _call("call_a", "a")
                if state["first"]:
                    state["first"] = False
                    return BadRequestError(OVERFLOW)           # attempt 1 is rejected after round a
                if not _has_result(messages, "call_b"):
                    return _call("call_b", "b")                # the replay runs round b ...
                return BadRequestError(OVERFLOW)               # ... and is rejected again: the chokepoint writes b

            with pytest.raises(ContextOverflowUnrecoverable):
                await run_turn(session, FnLLM(fn))
            history = [_text(m) for m in seen[-1]]            # the call the chokepoint made
            assert QUESTION in history and any("earlier conversation compacted" in t for t in history), (
                "the replay's history (the compaction's summary and the question) is the context"
            )
        finally:
            await session.aclose()
            await backend.aclose()


@POSIX
class TestALostLeaseWritesNothing:
    @staticmethod
    async def _cancelled_with(tmp_path, reason: str | None):
        backend, workspace, session = await open_session(tmp_path)
        try:
            await _seed(workspace, session)
            entered, release = asyncio.Event(), asyncio.Event()

            def fn(n, messages, kwargs):
                if not _has_result(messages, "call_a"):
                    return _call("call_a", "a")
                return Gate(entered, release, text_events("never"))

            task = asyncio.create_task(run_turn(session, FnLLM(fn)))
            await asyncio.wait_for(entered.wait(), timeout=30)
            task.cancel(reason) if reason is not None else task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            return await _reload(session)
        finally:
            await session.aclose()
            await backend.aclose()

    async def test_a_cancel_that_says_the_lease_was_lost_does_not_record_the_rounds(self, tmp_path) -> None:
        """The session may belong to another worker by now: a write from here could interleave with its own."""
        from primer.model.yield_ import CANCEL_REASON_PREEMPTED

        shown = await self._cancelled_with(tmp_path, CANCEL_REASON_PREEMPTED)
        assert _tool_ids(shown) == ([], []), "the completed round was written by a worker that no longer owned the session"

    async def test_any_other_cancel_still_records_them(self, tmp_path) -> None:
        for reason in (None, "user_signal", "worker_drain_timeout"):
            shown = await self._cancelled_with(tmp_path / str(reason), reason)
            assert _tool_ids(shown) == (["call_a"], ["call_a"]), f"cancel reason {reason!r}"
