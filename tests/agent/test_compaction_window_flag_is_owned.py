"""The ``compacting`` flag belongs to the window that set it.

``begin_compaction`` / ``end_compaction`` were a plain bool per session. A cancelled turn whose marker commit outlasts its
grace chains the close of its window to the commit's completion (``_close_the_window_when_done``); if the commit then lands
after a LATER turn on the same session has opened its own window (the later turn queued for the messages lock first), the
stale close cleared the later turn's flag. A steer sent while that later turn compacts was then committed at once instead of
deferred behind its marker, which is the loss the deferral exists to prevent (the folded steer is in no summary). The flag is
now owned by a token: a close that is not the open window's is a no-op, and it does not drain the steers deferred inside the
window that is still open either.
"""

from __future__ import annotations

import asyncio

import pytest

import primer.agent.base as base
from tests._support.off_golden import FnLLM, make_executor, open_session, run_turn, text_events
from tests.agent.test_overflow_replay_characterisation import POSIX, _markers, _reactive, _reload, _seed, _tool_ids

STEER = "a steer that arrives while the later turn compacts"


async def _jsonl(session) -> str:
    return await make_executor(session, FnLLM(lambda n, m, k: text_events("x")))._read_messages_jsonl_text()  # noqa: SLF001


@POSIX
async def test_a_stale_deferred_close_does_not_clear_the_window_of_a_later_turn(tmp_path, monkeypatch):
    monkeypatch.setattr(base, "_MARKER_COMMIT_GRACE_S", 0.2)
    backend, workspace, session = await open_session(tmp_path)
    a_gate, a_entered = asyncio.Event(), asyncio.Event()
    b_in_window, b_release = asyncio.Event(), asyncio.Event()
    a_close_ran = asyncio.Event()
    original_done = base._deferred_window_close_done  # noqa: SLF001

    def closes_done(task) -> None:
        original_done(task)
        a_close_ran.set()

    monkeypatch.setattr(base, "_deferred_window_close_done", closes_done)
    try:
        await _seed(workspace, session)
        real = session.commit_state

        async def commit_state(*args, **kwargs):
            if "compaction marker" in str(kwargs.get("summary", "")) and not a_entered.is_set():
                a_entered.set()
                await a_gate.wait()                      # turn A's marker commit hangs under the real lock
            return await real(*args, **kwargs)

        monkeypatch.setattr(session, "commit_state", commit_state)

        # Turn A: an overflow, a forced compaction whose marker commit hangs, a hard cancel, the grace runs out.
        task_a = asyncio.create_task(run_turn(session, FnLLM(_reactive())))
        await asyncio.wait_for(a_entered.wait(), timeout=30)
        task_a.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task_a, timeout=10)
        assert session._state.is_compacting(session.session_id), "A's window is still open: its close waits for the commit"  # noqa: SLF001
        assert not a_close_ran.is_set()

        # Turn B on the same session queues for the lock A's commit holds, and sits in its own window.
        def hold_the_window_open(executor) -> None:
            real_last = executor._last_compaction  # noqa: SLF001

            async def gated():
                b_in_window.set()
                await b_release.wait()
                return await real_last()

            executor._last_compaction = gated  # noqa: SLF001

        task_b = asyncio.create_task(run_turn(
            session, FnLLM(lambda n, m, k: text_events("done")), configure=hold_the_window_open,
        ))
        await asyncio.sleep(0.05)                        # B is queued on the lock, ahead of A's deferred close
        a_gate.set()                                     # A's commit lands: B takes the lock first, then A's close
        await asyncio.wait_for(b_in_window.wait(), timeout=10)
        await asyncio.wait_for(a_close_ran.wait(), timeout=10)   # A's deferred close ran, behind B's open
        assert not base._DEFERRED_WINDOW_CLOSES                   # noqa: SLF001

        assert session._state.is_compacting(session.session_id), "A's stale close cleared B's window"  # noqa: SLF001
        await session.append_instruction(STEER)
        assert len(session._state.peek_pending_steers(session.session_id)) == 1, "the steer is deferred"  # noqa: SLF001
        assert STEER not in await _jsonl(session), "and not committed ahead of B's window"

        b_release.set()
        await asyncio.wait_for(task_b, timeout=30)
        assert (await _jsonl(session)).count(STEER) == 1, "B's own close applied it, once"
        assert not session._state.is_compacting(session.session_id)  # noqa: SLF001
        assert len(_markers(workspace, session)) == 1 and _tool_ids(await _reload(session))[0].count("call_a") == 1
    finally:
        a_gate.set()
        b_release.set()
        await session.aclose()
        await backend.aclose()
