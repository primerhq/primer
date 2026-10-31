"""The compaction window of the workspace executor (AG-M6).

A read that fails or is cancelled while the window opens used to leave the session "compacting" (nothing closed it),
so the next wake's message was held in process memory; and the steers the pre-turn close drained were written to disk
but left out of the turn's own prompt.

Characterization (passes before and after): a window opens and closes cleanly on a real session, and the cleanup of a
failed open closes only its own window.
"""
from __future__ import annotations

import asyncio

import pytest

from primer.agent.workspace_executor import WorkspaceAgentExecutor
from primer.model.chat import TextPart
from primer.workspace.local.state import LocalStateRepo
from tests._support.off_golden import (
    FnLLM, Gate, append_messages, assistant_message, make_executor, open_session, run_turn, text_events, user_message,
)


def _text(message) -> str:
    return "".join(p.text for p in message.parts if isinstance(p, TextPart))

# ---------------------------------------------------------------------------
# AG-M6: a failed or cancelled window open leaves no compaction flag
# ---------------------------------------------------------------------------


class _State:
    """The real LocalStateRepo window and steer methods over a read that fails."""

    begin_compaction = LocalStateRepo.begin_compaction
    end_compaction = LocalStateRepo.end_compaction
    is_compacting = LocalStateRepo.is_compacting
    add_pending_steer = LocalStateRepo.add_pending_steer
    peek_pending_steers = LocalStateRepo.peek_pending_steers

    def __init__(self, exc: BaseException):
        self._compaction_tokens = 0
        self._compaction_owner: dict[str, int] = {}
        self._pending_steers: dict = {}
        self._exc = exc

    async def read_state_file(self, rel):
        raise self._exc


class _Session:
    def __init__(self, state):
        self.session_id = "sess-1"
        self._state = state
        self.messages_lock = asyncio.Lock()


def _bare_executor(state) -> WorkspaceAgentExecutor:
    ex = WorkspaceAgentExecutor.__new__(WorkspaceAgentExecutor)
    ex._session = _Session(state)
    ex._window_token = 0
    ex._window_last_compaction = None
    return ex


@pytest.mark.parametrize("exc", [OSError("runtime read failed"), asyncio.CancelledError()], ids=["oserror", "cancelled"])
async def test_a_failed_window_open_leaves_no_compaction_flag(exc):
    state = _State(exc)
    ex = _bare_executor(state)
    with pytest.raises(type(exc)):
        await ex._open_compaction_window()
    assert not state.is_compacting("sess-1")
    assert not ex._session.messages_lock.locked()


async def test_a_failed_window_open_does_not_clear_a_later_window():
    """The cleanup closes THIS window only: a window opened since (another token) stays open."""
    state = _State(OSError("read failed"))
    ex = _bare_executor(state)
    real_read = state.read_state_file

    async def read_then_reopen(rel):
        state.begin_compaction("sess-1")          # a later window takes the session before this read fails
        return await real_read(rel)

    state.read_state_file = read_then_reopen
    with pytest.raises(OSError):
        await ex._open_compaction_window()
    assert state.is_compacting("sess-1")


async def test_a_window_opens_and_closes_on_a_real_session(tmp_path):
    backend, workspace, session = await open_session(tmp_path)
    try:
        await append_messages(workspace, session, user_message("Q"))
        ex = make_executor(session, FnLLM(lambda *a: text_events("x")))
        history = await ex._open_compaction_window()
        assert _text(history[-1]) == "Q"
        assert session._state.is_compacting(session.session_id)
        assert await ex._close_compaction_window() == []
        assert not session._state.is_compacting(session.session_id)
    finally:
        await session.aclose()
        await backend.aclose()


# ---------------------------------------------------------------------------
# AG-M6: a steer the pre-turn close drained is in that turn's prompt
# ---------------------------------------------------------------------------


def _is_summariser(kwargs) -> bool:
    return kwargs.get("max_output_tokens") == 4096


async def test_a_steer_deferred_by_the_pre_turn_compaction_is_in_the_turns_prompt(tmp_path):
    backend, workspace, session = await open_session(tmp_path)
    try:
        for i in range(6):
            await append_messages(workspace, session, user_message(chr(65 + i) * 120_000), assistant_message(f"r{i}"))
        await append_messages(workspace, session, user_message("QUESTION"))
        entered, release = asyncio.Event(), asyncio.Event()

        def fn(n, messages, kwargs):
            if _is_summariser(kwargs):
                return Gate(entered, release, text_events("SUMMARY"))
            return text_events("done")

        llm = FnLLM(fn)
        task = asyncio.create_task(run_turn(session, llm))
        await asyncio.wait_for(entered.wait(), timeout=30)
        await session.append_instruction("STEER-WHILE-IT-COMPACTED")   # deferred: the window is open
        release.set()
        await asyncio.wait_for(task, timeout=30)
        answer_prompt = [_text(m) for m in llm.calls[-1]["messages"]]
        assert "STEER-WHILE-IT-COMPACTED" in answer_prompt, "the turn's prompt was not handed the drained steer"
        assert answer_prompt.index("QUESTION") < answer_prompt.index("STEER-WHILE-IT-COMPACTED")
        reloaded = [_text(m) for m in await make_executor(session, llm)._read_messages_jsonl()]
        assert reloaded.count("STEER-WHILE-IT-COMPACTED") == 1
        assert reloaded[-1] == "done"
    finally:
        await session.aclose()
        await backend.aclose()
