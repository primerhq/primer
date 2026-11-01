"""Stage 0 of Task 14: steers, event rows and persists racing never land in a rewrite gap, and the lock order is messages_lock -> _commit_lock.

The MEDIUM-1 guarantee: a writer that lands between another writer's read and its rewrite of ``messages.jsonl`` is silently truncated by it. The
guarantee is the order of two locks, taken the same way by every writer, so a spy records it: a task that holds the repo's commit lock must never
then ask for the session's messages lock.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import shutil

import pytest

from tests._support.log_golden import Env, record
from tests._support.off_golden import ScriptedLLM, assistant_message, open_session, user_message
from primer.model.workspace_session import SessionMessageKind

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git CLI not available on PATH")


class LockOrderSpy:
    """Wraps the repo's two locks and records, per task, the order they are taken in."""

    def __init__(self, state):
        self.state = state
        self.held: dict[asyncio.Task, list[str]] = {}
        self.violations: list[str] = []
        self.acquisitions = {"messages_lock": 0, "commit_lock": 0}
        real_messages_lock = state.messages_lock
        real_commit_lock = state._commit_lock

        @contextlib.asynccontextmanager
        async def messages_lock(session_id):
            self._enter("messages_lock")
            try:
                async with real_messages_lock(session_id):
                    yield
            finally:
                self._leave("messages_lock")

        class _CommitLock:
            async def __aenter__(inner):
                await real_commit_lock.__aenter__()
                self._enter("commit_lock")

            async def __aexit__(inner, *exc):
                self._leave("commit_lock")
                return await real_commit_lock.__aexit__(*exc)

        state.messages_lock = messages_lock
        state._commit_lock = _CommitLock()

    def _enter(self, name: str) -> None:
        task = asyncio.current_task()
        stack = self.held.setdefault(task, [])
        self.acquisitions[name] += 1
        if name == "messages_lock" and "commit_lock" in stack:
            self.violations.append("messages_lock taken while holding commit_lock")
        stack.append(name)

    def _leave(self, name: str) -> None:
        stack = self.held.get(asyncio.current_task(), [])
        if name in stack:
            stack.remove(name)


async def _lines(env: Env) -> list[dict]:
    return [json.loads(line) for line in env.messages_path.read_text(encoding="utf-8").splitlines() if line.strip()]


async def test_steers_event_rows_and_persists_racing_all_land_and_none_is_truncated(tmp_path):
    backend, workspace, session = await open_session(tmp_path)
    try:
        env = Env(workspace, session, ScriptedLLM(), append_only=False)
        spy = LockOrderSpy(session._state)
        writer = env.writer(start_seq=100)
        env.ex.bind_event_log(writer)

        async def persists():
            for i in range(6):
                await env.ex._persist_turn([user_message(f"turn {i}"), assistant_message(f"reply {i}")])

        async def steers():
            for i in range(6):
                await session.append_instruction(f"steer {i}")

        async def events():
            for i in range(12):
                await writer.append(record(101 + i, SessionMessageKind.ASSISTANT_TOKEN, {"text": f"tok {i}"}))
                await asyncio.sleep(0)
            await writer.flush()

        async def canvas():
            for i in range(6):
                await env.event_line(record(500 + i, SessionMessageKind.CANVAS_EVENT, {"event": f"click {i}"}))

        await asyncio.gather(persists(), steers(), events(), canvas())

        lines = await _lines(env)  # every line parses: none was torn
        texts = [p["text"] for obj in lines if "role" in obj for p in obj.get("parts", []) if p.get("type") == "text"]
        for i in range(6):
            assert texts.count(f"turn {i}") == 1 and texts.count(f"reply {i}") == 1 and texts.count(f"steer {i}") == 1
        seqs = [obj["seq"] for obj in lines if isinstance(obj.get("seq"), int)]
        assert sorted(s for s in seqs if 101 <= s <= 112) == list(range(101, 113))      # every event row survived
        assert sorted(s for s in seqs if s >= 500) == list(range(500, 506))             # and every canvas row
        assert spy.violations == []
        assert spy.acquisitions["messages_lock"] > 0 and spy.acquisitions["commit_lock"] > 0
    finally:
        await session.aclose()
        await backend.aclose()


async def test_the_spy_catches_the_inverted_order(tmp_path):
    """The spy itself: a task that takes the commit lock and then the messages lock is a violation."""
    backend, workspace, session = await open_session(tmp_path)
    try:
        spy = LockOrderSpy(session._state)
        async with session._state._commit_lock:
            async with session.messages_lock:
                pass
        assert spy.violations == ["messages_lock taken while holding commit_lock"]
    finally:
        await session.aclose()
        await backend.aclose()
