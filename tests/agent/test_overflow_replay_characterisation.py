"""Context-overflow recovery: what it did (pinned as limitations in #318) and what it does now.

When a turn's own LLM call is rejected as a context overflow, ``_BaseAgentExecutor.invoke`` force-compacts
the pre-turn history and runs the turn again. These tests drive the real ``WorkspaceAgentExecutor`` over a
real local workspace and pin the recovery:

* the replay CONTINUES the turn: the tool rounds the rejected attempt already ran are carried into it, so no
  tool runs twice and the persisted history records each executed call exactly once (before: the replay
  restarted from scratch, the effect happened twice and the record showed the replay's only);
* the turn keeps its ``max_tool_turns`` budget across the replay instead of getting a fresh one;
* a carried tool result that is bigger than the window is reduced FOR THE REPLAY'S PROMPT (a forced prune)
  and persisted raw;
* a second overflow persists the carried rounds and then fails the turn (before: nothing was recorded);
* an overflow in the summariser is retried once with a reduced input and then fails naming the summariser
  (before: it failed the turn on the first one, because ``maybe_compact`` is outside the handler);
* only an INPUT overflow is recovered: an output-limit error (``max_tokens``) is not.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from primer.agent.overflow import is_context_overflow
from primer.model.chat import Done, ToolCallEnd, ToolCallPart, ToolCallStart, ToolResultPart
from primer.model.except_ import BadRequestError, ServerError
from primer.model.yield_ import Yielded, YieldToWorker
from tests._support.off_golden import (
    BIG_USER_CHARS, Events, Raise, ScriptedLLM, append_messages, assistant_message, open_session, run_turn,
    text_events, user_message,
)

OVERFLOW = "This model's maximum context length is 100000 tokens, however you requested more"
POSIX = pytest.mark.skipif(not Path("/usr/bin/env").exists(), reason="needs a POSIX shell for the exec tool")


def _tool_call(call_id: str, command: str = "echo ran >> replay_counter.txt") -> Events:
    return Events([
        ToolCallStart(id=call_id, name="workspace__exec", index=0),
        ToolCallEnd(id=call_id, arguments={"command": command, "description": "count executions"}, index=0),
        Done(stop_reason="tool_use", raw_reason="tool_use"),
    ])


async def _seed_replies(workspace, session, n: int) -> None:
    """Replies enough that something precedes the 4th most recent assistant message (``tail_turns``), so a
    forced compaction has a head to summarise. The history starts with a user message, so n >= 5 does it."""
    for i in range(n):
        await append_messages(workspace, session, user_message(f"filler {i}: " + "f" * 500), assistant_message(f"reply {i}"))
    await append_messages(workspace, session, user_message("now do the thing"))


def _lines(workspace, session) -> list[dict]:
    path = workspace.root / workspace.template.state_path / "sessions" / session.session_id / "messages.jsonl"
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _markers(lines: list[dict]) -> list[dict]:
    return [l for l in lines if l.get("kind") == "compaction_marker"]


def _persisted(lines: list[dict]) -> tuple[list[str], list[str]]:
    """The ids of the tool calls and of the tool results in the persisted Message lines."""
    messages = [l for l in lines if "role" in l]
    calls = [p["id"] for m in messages for p in m["parts"] if p.get("type") == "tool_call"]
    results = [p["id"] for m in messages if m["role"] == "tool" for p in m["parts"]]
    return calls, results


def _size(call: dict) -> int:
    return sum(p[1] for m in call["messages"] for p in m["parts"])


def _cap_tool_turns_at_2(executor) -> None:
    """``WorkspaceAgentExecutor`` rebuilds its agent from a few fields and drops ``max_tool_turns``, so the cap is
    set on the executor's own agent."""
    executor._agent = executor._agent.model_copy(update={"max_tool_turns": 2})  # noqa: SLF001


class _Wrapped:
    """A tool manager that delegates, and lets a test intercept ``execute`` for chosen calls."""

    def __init__(self, inner, intercept):
        self._inner, self._intercept = inner, intercept

    def __getattr__(self, name):
        return getattr(self._inner, name)

    async def execute(self, call, **kwargs):
        intercepted = self._intercept(call)
        if intercepted is not None:
            return intercepted
        return await self._inner.execute(call, **kwargs)


@POSIX
class TestTheReplayContinuesTheTurn:
    async def test_a_tool_the_rejected_attempt_already_ran_is_not_run_again_and_is_recorded_once(self, tmp_path) -> None:
        backend, workspace, session = await open_session(tmp_path)
        try:
            await _seed_replies(workspace, session, 6)
            llm = ScriptedLLM()
            llm.session_id = session.session_id
            llm.extend([
                _tool_call("call_a"),                            # attempt 1: the tool runs
                Raise(BadRequestError(OVERFLOW)),                # attempt 1: its next call is rejected
                Events(text_events("SUMMARY")),                  # force_compact's summary
                Events(text_events("done")),                     # the replay continues from the carried round
            ])
            await run_turn(session, llm)

            assert (workspace.root / "replay_counter.txt").read_text().splitlines() == ["ran"], "it ran once"
            lines = _lines(workspace, session)
            assert _persisted(lines) == (["call_a"], ["call_a"]), "the executed call and its result are recorded once"
            assert [l for l in lines if "role" in l][-1]["parts"][0]["text"] == "done"
            assert len(_markers(lines)) == 1
            assert len(llm.calls) == 4
        finally:
            await session.aclose()
            await backend.aclose()

    async def test_the_replay_is_handed_the_carried_round_after_the_compacted_history(self, tmp_path) -> None:
        backend, workspace, session = await open_session(tmp_path)
        try:
            await _seed_replies(workspace, session, 6)
            llm = ScriptedLLM()
            llm.session_id = session.session_id
            llm.extend([
                _tool_call("call_a"), Raise(BadRequestError(OVERFLOW)), Events(text_events("SUMMARY")), Events(text_events("done")),
            ])
            await run_turn(session, llm)
            replay_roles = [m["role"] for m in llm.calls[3]["messages"]]
            assert replay_roles[:2] == ["system", "assistant"], "the compacted history starts with the summary"
            assert replay_roles[-2:] == ["assistant", "tool"], "and ends with the round the rejected attempt ran"
        finally:
            await session.aclose()
            await backend.aclose()

    async def test_the_tool_turn_budget_is_the_turns_not_a_fresh_one(self, tmp_path) -> None:
        """``max_tool_turns=2``: one round ran before the overflow, so the replay's first tool call is the cap."""
        backend, workspace, session = await open_session(tmp_path)
        try:
            await _seed_replies(workspace, session, 6)
            llm = ScriptedLLM()
            llm.session_id = session.session_id
            llm.extend([
                _tool_call("call_a"), Raise(BadRequestError(OVERFLOW)), Events(text_events("SUMMARY")), _tool_call("call_b"),
            ])
            await run_turn(session, llm, configure=_cap_tool_turns_at_2)
            assert (workspace.root / "replay_counter.txt").read_text().splitlines() == ["ran"], "call_b was not dispatched"
        finally:
            await session.aclose()
            await backend.aclose()

    async def test_a_carried_result_bigger_than_the_window_is_reduced_for_the_replay_and_persisted_raw(self, tmp_path) -> None:
        backend, workspace, session = await open_session(tmp_path)
        try:
            await _seed_replies(workspace, session, 6)
            llm = ScriptedLLM()
            llm.session_id = session.session_id
            llm.extend([
                _tool_call("call_big"), Raise(BadRequestError(OVERFLOW)), Events(text_events("SUMMARY")), Events(text_events("done")),
            ])
            raw = "x" * 600_000  # about 150k tokens: more than the whole window

            def intercept(call):
                return ToolResultPart(id=call.id, output=raw) if call.id == "call_big" else None

            await run_turn(session, llm, wrap_tools=lambda manager: _Wrapped(manager, intercept))

            assert _size(llm.calls[1]) > 600_000, "the rejected call carried the raw result"
            assert _size(llm.calls[3]) < 100_000, "the replay's prompt carries it reduced"
            results = [p for l in _lines(workspace, session) if l.get("role") == "tool" for p in l["parts"]]
            assert [len(p["output"]) for p in results] == [600_000], "the persisted record keeps the raw result"
        finally:
            await session.aclose()
            await backend.aclose()

    async def test_a_park_raised_by_the_replay_stamps_the_carried_rounds(self, tmp_path) -> None:
        """The resume path rebuilds history from ``llm_messages``: it must hold the round the first attempt ran."""
        backend, workspace, session = await open_session(tmp_path)
        try:
            await _seed_replies(workspace, session, 6)
            llm = ScriptedLLM()
            llm.session_id = session.session_id
            llm.extend([
                _tool_call("call_a"), Raise(BadRequestError(OVERFLOW)), Events(text_events("SUMMARY")), _tool_call("call_b"),
            ])

            def intercept(call):
                if call.id == "call_b":
                    raise YieldToWorker(Yielded(tool_name="workspace__exec", event_key="timer:call_b"), tool_call_id="call_b")
                return None

            with pytest.raises(YieldToWorker) as parked:
                await run_turn(session, llm, wrap_tools=lambda manager: _Wrapped(manager, intercept))
            stamped = parked.value.llm_messages
            calls = [p.id for m in stamped for p in m.parts if isinstance(p, ToolCallPart)]
            results = [p.id for m in stamped for p in m.parts if isinstance(p, ToolResultPart)]
            assert calls == ["call_a", "call_b"] and results == ["call_a"]
        finally:
            await session.aclose()
            await backend.aclose()


@POSIX
class TestASecondOverflow:
    async def test_persists_the_carried_rounds_and_then_fails_the_turn(self, tmp_path) -> None:
        backend, workspace, session = await open_session(tmp_path)
        try:
            await _seed_replies(workspace, session, 6)
            llm = ScriptedLLM()
            llm.session_id = session.session_id
            llm.extend([_tool_call("call_a"), Raise(BadRequestError(OVERFLOW)), Events(text_events("SUMMARY")), Raise(BadRequestError(OVERFLOW))])
            with pytest.raises(BadRequestError):
                await run_turn(session, llm)
            lines = _lines(workspace, session)
            assert len(llm.calls) == 4
            assert len(_markers(lines)) == 1, "the compaction from the forced attempt stays persisted"
            assert _persisted(lines) == (["call_a"], ["call_a"]), "the tool ran, so its call and result are in the record"
            assert (workspace.root / "replay_counter.txt").read_text().splitlines() == ["ran"]
        finally:
            await session.aclose()
            await backend.aclose()

    async def test_the_turns_own_input_is_recorded_with_the_carried_rounds(self, tmp_path) -> None:
        """The same rule as a normal end-of-turn persist: the input the turn was given comes first."""
        backend, workspace, session = await open_session(tmp_path)
        try:
            await _seed_replies(workspace, session, 6)
            llm = ScriptedLLM()
            llm.session_id = session.session_id
            llm.extend([_tool_call("call_a"), Raise(BadRequestError(OVERFLOW)), Events(text_events("SUMMARY")), Raise(BadRequestError(OVERFLOW))])
            with pytest.raises(BadRequestError):
                await run_turn(session, llm, messages=[user_message("THE NEW INPUT")])
            after_marker = _lines(workspace, session)[[l.get("kind") for l in _lines(workspace, session)].index("compaction_marker") + 1:]
            roles = [(l["role"], l["parts"][0].get("text", "")[:13]) for l in after_marker if "role" in l]
            assert roles[0] == ("user", "THE NEW INPUT") and [r for r, _ in roles[1:]] == ["assistant", "tool"]
        finally:
            await session.aclose()
            await backend.aclose()

    async def test_with_nothing_carried_it_records_nothing_but_the_marker(self, tmp_path) -> None:
        backend, workspace, session = await open_session(tmp_path)
        try:
            await _seed_replies(workspace, session, 6)
            llm = ScriptedLLM()
            llm.session_id = session.session_id
            llm.extend([Raise(BadRequestError(OVERFLOW)), Events(text_events("SUMMARY")), Raise(BadRequestError(OVERFLOW))])
            with pytest.raises(BadRequestError):
                await run_turn(session, llm)
            lines = _lines(workspace, session)
            assert len(llm.calls) == 3
            assert lines[-1].get("kind") == "compaction_marker", "no tool ran, so no turn lines were invented"
        finally:
            await session.aclose()
            await backend.aclose()


class TestTheSummariser:
    @staticmethod
    async def _seed_compactable(workspace, session) -> None:
        # over the trigger (6 x 30k tokens of user text) and with a head before the 4th most recent assistant
        # reply, so tier 2 runs
        for i in range(6):
            await append_messages(workspace, session, user_message(chr(ord("A") + i) * BIG_USER_CHARS), assistant_message(f"reply {i}"))
        await append_messages(workspace, session, user_message("now do the thing"))

    async def test_an_overflow_in_the_summariser_is_retried_once_with_a_reduced_input(self, tmp_path) -> None:
        backend, workspace, session = await open_session(tmp_path)
        try:
            await self._seed_compactable(workspace, session)
            llm = ScriptedLLM()
            llm.session_id = session.session_id
            llm.extend([Raise(BadRequestError(OVERFLOW)), Events(text_events("THE SUMMARY")), Events(text_events("done"))])
            await run_turn(session, llm)
            assert len(llm.calls) == 3
            assert _size(llm.calls[1]) < _size(llm.calls[0]) / 2, "the retry's input is a reduced copy"
            markers = _markers(_lines(workspace, session))
            assert len(markers) == 1
            assert "THE SUMMARY" in markers[0]["payload"]["summary"]
            assert "reduced" in markers[0]["payload"]["summary"], "the summary says it was written from a reduced copy"
        finally:
            await session.aclose()
            await backend.aclose()

    async def test_an_overflow_that_survives_the_reduction_fails_the_turn_naming_the_summariser(self, tmp_path) -> None:
        backend, workspace, session = await open_session(tmp_path)
        try:
            await self._seed_compactable(workspace, session)
            llm = ScriptedLLM()
            llm.session_id = session.session_id
            llm.extend([Raise(BadRequestError(OVERFLOW)), Raise(BadRequestError(OVERFLOW))])
            with pytest.raises(ServerError, match="summariser"):
                await run_turn(session, llm)
            assert len(llm.calls) == 2, "one retry, not a loop"
            assert _markers(_lines(workspace, session)) == []
        finally:
            await session.aclose()
            await backend.aclose()


class TestOnlyAnInputOverflowIsRecovered:
    async def test_an_output_limit_error_is_not_a_context_overflow_and_is_not_recovered(self, tmp_path) -> None:
        """``max_tokens`` used to match, which compacted (a lossy, durable summary) for an error compaction cannot fix."""
        backend, workspace, session = await open_session(tmp_path)
        try:
            await _seed_replies(workspace, session, 6)
            llm = ScriptedLLM()
            llm.session_id = session.session_id
            llm.extend([Raise(BadRequestError("max_tokens is too large: 200000. This model supports at most 4096 completion tokens."))])
            with pytest.raises(BadRequestError, match="max_tokens"):
                await run_turn(session, llm)
            assert len(llm.calls) == 1, "no force-compact, no retry"
            assert _markers(_lines(workspace, session)) == []
        finally:
            await session.aclose()
            await backend.aclose()

    @pytest.mark.parametrize("message", [
        "This model's maximum context length is 128000 tokens. However, your messages resulted in 130000 tokens.",
        "context_length_exceeded",
        "prompt is too long: 215123 tokens > 200000 maximum",
        "Input is too long for requested model",
        "The input token count (1200000) exceeds the maximum number of tokens allowed (1048576).",
        "the input length exceeds the context length",
        "Your input exceeds the context window of this model",
    ])
    def test_these_are_input_overflows(self, message: str) -> None:
        assert is_context_overflow(BadRequestError(message))

    @pytest.mark.parametrize("message", [
        "max_tokens is too large: 200000. This model supports at most 4096 completion tokens.",
        "max_tokens: 100000 > 64000, which is the maximum allowed number of output tokens for claude-x",
        "tool name too long (max 64 characters)",
        "file name is too long",
        "invalid request: temperature must be between 0 and 2",
        "",
    ])
    def test_these_are_not(self, message: str) -> None:
        assert not is_context_overflow(BadRequestError(message))
