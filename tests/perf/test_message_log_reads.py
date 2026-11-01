"""Task 14 stage 1: over a 2 MB log, the readers of a turn read the file once and parse only what was appended."""

from __future__ import annotations

import shutil

import pytest

from tests._support.off_golden import ScriptedLLM, assistant_message, make_executor, open_session, user_message

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git CLI not available on PATH")


async def _big_session(tmp_path):
    backend, workspace, session = await open_session(tmp_path)
    ex = make_executor(session, ScriptedLLM())
    chunk = "x" * 2000
    for i in range(0, 1000):
        await workspace.append_message_line(
            session.session_id, (user_message(f"{i} {chunk}").model_dump_json() + "\n").encode(),
        )
    size = (workspace.root / workspace.template.state_path / "sessions" / session.session_id / "messages.jsonl").stat().st_size
    assert size > 2_000_000
    return backend, workspace, session, ex, size


async def test_the_readers_of_a_turn_read_the_file_once_and_parse_each_byte_once(tmp_path):
    backend, workspace, session, ex, size = await _big_session(tmp_path)
    try:
        cache = None
        await ex._read_messages_jsonl()                     # the turn's history load
        cache = session._log_cache
        assert cache.reads == 1 and cache.parsed_bytes >= size
        for _ in range(4):                                   # the compaction state, the pending messages, a window open: same bytes
            await ex._last_compaction()
            await session.take_pending_messages()
            await ex._open_compaction_window()
            await ex._close_compaction_window()
        assert cache.reads == 1 and cache.parsed_bytes < size * 1.01
    finally:
        await session.aclose()
        await backend.aclose()


async def test_a_turn_persist_costs_a_parse_of_its_own_lines_not_of_the_log(tmp_path):
    backend, workspace, session, ex, size = await _big_session(tmp_path)
    try:
        await ex._read_messages_jsonl()
        cache = session._log_cache
        before = cache.parsed_bytes
        new = [user_message("next question"), assistant_message("the answer")]
        await ex._persist_turn(new)
        history = await ex._read_messages_jsonl()
        assert len(history) == 1003   # the session's opening instruction, the 1000 seeded lines and the 2 new ones
        parsed = cache.parsed_bytes - before
        assert parsed < 2_000, parsed                        # the two new lines (a few hundred bytes), not 2 MB
    finally:
        await session.aclose()
        await backend.aclose()
