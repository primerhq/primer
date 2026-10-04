"""Today's context-overflow recovery, characterised BEFORE the prompt-budget work changes it.

When a turn's own LLM call is rejected as a context overflow, ``_BaseAgentExecutor.invoke`` force-compacts
the pre-turn history and re-runs ``_run_loop`` FROM SCRATCH. These tests pin what that means in practice,
on the real ``WorkspaceAgentExecutor`` over a real local workspace, including the parts that are
limitations (task 01a10893-ef41 changes exactly these; the proposed L1 and L2 layers are its general form):

* tools the rejected attempt already ran run AGAIN, and the first attempt's tool call and result are not
  in the persisted history, so one effect happened twice and the record shows it once;
* the replay is not itself covered: a second overflow propagates out of the turn;
* ``maybe_compact`` runs OUTSIDE the overflow handler, so a context overflow in the summariser fails the turn.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from primer.model.chat import Done, ToolCallEnd, ToolCallStart
from primer.model.except_ import BadRequestError
from tests._support.off_golden import (
    BIG_USER_CHARS, Events, Raise, ScriptedLLM, append_messages, assistant_message, open_session, run_turn,
    text_events, user_message,
)

OVERFLOW = "This model's maximum context length is 100000 tokens, however you requested more"


def _tool_call(call_id: str, command: str) -> Events:
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


@pytest.mark.skipif(not Path("/usr/bin/env").exists(), reason="needs a POSIX shell for the exec tool")
async def test_a_tool_the_rejected_attempt_already_ran_runs_again_and_only_the_replay_is_recorded(tmp_path) -> None:
    backend, workspace, session = await open_session(tmp_path)
    try:
        await _seed_replies(workspace, session, 6)
        llm = ScriptedLLM()
        llm.session_id = session.session_id
        llm.extend([
            _tool_call("call_a", "echo ran >> replay_counter.txt"),   # attempt 1: the tool runs
            Raise(BadRequestError(OVERFLOW)),                           # attempt 1: its next call is rejected
            Events(text_events("SUMMARY")),                             # force_compact's summary
            _tool_call("call_b", "echo ran >> replay_counter.txt"),   # attempt 2, from scratch: the SAME tool again
            Events(text_events("done")),
        ])
        await run_turn(session, llm)

        ran = (workspace.root / "replay_counter.txt").read_text().splitlines()
        assert ran == ["ran", "ran"], "the effect happened twice"
        messages = [l for l in _lines(workspace, session) if "role" in l]
        tool_calls = [p for m in messages for p in m["parts"] if p.get("type") == "tool_call"]
        tool_results = [p for m in messages if m["role"] == "tool" for p in m["parts"]]
        assert [p["id"] for p in tool_calls] == ["call_b"], "the first attempt's call is not in the persisted history"
        assert [p["id"] for p in tool_results] == ["call_b"]
        assert len(_markers(_lines(workspace, session))) == 1, "the forced compaction wrote its marker"
        assert len(llm.calls) == 5
    finally:
        await session.aclose()
        await backend.aclose()


async def test_a_second_overflow_has_no_handler_and_the_turn_fails_after_the_forced_compaction_was_persisted(tmp_path) -> None:
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
        assert len(_markers(lines)) == 1, "the compaction from the forced attempt stays persisted"
        assert lines[-1].get("kind") == "compaction_marker", "the last thing persisted is that marker: the failed turn recorded no reply"
    finally:
        await session.aclose()
        await backend.aclose()


async def test_a_context_overflow_in_the_summariser_fails_the_turn_because_maybe_compact_is_outside_the_handler(tmp_path) -> None:
    backend, workspace, session = await open_session(tmp_path)
    try:
        # over the trigger (6 x 30k tokens of user text) and with a head before the 4th most recent assistant
        # reply, so tier 2 runs
        for i in range(6):
            await append_messages(workspace, session, user_message(chr(ord("A") + i) * BIG_USER_CHARS), assistant_message(f"reply {i}"))
        await append_messages(workspace, session, user_message("now do the thing"))
        llm = ScriptedLLM()
        llm.session_id = session.session_id
        llm.extend([Raise(BadRequestError(OVERFLOW))])  # the summariser's own call is rejected
        with pytest.raises(BadRequestError):
            await run_turn(session, llm)
        assert len(llm.calls) == 1, "no force-compact, no retry: the summariser call was the only call"
        assert _markers(_lines(workspace, session)) == []
    finally:
        await session.aclose()
        await backend.aclose()
