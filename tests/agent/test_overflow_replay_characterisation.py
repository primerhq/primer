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

from primer.model.chat import (
    Done, ExtendedEvent, TextPart, ToolCallEnd, ToolCallStart, ToolResultPart,
)
from primer.model.except_ import BadRequestError, ContextOverflowUnrecoverable, ServerError
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
            assert error.problem_extensions == {"forced_compaction": True, "replay_attempted": True, "persisted_rounds": 1}
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
            assert failed.value.problem_extensions == {"forced_compaction": True, "replay_attempted": True, "persisted_rounds": 0}
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
