"""The ``compacting`` flag belongs to the window that set it.

``begin_compaction`` / ``end_compaction`` were a plain bool per session. A cancelled turn whose marker commit outlasts its
grace chains the close of its window to the commit's completion (``_close_the_window_when_done``); if the commit then lands
after a LATER turn on the same session has opened its own window (the later turn queued for the messages lock first), the
stale close cleared the later turn's flag. A steer sent while that later turn compacts was then committed at once instead of
deferred behind its marker. What the flag protects is the deferral invariant: a steer sent while a window is open is applied AFTER
that window's marker, FIFO among the steers deferred, and not committed ahead of it (the marker's kept-tail carry of lines
written after its snapshot is a backstop, not a guarantee). The flag is now owned by a token: a close that is not the open
window's is a no-op, and it leaves the steers deferred under the window that is still open alone (the next close applies them).
"""

from __future__ import annotations

import asyncio

import pytest

import primer.agent.base as base
from primer.model.chat import Message, TextPart
from tests._support.off_golden import FnLLM, make_executor, open_session, run_turn, text_events
from tests.agent.test_overflow_replay_characterisation import POSIX, _markers, _reactive, _reload, _seed, _tool_ids

STEER = "a steer that arrives while the later turn compacts"
EARLY = "a steer deferred under the earlier turn's window, before the later turn opened its own"


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
        # A steer deferred under A's window. ``append_instruction`` would block on the lock A's hung commit holds, so it is
        # queued the way ``append_instruction`` queues one while the flag is set (under the lock, which this test cannot take).
        session._state.add_pending_steer(  # noqa: SLF001
            session.session_id, Message(role="user", parts=[TextPart(text=EARLY)]),
        )
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
        pending = [
            "".join(p.text for p in m.parts if isinstance(p, TextPart))
            for m in session._state.peek_pending_steers(session.session_id)  # noqa: SLF001
        ]
        assert pending == [EARLY, STEER], "A's refused close left EARLY alone, and STEER is queued behind it (FIFO)"
        written = await _jsonl(session)
        assert EARLY not in written and STEER not in written, "neither is committed ahead of the window that is open"

        b_release.set()
        await asyncio.wait_for(task_b, timeout=30)
        lines = (await _jsonl(session)).splitlines()
        assert sum(EARLY in line for line in lines) == 1 and sum(STEER in line for line in lines) == 1, "each applied once"
        marker = max(i for i, line in enumerate(lines) if '"compaction_marker"' in line)
        early = next(i for i, line in enumerate(lines) if EARLY in line)
        steer = next(i for i, line in enumerate(lines) if STEER in line)
        assert marker < early < steer, "applied after the last marker, in the order they were deferred"
        assert not session._state.is_compacting(session.session_id)  # noqa: SLF001
        assert not session._state.peek_pending_steers(session.session_id)  # noqa: SLF001
        assert len(_markers(workspace, session)) == 1 and _tool_ids(await _reload(session))[0].count("call_a") == 1
    finally:
        a_gate.set()
        b_release.set()
        await session.aclose()
        await backend.aclose()
