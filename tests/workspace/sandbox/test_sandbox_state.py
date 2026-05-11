"""Tests for SandboxStateRepo against FakeSandbox.

FakeSandbox shells out to host git, so these tests are skipped if
``git`` isn't on PATH.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from matrix.workspace.sandbox.fake import FakeSandbox
from matrix.workspace.sandbox.state import SandboxStateRepo


pytestmark = pytest.mark.skipif(
    shutil.which("git") is None,
    reason="git CLI not available on PATH (SandboxStateRepo needs it)",
)


@pytest.fixture
def sandbox(tmp_path: Path) -> FakeSandbox:
    return FakeSandbox(root=tmp_path)


@pytest.mark.asyncio
async def test_initialize_creates_git_repo(sandbox: FakeSandbox) -> None:
    repo = SandboxStateRepo(
        sandbox, state_path="/workspace/.state", workspace_id="ws-1",
    )
    await repo.initialize()
    info = await sandbox.stat("/workspace/.state/.git")
    assert info is not None
    assert info.kind == "dir"


@pytest.mark.asyncio
async def test_initialize_idempotent(sandbox: FakeSandbox) -> None:
    repo = SandboxStateRepo(
        sandbox, state_path="/workspace/.state", workspace_id="ws-1",
    )
    await repo.initialize()
    await repo.initialize()  # second call must not fail
    info = await sandbox.stat("/workspace/.state/.git")
    assert info is not None and info.kind == "dir"


@pytest.mark.asyncio
async def test_commit_turn_writes_files_and_returns_sha(
    sandbox: FakeSandbox,
) -> None:
    repo = SandboxStateRepo(
        sandbox, state_path="/workspace/.state", workspace_id="ws-1",
    )
    await repo.initialize()
    sha = await repo.commit_turn(
        session_id="sess-a",
        op="message",
        agent_id="agent-x",
        message_body="first turn",
        files={"messages.jsonl": b'{"role":"user","content":"hi"}\n'},
    )
    assert len(sha) == 40
    body = await sandbox.read_file(
        "/workspace/.state/sessions/sess-a/messages.jsonl"
    )
    assert b"hi" in body


@pytest.mark.asyncio
async def test_history_returns_recent_commits(sandbox: FakeSandbox) -> None:
    repo = SandboxStateRepo(
        sandbox, state_path="/workspace/.state", workspace_id="ws-1",
    )
    await repo.initialize()
    for i in range(3):
        await repo.commit_turn(
            session_id="sess-a", op="message", agent_id="agent-x",
            message_body=f"turn {i}",
            files={"messages.jsonl": f"{i}\n".encode()},
        )
    history = await repo.history(limit=10)
    assert len(history) >= 3
    subjects = [h.subject for h in history]
    assert "turn 2" in subjects


@pytest.mark.asyncio
async def test_history_carries_trailer_metadata(sandbox: FakeSandbox) -> None:
    repo = SandboxStateRepo(
        sandbox, state_path="/workspace/.state", workspace_id="ws-7",
    )
    await repo.initialize()
    await repo.commit_turn(
        session_id="sess-y", op="tool_call", agent_id="agent-z",
        tool_id="exec", call_id="c-1",
        message_body="call exec",
        files={"transcript.jsonl": b"x\n"},
    )
    history = await repo.history(limit=5)
    head = history[0]
    assert head.workspace_id == "ws-7"
    assert head.session_id == "sess-y"
    assert head.agent_id == "agent-z"
    assert head.op == "tool_call"
    assert head.tool == "exec"
    assert head.call_id == "c-1"
