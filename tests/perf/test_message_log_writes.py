"""Task 14 stage 2: with the log append-only, a turn writes its own lines, not the whole log, and spawns the same git subprocesses."""

from __future__ import annotations

import asyncio
import shutil

import pytest

import primer.workspace.local.state as state_mod
from tests._support.log_golden import Env
from tests._support.off_golden import ScriptedLLM, assistant_message, open_session, user_message

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git CLI not available on PATH")


async def _run(tmp_path, append_only: bool, monkeypatch):
    backend, workspace, session = await open_session(tmp_path)
    try:
        env = Env(workspace, session, ScriptedLLM(), append_only=append_only)
        chunk = "x" * 2000
        for i in range(1000):  # a 2 MB log
            await workspace.append_message_line(session.session_id, (user_message(f"{i} {chunk}").model_dump_json() + "\n").encode())
        written = {"whole": 0, "appended": 0}
        real_write, real_append = state_mod._write_atomic, state_mod._append_bytes

        def counting_write(target, content):
            written["whole"] += len(content.encode() if isinstance(content, str) else content) if target.name == "messages.jsonl" else 0
            return real_write(target, content)

        def counting_append(target, data):
            written["appended"] += len(data)
            return real_append(target, data)

        git_calls = []
        real_exec = asyncio.create_subprocess_exec

        async def counting_exec(*args, **kw):
            git_calls.append(args[3] if len(args) > 3 else args)
            return await real_exec(*args, **kw)

        monkeypatch.setattr(state_mod, "_write_atomic", counting_write)
        monkeypatch.setattr(state_mod, "_append_bytes", counting_append)
        monkeypatch.setattr(state_mod.asyncio, "create_subprocess_exec", counting_exec)
        await env.ex._persist_turn([user_message("next question"), assistant_message("the answer")])
        await session.append_instruction("a steer")
        return written, git_calls, env.messages_path.read_bytes()
    finally:
        await session.aclose()
        await backend.aclose()


async def test_an_append_only_turn_writes_its_own_bytes_and_the_same_number_of_git_commands(tmp_path, monkeypatch):
    whole_written, whole_git, whole_bytes = await _run(tmp_path / "off", False, monkeypatch)
    monkeypatch.undo()
    appended_written, appended_git, appended_bytes = await _run(tmp_path / "on", True, monkeypatch)
    assert whole_written["whole"] > 2_000_000 * 2 and whole_written["appended"] == 0         # the persist and the steer each rewrote the log
    assert appended_written["whole"] == 0 and 0 < appended_written["appended"] < 2_000       # only the new lines were written
    assert len(appended_git) == len(whole_git)                                               # and the same git commands ran
    assert appended_bytes.count(b"\n") == whole_bytes.count(b"\n")                             # the same lines (byte identity: the goldens)
