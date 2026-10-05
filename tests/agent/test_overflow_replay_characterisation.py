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

(The overflow classifier and the cap results of ``max_tool_turns`` are other units: tasks 01a108b1-2480 and
01a108b1-47d7. The summariser's own overflow is recovered inside the compaction, and is covered by
``test_summariser_overflow_recovery.py``.)
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
from tests._support.provider_history import assert_anthropic_valid, assert_openai_valid
from tests._support.off_golden import (
    CONTEXT_LENGTH, Events, FnLLM, Gate, ScriptedLLM, append_messages, assistant_message, make_executor, open_session,
    run_turn, text_events, user_message,
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
            # The cap answers the call it does not run (it used to leave a tool_use with no result, a 400 on every
            # later request), so what the turn persisted is a history both provider families accept.
            shown = await _reload(session)
            assert _tool_ids(shown) == (["call_a", "call_b"], ["call_a", "call_b"]), "call_b is answered, not dangling"
            assert_anthropic_valid(shown)
            assert_openai_valid(shown)
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
class TestAnOutputCapThatFillsTheWindow:
    """The agent's own ``max_output_tokens`` is not below the model's window: the guard at the top of
    ``_recover_from_overflow`` ends the turn before any compaction, whatever the provider's rejection says."""

    async def test_a_turn_that_ran_a_round_ends_without_compacting_and_keeps_the_round_once(self, tmp_path) -> None:
        """The model is only rejected once the round's result is in its prompt, so the guard fires AFTER a tool ran. The
        typed failure says recovery got nowhere (no compaction, no replay), and the completed round is neither lost
        (the guard raises before anything is folded, so the record still holds it) nor written twice: the one
        chokepoint writes it on the way out."""
        def cap_fills_the_window(executor) -> None:
            executor._agent = executor._agent.model_copy(update={"max_output_tokens": CONTEXT_LENGTH})  # noqa: SLF001

        backend, workspace, session = await open_session(tmp_path)
        try:
            await _seed(workspace, session)
            llm = FnLLM(_reactive())
            with pytest.raises(ContextOverflowUnrecoverable) as failed:
                await run_turn(session, llm, configure=cap_fills_the_window)
            error = failed.value
            assert error.code == "context_overflow_unrecoverable" and isinstance(error.__cause__, BadRequestError)
            assert error.problem_extensions == {
                "forced_compaction": False, "replay_attempted": False, "persisted_rounds": 1, "summarised_rounds": 0,
            }
            assert _counter(workspace) == ["a"], "the tool ran once"
            assert len(llm.calls) == 2, "the round, then the rejected call: no summariser call and no replay"
            shown = await _reload(session)
            assert _tool_ids(shown) == (["call_a"], ["call_a"]), "the completed round is persisted, exactly once"
            assert [_text(m) for m in shown].count(QUESTION) == 1 and "done" not in [_text(m) for m in shown]
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


class _HugeResult:
    """The real tool manager, except that one call comes back with a huge result (more than any envelope allows)."""

    def __init__(self, inner, call_ids: tuple[str, ...], chars: int, ran: list[str]) -> None:
        self._inner, self._call_ids, self._chars, self._ran = inner, call_ids, chars, ran

    def __getattr__(self, name):
        return getattr(self._inner, name)

    async def execute(self, call, **kwargs):
        if call.id in self._call_ids:
            self._ran.append(call.id)
            return ToolResultPart(id=call.id, output="x" * self._chars)
        return await self._inner.execute(call, **kwargs)


@POSIX
class TestAFreshSessionThatOverflowsAfterOneBigRound:
    """No history before the question, so the forced compaction has nothing to summarise (``empty_head``). That is
    not the end of the turn: folding the round the turn ran SHRANK it (our own reduction, ALREADY RAN placeholders),
    so the replay does not send the prompt that was rejected and has every chance to fit. The turn continues without a
    marker: the reduced round stays in the record, where the persistence chokepoint and a normal finish write it."""

    async def test_a_single_huge_round_is_reduced_and_the_turn_continues(self, tmp_path) -> None:
        backend, workspace, session = await open_session(tmp_path)
        try:
            await append_messages(workspace, session, user_message(QUESTION))     # [hello, question]: nothing before them
            ran: list[str] = []
            llm = FnLLM(_reactive())
            await run_turn(session, llm, wrap_tools=lambda m: _HugeResult(m, ("call_a",), 600_000, ran))   # ~150k tokens
            assert ran == ["call_a"], "the tool ran once"
            size = CompactionStrategy._estimate_tokens  # noqa: SLF001
            replay = [m for m in llm.calls[-1]["messages"] if m.role != "system"]
            assert size(replay) < 10_000, "the replay was sent the REDUCED round, not ~150k tokens"
            shown = await _reload(session)
            assert _tool_ids(shown) == (["call_a"], ["call_a"]), "the round is in the history once"
            outputs = [p.output for m in shown for p in m.parts if isinstance(p, ToolResultPart)]
            assert len(outputs) == 1 and len(outputs[0]) < 10_000 and "ALREADY RAN" in outputs[0], "persisted reduced"
            assert _text(shown[-1]) == "done" and _markers(workspace, session) == [], "no marker: nothing was summarised"
        finally:
            await session.aclose()
            await backend.aclose()

    async def test_a_second_overflow_after_the_continued_replay_records_both_reduced_rounds_once(
        self, tmp_path, monkeypatch,
    ) -> None:
        """Round a is huge and is reduced by the fold; the replay (continued without a marker) runs round b, also huge,
        and is rejected again. Both rounds are in the record in their reduced form, the chokepoint writes each once,
        and the recorded prune set is applied over the prompt BEFORE the record's own rounds (the reduced round a is
        in the record as well as at the end of the compacted history; counting it twice would shift the keys)."""
        import primer.agent.base as base

        contexts: list[list] = []
        real = base.reduce_for_persist

        def spy(rounds, **kwargs):
            contexts.append(list(kwargs.get("context", ())))
            return real(rounds, **kwargs)

        monkeypatch.setattr(base, "reduce_for_persist", spy)
        backend, workspace, session = await open_session(tmp_path)
        try:
            await append_messages(workspace, session, user_message(QUESTION))
            ran: list[str] = []
            state = {"overflowed": False}

            def fn(n, messages, kwargs):
                if _is_summariser(kwargs):
                    return text_events("SUMMARY")
                if not _has_result(messages, "call_a"):
                    return _call("call_a", "a")
                if not state["overflowed"]:
                    state["overflowed"] = True
                    return BadRequestError(OVERFLOW)
                if not _has_result(messages, "call_b"):
                    return _call("call_b", "b")               # the continued replay runs a second huge round ...
                return BadRequestError(OVERFLOW)              # ... and is rejected again

            with pytest.raises(ContextOverflowUnrecoverable) as failed:
                await run_turn(session, FnLLM(fn), wrap_tools=lambda m: _HugeResult(m, ("call_a", "call_b"), 600_000, ran))
            error = failed.value
            assert ran == ["call_a", "call_b"], "each tool ran once"
            assert (error.forced_compaction, error.replay_attempted, error.persisted_rounds) == (True, True, 2)
            shown = await _reload(session)
            assert _tool_ids(shown) == (["call_a", "call_b"], ["call_a", "call_b"]), "both rounds, once each"
            outputs = [p.output for m in shown for p in m.parts if isinstance(p, ToolResultPart)]
            assert all(len(o) < 10_000 for o in outputs), "neither was persisted raw (600k characters)"
            assert not any(isinstance(p, ToolResultPart) for m in contexts[-1] for p in m.parts), (
                "the context holds no round of the record's own"
            )
        finally:
            await session.aclose()
            await backend.aclose()

    async def test_the_builder_shape_with_no_history_continues_too(self, tmp_path) -> None:
        """A 22k fixed part in a 32k window, a 5k-token round, nothing seeded: the round is cut to its placeholders
        by the cap and the replay fits. (Seeded with older exchanges the same turn already continued, through a
        marker; this is the case that used to fail.)"""
        backend, workspace, session = await open_session(tmp_path)
        try:
            await append_messages(workspace, session, user_message(QUESTION))
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
            await run_turn(session, llm, llm_model=TestTheFixedPartInTheReplay.WINDOW, wrap_tools=_FatCatalogue)
            assert _counter(workspace) == ["a"] and _text((await _reload(session))[-1]) == "done"
            assert [c for c in llm.calls if _is_summariser(c["kwargs"])] == [], "nothing to summarise: no summariser call"
            outputs = [p.output for m in llm.calls[-1]["messages"] for p in m.parts if isinstance(p, ToolResultPart)]
            assert len(outputs) == 1 and "ALREADY RAN" in outputs[0] and len(outputs[0]) < 2_000
        finally:
            await session.aclose()
            await backend.aclose()


@POSIX
class TestAReplayThatParksWithoutAMarker:
    """Without a marker the rounds the rejected attempt ran are NOT in a compaction: they stay in the turn's record, in
    the reduced form the replay is sent. A replay that then parks stamps them too (``llm_messages`` is the record), so
    the resume appends the whole stamped slice: the carried round (reduced) and the parking call."""

    async def test_the_park_stamps_the_rejected_attempts_reduced_rounds_and_the_resume_holds_every_call_once(
        self, tmp_path,
    ) -> None:
        backend, workspace, session = await open_session(tmp_path)
        try:
            await append_messages(workspace, session, user_message(QUESTION))      # nothing before the question
            ran: list[str] = []
            state = {"overflowed": False}

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

            llm = FnLLM(fn)
            with pytest.raises(YieldToWorker) as parked:
                await run_turn(
                    session, llm,
                    wrap_tools=lambda m: _ParksOn(_HugeResult(m, ("call_a",), 600_000, ran), "call_park"),
                )
            assert ran == ["call_a"] and _markers(workspace, session) == [], "the round ran once; nothing was summarised"
            assert [c for c in llm.calls if _is_summariser(c["kwargs"])] == [], "nothing to summarise: no summariser call"

            stamped = parked.value.llm_messages
            assert _tool_ids(stamped) == (["call_a", "call_park"], ["call_a"]), (
                "unlike the marker path, the rejected attempt's round is stamped too, with the parking call after it"
            )
            carried = [p.output for m in stamped for p in m.parts if isinstance(p, ToolResultPart)]
            assert len(carried) == 1 and len(carried[0]) < 10_000 and "ALREADY RAN" in carried[0], "in its reduced form"

            result = Message(role="tool", parts=[ToolResultPart(id="call_park", output="resumed")])
            await make_executor(session, ScriptedLLM()).inject_resume_messages([*stamped, result])
            shown = await _reload(session)
            assert _tool_ids(shown) == (["call_a", "call_park"], ["call_a", "call_park"]), "every call and result once"
            assert [_text(m) for m in shown].count(QUESTION) == 1
            assert_anthropic_valid(shown)
        finally:
            await session.aclose()
            await backend.aclose()

@POSIX
class TestACompactionSummaryIsNeverAMessageLine:
    """``CompactionSummary`` is a tag that does not survive JSON: written as a message line it would come back as a reply
    the model wrote, and ``pending_from`` would stop looking through it. Its durable form is the marker."""

    async def test_persisting_one_is_refused_and_writes_nothing(self, tmp_path) -> None:
        from primer.model.chat import CompactionSummary

        backend, workspace, session = await open_session(tmp_path)
        try:
            await append_messages(workspace, session, user_message(QUESTION))
            before = (await _reload(session))
            executor = make_executor(session, ScriptedLLM())
            summary = CompactionSummary(role="assistant", parts=[TextPart(text="[earlier conversation compacted] ...")])
            with pytest.raises(ValueError, match="CompactionSummary"):
                await executor._persist_turn([summary])  # noqa: SLF001
            with pytest.raises(ValueError, match="CompactionSummary"):
                await executor.inject_resume_messages([summary])
            assert [(_text(m), m.role) for m in await _reload(session)] == [(_text(m), m.role) for m in before]
        finally:
            await session.aclose()
            await backend.aclose()

    def test_the_guard_looks_at_the_type_not_the_text(self) -> None:
        from primer.agent.base import refuse_compaction_summaries
        from primer.model.chat import CompactionSummary

        refuse_compaction_summaries([user_message("hi"), assistant_message("[earlier conversation compacted on x] y")], "x")
        with pytest.raises(ValueError, match="a parked state"):
            refuse_compaction_summaries([CompactionSummary(role="assistant", parts=[TextPart(text="s")])], "a parked state")

    async def test_a_park_that_would_stamp_one_fails_loudly_instead(self, tmp_path, monkeypatch) -> None:
        """If something ever put a summary among the turn's own messages, the park must not carry it into the parked
        state (it would come back as a model reply)."""
        import primer.agent.loop as loop
        from primer.model.chat import CompactionSummary

        real = loop.output_to_message

        def tagged(buffered):
            message = real(buffered)
            return CompactionSummary(**message.model_dump()) if message.role == "assistant" else message

        monkeypatch.setattr(loop, "output_to_message", tagged)
        backend, workspace, session = await open_session(tmp_path)
        try:
            await append_messages(workspace, session, user_message(QUESTION))
            llm = FnLLM(lambda n, messages, kwargs: _call("call_park", "park"))
            with pytest.raises(ValueError, match="a parked state"):
                await run_turn(session, llm, wrap_tools=lambda m: _ParksOn(m, "call_park"))
        finally:
            await session.aclose()
            await backend.aclose()

    async def test_a_replay_that_parks_stamps_no_summary_and_the_history_has_none_as_a_line(self, tmp_path) -> None:
        """The recovery builds a summary in memory (the compacted history); what a park stamps and what is persisted are
        the turn's own messages only."""
        from primer.model.chat import CompactionSummary

        harness = TestAReplayThatParks()
        backend, workspace, session, parked = await harness._park(tmp_path)  # noqa: SLF001
        try:
            assert not any(isinstance(m, CompactionSummary) for m in parked.llm_messages)
            texts = [_text(m) for m in await _reload(session) if not isinstance(m, CompactionSummary)]
            assert not any(t.startswith("[earlier conversation compacted") for t in texts), (
                "the summary is in the history only as the marker's, never as a message line"
            )
            lines = [line for line in _lines(workspace, session) if "role" in line]
            assert not any(
                p.get("text", "").startswith("[earlier conversation compacted") for line in lines for p in line["parts"]
            )
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
            assert (error.forced_compaction, error.replay_attempted, error.persisted_rounds) == (True, False, 1)
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
class TestAHardCancelDuringTheMarkerCommit:
    async def test_the_rounds_are_in_the_history_once_not_twice(self, tmp_path) -> None:
        """The forced compaction's marker holds the rounds the rejected attempt ran. A hard cancel that lands after the
        commit but before ``invoke`` has accounted for it must not leave the record holding rounds the marker already
        has: the chokepoint would write them again and every tool_use id would be in the history twice (a 400 on every
        later request). The commit runs under a shield and the record is told before the cancel goes on."""
        backend, workspace, session = await open_session(tmp_path)
        try:
            await _seed(workspace, session)
            entered, release = asyncio.Event(), asyncio.Event()

            def gate_after_the_commit(executor) -> None:
                original = executor._replace_compacted_head  # noqa: SLF001

                async def gated(*args, **kwargs):
                    result = await original(*args, **kwargs)     # the marker has landed ...
                    entered.set()
                    await release.wait()                          # ... and the turn is cancelled before it returns
                    return result

                executor._replace_compacted_head = gated  # noqa: SLF001

            task = asyncio.create_task(run_turn(session, FnLLM(_reactive()), configure=gate_after_the_commit))
            await asyncio.wait_for(entered.wait(), timeout=30)
            task.cancel()
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert len(_markers(workspace, session)) == 1, "the commit was allowed to finish"
            shown = await _reload(session)
            assert _tool_ids(shown) == (["call_a"], ["call_a"]), "the round is in the history once"
            assert_anthropic_valid(shown)
        finally:
            await session.aclose()
            await backend.aclose()

    async def test_a_second_cancel_while_waiting_for_the_commit_does_not_make_the_rounds_land_twice(self, tmp_path) -> None:
        """The shield keeps the commit running through the first cancel, and the handler then waits for it. A SECOND
        cancel lands on that wait: it cancels the await, not the write (the marker is written by a thread and lands
        whatever the task does). Reading that cancel as "the commit failed, nothing landed" left the record holding
        rounds the marker already has, and the chokepoint wrote them again."""
        backend, workspace, session = await open_session(tmp_path)
        try:
            await _seed(workspace, session)
            entered, release = asyncio.Event(), asyncio.Event()

            def gate_after_the_commit(executor) -> None:
                original = executor._replace_compacted_head  # noqa: SLF001

                async def gated(*args, **kwargs):
                    result = await original(*args, **kwargs)
                    entered.set()
                    await release.wait()
                    return result

                executor._replace_compacted_head = gated  # noqa: SLF001

            task = asyncio.create_task(run_turn(session, FnLLM(_reactive()), configure=gate_after_the_commit))
            await asyncio.wait_for(entered.wait(), timeout=30)
            task.cancel()
            await asyncio.sleep(0.05)        # the first cancel is delivered: the handler is now waiting for the commit
            task.cancel()                    # a second one lands on that wait
            await asyncio.sleep(0.05)
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert len(_markers(workspace, session)) == 1, "the commit was allowed to finish"
            shown = await _reload(session)
            assert _tool_ids(shown) == (["call_a"], ["call_a"]), "the round is in the history once"
            assert_anthropic_valid(shown)
        finally:
            await session.aclose()
            await backend.aclose()

    async def test_a_commit_that_fails_while_the_turn_is_cancelled_is_logged_and_the_rounds_are_written_once(
        self, tmp_path, caplog,
    ) -> None:
        """If the marker never lands (the commit raises under the cancel), nothing is folded: the record keeps the rounds
        and the chokepoint writes them, once, with no marker; the failure is in the log, not swallowed."""
        import logging

        backend, workspace, session = await open_session(tmp_path)
        try:
            await _seed(workspace, session)
            entered, release = asyncio.Event(), asyncio.Event()

            def failing_commit(executor) -> None:
                async def failing(*args, **kwargs):
                    entered.set()
                    await release.wait()
                    raise OSError("the workspace mount went away")

                executor._replace_compacted_head = failing  # noqa: SLF001

            with caplog.at_level(logging.WARNING, logger="primer.agent.base"):
                task = asyncio.create_task(run_turn(session, FnLLM(_reactive()), configure=failing_commit))
                await asyncio.wait_for(entered.wait(), timeout=30)
                task.cancel()
                await asyncio.sleep(0.05)
                release.set()
                with pytest.raises(asyncio.CancelledError):
                    await task
            assert _markers(workspace, session) == [], "nothing landed"
            assert any("marker commit failed" in r.getMessage() for r in caplog.records)
            shown = await _reload(session)
            assert _tool_ids(shown) == (["call_a"], ["call_a"]), "the round is in the history once, written by the chokepoint"
            assert_anthropic_valid(shown)
        finally:
            await session.aclose()
            await backend.aclose()

    async def test_a_commit_that_hangs_cannot_make_the_turn_uncancellable(self, tmp_path, monkeypatch, caplog) -> None:
        """The wait for the commit is bounded, and the bound runs from the FIRST cancel: a drain must still be able to
        abort a turn whose commit hangs on a dead storage, however many cancels it has already absorbed. The storm here
        is spaced closer than the grace and lasts well past it (a cancel every 0.1s for a second, against a 0.3s grace),
        so a deadline that restarted at every cancel would keep the turn waiting until the storm stopped."""
        import logging

        import primer.agent.base as base

        grace = 0.3
        monkeypatch.setattr(base, "_MARKER_COMMIT_GRACE_S", grace)
        backend, workspace, session = await open_session(tmp_path)
        forever = asyncio.Event()
        try:
            await _seed(workspace, session)
            entered = asyncio.Event()

            def hanging_commit(executor) -> None:
                async def hang(*args, **kwargs):
                    entered.set()
                    await forever.wait()

                executor._replace_compacted_head = hang  # noqa: SLF001

            with caplog.at_level(logging.ERROR, logger="primer.agent.base"):
                loop = asyncio.get_running_loop()
                task = asyncio.create_task(run_turn(session, FnLLM(_reactive()), configure=hanging_commit))
                await asyncio.wait_for(entered.wait(), timeout=30)

                async def when_done() -> float:
                    await asyncio.wait({task})               # does not cancel it, does not raise what it ended with
                    return loop.time()

                watcher = asyncio.create_task(when_done())
                first_cancel = loop.time()
                sent = 0
                for _ in range(10):                      # a cancel storm: each is absorbed until the grace is up
                    if task.done():
                        break
                    task.cancel()
                    sent += 1
                    await asyncio.sleep(0.1)
                finished_at = await asyncio.wait_for(watcher, timeout=5)
                with pytest.raises(asyncio.CancelledError):
                    await task
            assert sent >= 3, "the storm landed more than one cancel inside the grace, so the test says something"
            assert finished_at - first_cancel < grace + 0.5, (
                f"the turn ended {finished_at - first_cancel:.2f}s after the FIRST cancel, grace {grace}s: the "
                "deadline moved with later cancels"
            )
            assert any("did not finish within" in r.getMessage() for r in caplog.records), "said so, loudly"
        finally:
            forever.set()
            await session.aclose()
            await backend.aclose()

    async def test_a_lost_lease_cancel_is_not_held_up_by_the_commit(self, tmp_path, monkeypatch) -> None:
        """A cancel that says the lease was lost (``CANCEL_REASON_PREEMPTED``) is the one the wait is for least: the
        chokepoint writes no rounds for it, so there is no accounting to settle, and the session may belong to another
        worker. It goes through at once instead of waiting the grace for a commit that hangs."""
        import primer.agent.base as base
        from primer.model.yield_ import CANCEL_REASON_PREEMPTED

        monkeypatch.setattr(base, "_MARKER_COMMIT_GRACE_S", 30.0)
        backend, workspace, session = await open_session(tmp_path)
        forever = asyncio.Event()
        try:
            await _seed(workspace, session)
            entered = asyncio.Event()

            def hanging_commit(executor) -> None:
                async def hang(*args, **kwargs):
                    entered.set()
                    await forever.wait()

                executor._replace_compacted_head = hang  # noqa: SLF001

            task = asyncio.create_task(run_turn(session, FnLLM(_reactive()), configure=hanging_commit))
            await asyncio.wait_for(entered.wait(), timeout=30)
            task.cancel(CANCEL_REASON_PREEMPTED)
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(task, timeout=3)      # not the 30s grace
            assert _markers(workspace, session) == []
            shown = await _reload(session)
            assert _tool_ids(shown) == ([], []), "a worker that lost the lease writes nothing"
        finally:
            forever.set()
            await session.aclose()
            await backend.aclose()

    @staticmethod
    async def _cancelled_then_the_lease_is_lost(
        tmp_path, monkeypatch, *, preempted_first: bool, commit_fails_after: float | None,
    ):
        """One turn whose marker commit hangs (``commit_fails_after`` None) or fails after that many seconds, a 30s grace,
        and the two cancels a worker delivers when a user cancel is followed by a lost lease (or the lost lease alone).
        Returns the exception the turn ended with, the seconds from the LAST cancel to the end of the turn, the history
        as it is once the commit has had time to fail, and the markers."""
        import primer.agent.base as base
        from primer.model.yield_ import CANCEL_REASON_PREEMPTED

        monkeypatch.setattr(base, "_MARKER_COMMIT_GRACE_S", 30.0)
        backend, workspace, session = await open_session(tmp_path)
        forever = asyncio.Event()
        try:
            await _seed(workspace, session)
            entered = asyncio.Event()

            def commit(executor) -> None:
                async def run(*args, **kwargs):
                    entered.set()
                    if commit_fails_after is None:
                        await forever.wait()
                    await asyncio.sleep(commit_fails_after)
                    raise OSError("the workspace mount went away")

                executor._replace_compacted_head = run  # noqa: SLF001

            loop = asyncio.get_running_loop()
            task = asyncio.create_task(run_turn(session, FnLLM(_reactive()), configure=commit))
            await asyncio.wait_for(entered.wait(), timeout=30)
            if preempted_first:
                task.cancel(CANCEL_REASON_PREEMPTED)
            else:
                task.cancel()                            # the user's hard cancel starts the wait ...
                await asyncio.sleep(0.1)
                task.cancel(CANCEL_REASON_PREEMPTED)     # ... and the lease is lost while it waits
            last_cancel = loop.time()
            with pytest.raises(asyncio.CancelledError) as ended:
                await asyncio.wait_for(task, timeout=3)  # not the 30s grace
            elapsed = loop.time() - last_cancel
            if commit_fails_after is not None:
                await asyncio.sleep(commit_fails_after + 0.4)    # the abandoned commit fails on its own
            return ended.value, elapsed, await _reload(session), _markers(workspace, session)
        finally:
            forever.set()
            await session.aclose()
            await backend.aclose()

    async def test_a_lost_lease_cancel_that_arrives_second_is_not_held_up_either(self, tmp_path, monkeypatch) -> None:
        """The skip is for the lost lease whichever cancel it is. A user hard cancel lands first and starts the wait; the
        lease is then lost and the heartbeat delivers ``CANCEL_REASON_PREEMPTED``. A loop that absorbed it held the turn
        for the rest of the grace and then raised the FIRST cancel, so the chokepoint never saw the lost lease."""
        from primer.model.yield_ import CANCEL_REASON_PREEMPTED

        ended, elapsed, shown, markers = await self._cancelled_then_the_lease_is_lost(
            tmp_path, monkeypatch, preempted_first=False, commit_fails_after=None,
        )
        assert elapsed < 2, f"the turn waited {elapsed:.1f}s after the lost lease"
        assert ended.args[:1] == (CANCEL_REASON_PREEMPTED,), "the lost lease is what the chokepoint is told"
        assert markers == [] and _tool_ids(shown) == ([], []), "a worker that lost the lease writes nothing"

    async def test_a_lost_lease_after_a_cancel_does_not_let_the_turn_write_its_rounds_as_a_non_owner(
        self, tmp_path, monkeypatch,
    ) -> None:
        """The same order against a commit that FAILS inside the grace. The turn used to wait for it, see the failure,
        keep the rounds in the record and re-raise the user's cancel: the chokepoint, told nothing about the lost lease,
        wrote them after the session had passed to another worker."""
        ended, elapsed, shown, markers = await self._cancelled_then_the_lease_is_lost(
            tmp_path, monkeypatch, preempted_first=False, commit_fails_after=0.5,
        )
        assert elapsed < 2
        assert markers == []
        assert _tool_ids(shown) == ([], []), "the rounds were written by a worker that no longer owned the session"

    @pytest.mark.parametrize("preempted_first", [True, False], ids=["lost_lease_first", "lost_lease_second"])
    async def test_the_commit_a_lost_lease_cancel_abandons_is_still_consumed_and_logged(
        self, tmp_path, monkeypatch, caplog, preempted_first,
    ) -> None:
        """The turn does not wait for the commit, but it still takes it over: a commit that fails afterwards is
        retrieved and logged once (a WARNING, not asyncio's "exception was never retrieved")."""
        import logging

        with caplog.at_level(logging.WARNING, logger="primer.agent.base"):
            await self._cancelled_then_the_lease_is_lost(
                tmp_path, monkeypatch, preempted_first=preempted_first, commit_fails_after=0.4,
            )
        failed = [r for r in caplog.records if "abandoned compaction marker commit failed" in r.getMessage()]
        assert len(failed) == 1 and failed[0].levelno == logging.WARNING, [r.getMessage() for r in caplog.records]

    def test_the_marker_commit_grace_is_the_terminal_exit_grace(self) -> None:
        """Both are the bound on how long a cancelled turn may keep a drain waiting (they do not add up: a hard cancel
        during the stream skips the sheltered exit), and the pod budget in worker-system.md is built on that figure.
        A change to one has to be a decision about the other."""
        import primer.agent.base as base
        import primer.session.dispatch as dispatch

        assert base._MARKER_COMMIT_GRACE_S == dispatch._TERMINAL_EXIT_GRACE_S  # noqa: SLF001

    async def test_a_commit_that_lands_after_the_grace_is_not_written_twice(self, tmp_path, monkeypatch) -> None:
        """Past the grace the outcome is unknown. The commit is a thread that may still land, so the rounds are taken as
        in the marker: writing them again would put every tool_use id in the history twice."""
        import primer.agent.base as base

        monkeypatch.setattr(base, "_MARKER_COMMIT_GRACE_S", 0.2)
        backend, workspace, session = await open_session(tmp_path)
        try:
            await _seed(workspace, session)
            entered = asyncio.Event()

            def slow_commit(executor) -> None:
                original = executor._replace_compacted_head  # noqa: SLF001

                async def slow(*args, **kwargs):
                    entered.set()
                    await asyncio.sleep(0.6)             # well past the grace ...
                    return await original(*args, **kwargs)   # ... and then it lands

                executor._replace_compacted_head = slow  # noqa: SLF001

            task = asyncio.create_task(run_turn(session, FnLLM(_reactive()), configure=slow_commit))
            await asyncio.wait_for(entered.wait(), timeout=30)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(task, timeout=5)
            await asyncio.sleep(1.2)                     # the abandoned commit finishes on its own
            assert len(_markers(workspace, session)) == 1
            shown = await _reload(session)
            assert _tool_ids(shown) == (["call_a"], ["call_a"]), "the round is in the history once"
            assert_anthropic_valid(shown)
        finally:
            await session.aclose()
            await backend.aclose()

    async def test_a_commit_that_fails_after_the_grace_loses_the_rounds_and_says_how_many(
        self, tmp_path, monkeypatch, caplog,
    ) -> None:
        """The loss mode of taking an unfinished commit as landed, pinned so the docs say what the code does: the
        turn dropped its rounds from the record, so a commit that then FAILS leaves them in neither the marker nor
        messages.jsonl (the next turn runs those tool calls again), and the log says how many rounds went."""
        import logging

        import primer.agent.base as base

        monkeypatch.setattr(base, "_MARKER_COMMIT_GRACE_S", 0.2)
        backend, workspace, session = await open_session(tmp_path)
        try:
            await _seed(workspace, session)
            entered = asyncio.Event()

            def late_failure(executor) -> None:
                async def fail_late(*args, **kwargs):
                    entered.set()
                    await asyncio.sleep(0.6)             # past the grace ...
                    raise OSError("the workspace mount went away")   # ... and then it fails

                executor._replace_compacted_head = fail_late  # noqa: SLF001

            with caplog.at_level(logging.WARNING, logger="primer.agent.base"):
                task = asyncio.create_task(run_turn(session, FnLLM(_reactive()), configure=late_failure))
                await asyncio.wait_for(entered.wait(), timeout=30)
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await asyncio.wait_for(task, timeout=5)
                await asyncio.sleep(1.2)                 # the abandoned commit fails on its own
            assert _markers(workspace, session) == [], "the marker never landed"
            shown = await _reload(session)
            assert _tool_ids(shown) == ([], []), "and the round is not in the history either"
            lost = [r.getMessage() for r in caplog.records if "did not land" in r.getMessage()]
            assert len(lost) == 1 and "1 completed tool round(s)" in lost[0], lost
            assert all(r.levelno == logging.ERROR for r in caplog.records if "did not land" in r.getMessage())
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
