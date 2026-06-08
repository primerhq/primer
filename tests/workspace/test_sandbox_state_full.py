"""Tests for the full SandboxStateRepo surface (Tasks 3.1 + 3.2).

Uses a mocked sandbox object whose ``state_commit`` / ``state_read`` /
``state_history`` are AsyncMocks and whose ``protocol_version`` is a
settable attribute.  No real container or git process is needed.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from primer.model.except_ import ValidationError
from primer.model.workspace_session import (
    AgentBinding,
    SessionInfo,
    SessionStatus,
)
from primer.workspace.sandbox.state import SandboxStateRepo


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


def _make_mock_sandbox(protocol_version: str = "1.1") -> MagicMock:
    """Return a MagicMock that satisfies _StateCapableSandbox.

    The three async state ops are AsyncMocks with safe default return
    values.  ``protocol_version`` is a plain attribute (not a property)
    so tests can reassign it freely.
    """
    m = MagicMock()
    m.protocol_version = protocol_version
    m.state_commit = AsyncMock(return_value="a" * 40)
    m.state_read = AsyncMock(return_value={})
    m.state_history = AsyncMock(return_value=[])
    return m


def _make_session_info(
    session_id: str = "sess-1",
    agent_id: str = "agent-1",
    workspace_id: str = "ws-1",
) -> SessionInfo:
    return SessionInfo(
        session_id=session_id,
        agent_id=agent_id,
        workspace_id=workspace_id,
        status=SessionStatus.RUNNING,
        started_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        last_activity_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )


def _make_agent_binding(
    agent_id: str = "agent-1",
    agent_name: str = "Test Agent",
) -> AgentBinding:
    return AgentBinding(
        agent_id=agent_id,
        agent_name=agent_name,
    )


def _make_repo(sandbox: object, workspace_id: str = "ws-1") -> SandboxStateRepo:
    return SandboxStateRepo(
        sandbox,
        state_path="/workspace/.state",
        workspace_id=workspace_id,
    )


# ---------------------------------------------------------------------------
# 3.1  Version guard
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_version_guard_create_session_raises_on_old_runtime() -> None:
    sandbox = _make_mock_sandbox(protocol_version="1.0")
    repo = _make_repo(sandbox)
    session_info = _make_session_info()
    agent_binding = _make_agent_binding()

    with pytest.raises(ValidationError, match="protocol.*1.0"):
        await repo.create_session(session_info, agent_binding)


@pytest.mark.asyncio
async def test_version_guard_commit_raises_on_old_runtime() -> None:
    sandbox = _make_mock_sandbox(protocol_version="0.9")
    repo = _make_repo(sandbox)
    # Need an agent in cache to get past the session lookup.
    repo._agent_by_session["sess-1"] = "agent-1"

    with pytest.raises(ValidationError, match="runtime.*too old"):
        await repo.commit(
            "sess-1",
            summary="test",
            op="message",
        )


@pytest.mark.asyncio
async def test_version_guard_commit_arbitrary_raises_on_old_runtime() -> None:
    sandbox = _make_mock_sandbox(protocol_version="0.0")
    repo = _make_repo(sandbox)

    with pytest.raises(ValidationError):
        await repo.commit_arbitrary(summary="test")


@pytest.mark.asyncio
async def test_version_guard_passes_on_exactly_1_1() -> None:
    sandbox = _make_mock_sandbox(protocol_version="1.1")
    repo = _make_repo(sandbox)
    repo._agent_by_session["sess-1"] = "agent-1"

    # Should NOT raise ValidationError.
    await repo.commit("sess-1", summary="test", op="message")
    assert sandbox.state_commit.called


@pytest.mark.asyncio
async def test_version_guard_passes_on_newer_protocol() -> None:
    sandbox = _make_mock_sandbox(protocol_version="2.0")
    repo = _make_repo(sandbox)
    repo._agent_by_session["sess-1"] = "agent-1"

    await repo.commit("sess-1", summary="test", op="message")
    assert sandbox.state_commit.called


@pytest.mark.asyncio
async def test_version_guard_non_state_sandbox_raises() -> None:
    """A plain object without state ops raises ValidationError."""
    class PlainSandbox:
        pass

    repo = SandboxStateRepo(
        PlainSandbox(),
        state_path="/workspace/.state",
        workspace_id="ws-1",
    )
    with pytest.raises(ValidationError, match="does not support"):
        await repo.create_session(
            _make_session_info(), _make_agent_binding()
        )


# ---------------------------------------------------------------------------
# 3.2  create_session
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_session_issues_one_state_commit() -> None:
    sandbox = _make_mock_sandbox()
    repo = _make_repo(sandbox, workspace_id="ws-42")

    session_info = _make_session_info(session_id="sess-abc", agent_id="agent-x")
    agent_binding = _make_agent_binding(agent_id="agent-x")

    sha = await repo.create_session(session_info, agent_binding)

    assert sha == "a" * 40
    sandbox.state_commit.assert_called_once()


@pytest.mark.asyncio
async def test_create_session_files_contain_session_and_agent_json() -> None:
    sandbox = _make_mock_sandbox()
    repo = _make_repo(sandbox, workspace_id="ws-42")

    session_info = _make_session_info(session_id="sess-abc", agent_id="agent-x")
    agent_binding = _make_agent_binding(agent_id="agent-x", agent_name="MyAgent")

    await repo.create_session(session_info, agent_binding)

    call_kwargs = sandbox.state_commit.call_args.kwargs
    files = call_kwargs["files"]

    assert "sessions/sess-abc/session.json" in files
    assert "sessions/sess-abc/agent.json" in files

    # Verify the JSON bodies round-trip through the models.
    parsed_session = SessionInfo.model_validate_json(files["sessions/sess-abc/session.json"])
    assert parsed_session.session_id == "sess-abc"
    assert parsed_session.agent_id == "agent-x"

    parsed_agent = AgentBinding.model_validate_json(files["sessions/sess-abc/agent.json"])
    assert parsed_agent.agent_id == "agent-x"
    assert parsed_agent.agent_name == "MyAgent"


@pytest.mark.asyncio
async def test_create_session_message_contains_attach_and_trailers() -> None:
    sandbox = _make_mock_sandbox()
    repo = _make_repo(sandbox, workspace_id="ws-42")

    session_info = _make_session_info(session_id="sess-abc", agent_id="agent-x")
    agent_binding = _make_agent_binding(agent_id="agent-x")

    await repo.create_session(session_info, agent_binding)

    call_kwargs = sandbox.state_commit.call_args.kwargs
    message = call_kwargs["message"]

    assert "attach" in message
    assert "X-Primer-Workspace: ws-42" in message
    assert "X-Primer-Session: sess-abc" in message
    assert "X-Primer-Agent: agent-x" in message
    assert "X-Primer-Op: attach" in message


@pytest.mark.asyncio
async def test_create_session_caches_agent_id() -> None:
    sandbox = _make_mock_sandbox()
    repo = _make_repo(sandbox)

    session_info = _make_session_info(session_id="sess-cache", agent_id="agt-y")
    agent_binding = _make_agent_binding(agent_id="agt-y")

    await repo.create_session(session_info, agent_binding)

    assert repo._agent_by_session.get("sess-cache") == "agt-y"


# ---------------------------------------------------------------------------
# commit
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_commit_issues_state_commit_with_files() -> None:
    sandbox = _make_mock_sandbox()
    repo = _make_repo(sandbox, workspace_id="ws-5")
    repo._agent_by_session["sess-1"] = "agent-1"

    sha = await repo.commit(
        "sess-1",
        summary="turn 1",
        op="message",
        files={"messages.jsonl": b'{"role":"user"}\n'},
    )

    assert sha == "a" * 40
    sandbox.state_commit.assert_called_once()
    call_kwargs = sandbox.state_commit.call_args.kwargs
    files = call_kwargs["files"]
    assert "sessions/sess-1/messages.jsonl" in files
    assert files["sessions/sess-1/messages.jsonl"] == b'{"role":"user"}\n'


@pytest.mark.asyncio
async def test_commit_message_has_correct_trailers() -> None:
    sandbox = _make_mock_sandbox()
    repo = _make_repo(sandbox, workspace_id="ws-5")
    repo._agent_by_session["sess-1"] = "agent-1"

    await repo.commit(
        "sess-1",
        summary="tool call",
        op="tool_call",
        tool="exec",
        call_id="call-99",
    )

    message = sandbox.state_commit.call_args.kwargs["message"]
    assert "X-Primer-Workspace: ws-5" in message
    assert "X-Primer-Session: sess-1" in message
    assert "X-Primer-Agent: agent-1" in message
    assert "X-Primer-Op: tool_call" in message
    assert "X-Primer-Tool: exec" in message
    assert "X-Primer-Call: call-99" in message


@pytest.mark.asyncio
async def test_commit_str_files_encoded_to_utf8() -> None:
    sandbox = _make_mock_sandbox()
    repo = _make_repo(sandbox)
    repo._agent_by_session["sess-1"] = "agent-1"

    await repo.commit(
        "sess-1",
        summary="s",
        op="message",
        files={"note.txt": "hello world"},
    )

    files = sandbox.state_commit.call_args.kwargs["files"]
    assert files["sessions/sess-1/note.txt"] == b"hello world"


@pytest.mark.asyncio
async def test_commit_delete_files_sent_in_deletes() -> None:
    sandbox = _make_mock_sandbox()
    repo = _make_repo(sandbox)
    repo._agent_by_session["sess-1"] = "agent-1"

    await repo.commit(
        "sess-1",
        summary="rm waiting",
        op="status_change",
        delete_files=["waiting.json"],
    )

    call_kwargs = sandbox.state_commit.call_args.kwargs
    assert "sessions/sess-1/waiting.json" in call_kwargs["deletes"]


@pytest.mark.asyncio
async def test_commit_raises_lookup_error_for_unknown_session() -> None:
    sandbox = _make_mock_sandbox()
    repo = _make_repo(sandbox)

    with pytest.raises(LookupError, match="unknown"):
        await repo.commit("no-such-session", summary="x", op="message")


@pytest.mark.asyncio
async def test_commit_invalid_op_raises_value_error() -> None:
    sandbox = _make_mock_sandbox()
    repo = _make_repo(sandbox)
    repo._agent_by_session["sess-1"] = "agent-1"

    with pytest.raises(ValueError, match="unknown op"):
        await repo.commit("sess-1", summary="x", op="bogus_op")  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# commit_arbitrary
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_commit_arbitrary_sends_correct_files() -> None:
    sandbox = _make_mock_sandbox()
    repo = _make_repo(sandbox, workspace_id="ws-arb")

    sha = await repo.commit_arbitrary(
        summary="graph state",
        files={"graphs/gs-1/state.json": b'{"status":"done"}'},
        trailers={"X-Custom": "value"},
    )

    assert sha == "a" * 40
    call_kwargs = sandbox.state_commit.call_args.kwargs
    assert "graphs/gs-1/state.json" in call_kwargs["files"]
    assert call_kwargs["files"]["graphs/gs-1/state.json"] == b'{"status":"done"}'
    assert "X-Primer-Workspace: ws-arb" in call_kwargs["message"]
    assert "X-Custom: value" in call_kwargs["message"]


@pytest.mark.asyncio
async def test_commit_arbitrary_delete_files() -> None:
    sandbox = _make_mock_sandbox()
    repo = _make_repo(sandbox)

    await repo.commit_arbitrary(
        summary="remove old",
        delete_files=["graphs/gs-1/old.json"],
    )

    call_kwargs = sandbox.state_commit.call_args.kwargs
    assert "graphs/gs-1/old.json" in call_kwargs["deletes"]


# ---------------------------------------------------------------------------
# load_session_info / load_agent_binding / load_waiting_state
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_load_session_info_parses_bytes() -> None:
    sandbox = _make_mock_sandbox()
    repo = _make_repo(sandbox)

    session_info = _make_session_info(session_id="sess-load", agent_id="agt-z")
    raw = session_info.model_dump_json(indent=2).encode()
    sandbox.state_read.return_value = {"sessions/sess-load/session.json": raw}

    result = await repo.load_session_info("sess-load")

    assert result is not None
    assert result.session_id == "sess-load"
    assert result.agent_id == "agt-z"
    sandbox.state_read.assert_called_once_with(["sessions/sess-load/session.json"])


@pytest.mark.asyncio
async def test_load_session_info_returns_none_when_absent() -> None:
    sandbox = _make_mock_sandbox()
    repo = _make_repo(sandbox)
    sandbox.state_read.return_value = {"sessions/sess-miss/session.json": None}

    result = await repo.load_session_info("sess-miss")
    assert result is None


@pytest.mark.asyncio
async def test_load_agent_binding_parses_bytes() -> None:
    sandbox = _make_mock_sandbox()
    repo = _make_repo(sandbox)

    binding = _make_agent_binding(agent_id="agt-b", agent_name="Bot")
    raw = binding.model_dump_json(indent=2).encode()
    sandbox.state_read.return_value = {"sessions/sess-1/agent.json": raw}

    result = await repo.load_agent_binding("sess-1")

    assert result is not None
    assert result.agent_id == "agt-b"
    assert result.agent_name == "Bot"


@pytest.mark.asyncio
async def test_load_agent_binding_returns_none_when_absent() -> None:
    sandbox = _make_mock_sandbox()
    repo = _make_repo(sandbox)
    sandbox.state_read.return_value = {"sessions/sess-1/agent.json": None}

    result = await repo.load_agent_binding("sess-1")
    assert result is None


@pytest.mark.asyncio
async def test_load_waiting_state_returns_none_when_absent() -> None:
    sandbox = _make_mock_sandbox()
    repo = _make_repo(sandbox)
    sandbox.state_read.return_value = {"sessions/sess-1/waiting.json": None}

    result = await repo.load_waiting_state("sess-1")
    assert result is None


@pytest.mark.asyncio
async def test_load_waiting_state_calls_correct_path() -> None:
    sandbox = _make_mock_sandbox()
    repo = _make_repo(sandbox)
    sandbox.state_read.return_value = {"sessions/sess-w/waiting.json": None}

    await repo.load_waiting_state("sess-w")

    sandbox.state_read.assert_called_once_with(["sessions/sess-w/waiting.json"])


# ---------------------------------------------------------------------------
# read_state_file
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_read_state_file_returns_bytes() -> None:
    sandbox = _make_mock_sandbox()
    repo = _make_repo(sandbox)
    sandbox.state_read.return_value = {"graphs/gs-1/state.json": b'{"x":1}'}

    result = await repo.read_state_file("graphs/gs-1/state.json")

    assert result == b'{"x":1}'
    sandbox.state_read.assert_called_once_with(["graphs/gs-1/state.json"])


@pytest.mark.asyncio
async def test_read_state_file_returns_none_when_absent() -> None:
    sandbox = _make_mock_sandbox()
    repo = _make_repo(sandbox)
    sandbox.state_read.return_value = {"some/path.txt": None}

    result = await repo.read_state_file("some/path.txt")
    assert result is None


# ---------------------------------------------------------------------------
# history
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_history_delegates_to_state_history() -> None:
    sandbox = _make_mock_sandbox()
    repo = _make_repo(sandbox, workspace_id="ws-h")

    raw_commits = [
        {
            "sha": "b" * 40,
            "subject": "sess-1: attach",
            "committed_at": "2026-01-01T00:00:00+00:00",
            "trailers": {
                "X-Primer-Workspace": "ws-h",
                "X-Primer-Session": "sess-1",
                "X-Primer-Agent": "agent-1",
                "X-Primer-Op": "attach",
            },
        }
    ]
    sandbox.state_history.return_value = raw_commits

    commits = await repo.history(session_id="sess-1", limit=10)

    sandbox.state_history.assert_called_once_with(
        session_id="sess-1", agent_id=None, limit=10
    )
    assert len(commits) == 1
    c = commits[0]
    assert c.sha == "b" * 40
    assert c.subject == "sess-1: attach"
    assert c.workspace_id == "ws-h"
    assert c.session_id == "sess-1"
    assert c.agent_id == "agent-1"
    assert c.op == "attach"


@pytest.mark.asyncio
async def test_history_maps_unix_timestamp() -> None:
    sandbox = _make_mock_sandbox()
    repo = _make_repo(sandbox)

    raw_commits = [
        {
            "sha": "c" * 40,
            "subject": "test",
            "committed_at": 1735689600.0,  # unix ts
            "trailers": {},
        }
    ]
    sandbox.state_history.return_value = raw_commits

    commits = await repo.history()
    assert len(commits) == 1
    assert commits[0].committed_at == datetime.fromtimestamp(1735689600.0, tz=timezone.utc)


# ---------------------------------------------------------------------------
# Commit serialisation: concurrent commits go through the lock
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_concurrent_commits_serialised_through_lock() -> None:
    """Verify that concurrent commits do not interleave.

    Two coroutines call commit() simultaneously; the lock must ensure the
    actual state_commit calls are serialised.  We verify this by recording
    the order in which the mock was entered and confirming neither call
    overlapped.
    """
    sandbox = _make_mock_sandbox()
    repo = _make_repo(sandbox, workspace_id="ws-lock")
    repo._agent_by_session["sess-1"] = "agent-1"
    repo._agent_by_session["sess-2"] = "agent-1"

    call_order: list[str] = []
    lock_held_during: list[list[str]] = []

    original_state_commit = sandbox.state_commit

    async def recording_state_commit(**kwargs: Any) -> str:
        call_order.append("enter")
        # Brief yield to give the other coroutine a chance to run
        await asyncio.sleep(0)
        lock_held_during.append(list(call_order))
        call_order.append("exit")
        return "a" * 40

    sandbox.state_commit = recording_state_commit

    # Launch two concurrent commits
    results = await asyncio.gather(
        repo.commit("sess-1", summary="turn-1", op="message"),
        repo.commit("sess-2", summary="turn-2", op="message"),
    )

    assert len(results) == 2

    # Verify that calls were serialised: the second "enter" should have
    # appeared only after the first "exit".
    # call_order should be: enter, exit, enter, exit (no interleave).
    assert call_order == ["enter", "exit", "enter", "exit"], (
        f"Expected serialised call order, got: {call_order}"
    )


@pytest.mark.asyncio
async def test_commit_lock_exists() -> None:
    """The _commit_lock attribute exists and is an asyncio.Lock."""
    sandbox = _make_mock_sandbox()
    repo = _make_repo(sandbox)
    assert isinstance(repo._commit_lock, asyncio.Lock)


# ---------------------------------------------------------------------------
# show_commit raises NotImplementedError
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_show_commit_raises_not_implemented() -> None:
    sandbox = _make_mock_sandbox()
    repo = _make_repo(sandbox)

    with pytest.raises(NotImplementedError):
        await repo.show_commit("a" * 40)


# ---------------------------------------------------------------------------
# initialize is a no-op (no crash)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_initialize_does_not_crash() -> None:
    sandbox = _make_mock_sandbox()
    repo = _make_repo(sandbox)
    await repo.initialize()  # must not raise
