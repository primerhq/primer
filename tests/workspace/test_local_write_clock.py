"""The local and sandbox workspaces tell the writer when the request is actually sent (review of #545, B4).

Both take the session's ``messages_lock`` before they write. That lock is held across a turn persist (read, rewrite, git commit), so a
batch can legitimately wait for it for longer than the write bound; the writer's clock must start when the lock is taken, not when the
batch was handed over. The bound here is 0.2 s and the wait 0.5 s.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from primer.model.workspace_session import SessionMessageKind, SessionMessageRecord
from primer.session import persistence
from primer.session.persistence import WorkspaceMessageWriter
from primer.workspace import LocalWorkspace
from primer.workspace.sandbox.workspace import SandboxWorkspace
from tests.workspace.test_local import _template, provider  # noqa: F401  (the fixture is used by name)

HARD_BOUND_S = 5.0


def _record() -> SessionMessageRecord:
    return SessionMessageRecord(
        seq=1, kind=SessionMessageKind.ASSISTANT_TOKEN, payload={"text": "a"}, created_at=datetime.now(timezone.utc),
    )


async def test_the_local_workspace_does_not_count_the_wait_for_the_messages_lock(provider, monkeypatch) -> None:
    monkeypatch.setattr(persistence, "_WRITE_TIMEOUT_S", 0.2, raising=False)
    ws = await provider.create(_template())
    assert isinstance(ws, LocalWorkspace)
    sid = "sess-wc-local"
    writer = WorkspaceMessageWriter(workspace_io=ws, session_id=sid)
    await writer.append(_record())

    async with asyncio.timeout(HARD_BOUND_S):
        async with ws.state_repo.messages_lock(sid):                  # a turn persist holds it
            flushing = asyncio.create_task(writer.flush())
            await asyncio.sleep(0.5)
        await flushing

    path = ws.root / ws.template.state_path / "sessions" / sid / "messages.jsonl"
    assert path.read_bytes().count(b"\n") == 1


async def test_the_sandbox_workspace_does_not_count_the_wait_for_the_messages_lock(monkeypatch) -> None:
    monkeypatch.setattr(persistence, "_WRITE_TIMEOUT_S", 0.2, raising=False)
    lock = asyncio.Lock()
    appended: list[bytes] = []

    class _Repo:
        def messages_lock(self, session_id: str):
            return lock

    class _Sandbox:
        async def append_line(self, path: str, line: bytes) -> None:
            appended.append(line)

    ws = SandboxWorkspace.__new__(SandboxWorkspace)
    ws._workspace_root = "/ws"
    ws._template = SimpleNamespace(state_path=".state")
    ws._state_repo = _Repo()
    ws._sandbox = _Sandbox()
    writer = WorkspaceMessageWriter(workspace_io=ws, session_id="sess-wc-sandbox")
    await writer.append(_record())

    async with asyncio.timeout(HARD_BOUND_S):
        async with lock:
            flushing = asyncio.create_task(writer.flush())
            await asyncio.sleep(0.5)
        await flushing

    assert len(appended) == 1
