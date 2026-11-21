"""Smoke tests for SandboxStateRepo basic API surface.

The full behaviour is covered by tests/workspace/test_sandbox_state_full.py
which uses a mocked WSSandbox.  These tests verify the remaining stable
surface (constructors, attribute access, no-crash initialize).
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from primer.workspace.sandbox.state import SandboxStateRepo


def _make_mock_sandbox(protocol_version: str = "1.1") -> MagicMock:
    m = MagicMock()
    m.protocol_version = protocol_version
    m.state_commit = AsyncMock(return_value="a" * 40)
    m.state_read = AsyncMock(return_value={})
    m.state_history = AsyncMock(return_value=[])
    return m


@pytest.mark.asyncio
async def test_initialize_does_not_raise() -> None:
    """initialize() is a no-op; must not crash on any sandbox type."""
    sandbox = _make_mock_sandbox()
    repo = SandboxStateRepo(
        sandbox, state_path="/workspace/.state", workspace_id="ws-1",
    )
    await repo.initialize()


def test_workspace_id_property() -> None:
    sandbox = _make_mock_sandbox()
    repo = SandboxStateRepo(
        sandbox, state_path="/workspace/.state", workspace_id="ws-42",
    )
    assert repo.workspace_id == "ws-42"


def test_state_path_property() -> None:
    sandbox = _make_mock_sandbox()
    repo = SandboxStateRepo(
        sandbox, state_path="/data/.state", workspace_id="ws-1",
    )
    assert repo.state_path == "/data/.state"


def test_empty_workspace_id_raises() -> None:
    sandbox = _make_mock_sandbox()
    with pytest.raises(ValueError, match="workspace_id"):
        SandboxStateRepo(sandbox, state_path="/workspace/.state", workspace_id="")


# ===========================================================================
# list_session_ids -- the enumeration source for cross-process rehydration
# ===========================================================================


@pytest.mark.asyncio
async def test_list_session_ids_from_flat_history() -> None:
    """The real runtime returns flat ``session_id`` fields; distinct,
    non-empty ids are collected (newest-first order preserved)."""
    sandbox = _make_mock_sandbox()
    sandbox.state_history = AsyncMock(return_value=[
        {"sha": "c3", "session_id": "sess-b", "agent_id": "ag-2", "op": "turn"},
        {"sha": "c2", "session_id": "sess-a", "agent_id": "ag-1", "op": "turn"},
        {"sha": "c1", "session_id": "sess-a", "agent_id": "ag-1", "op": "attach"},
    ])
    repo = SandboxStateRepo(
        sandbox, state_path="/workspace/.state", workspace_id="ws-1",
    )
    ids = await repo.list_session_ids()
    assert ids == ["sess-b", "sess-a"]


@pytest.mark.asyncio
async def test_list_session_ids_from_nested_trailers() -> None:
    """Falls back to the nested ``trailers`` dict shape."""
    sandbox = _make_mock_sandbox()
    sandbox.state_history = AsyncMock(return_value=[
        {"sha": "c1", "trailers": {
            "X-Primer-Session": "sess-x", "X-Primer-Agent": "ag-x",
        }},
    ])
    repo = SandboxStateRepo(
        sandbox, state_path="/workspace/.state", workspace_id="ws-1",
    )
    assert await repo.list_session_ids() == ["sess-x"]


@pytest.mark.asyncio
async def test_list_session_ids_skips_commits_without_session() -> None:
    """Commits with no session trailer (e.g. arbitrary graph commits) are
    ignored rather than producing empty-string ids."""
    sandbox = _make_mock_sandbox()
    sandbox.state_history = AsyncMock(return_value=[
        {"sha": "c2", "op": "graph_state"},
        {"sha": "c1", "session_id": "sess-only", "op": "attach"},
    ])
    repo = SandboxStateRepo(
        sandbox, state_path="/workspace/.state", workspace_id="ws-1",
    )
    assert await repo.list_session_ids() == ["sess-only"]


@pytest.mark.asyncio
async def test_list_session_ids_empty_history() -> None:
    sandbox = _make_mock_sandbox()
    repo = SandboxStateRepo(
        sandbox, state_path="/workspace/.state", workspace_id="ws-1",
    )
    assert await repo.list_session_ids() == []


@pytest.mark.parametrize("version,with_files", [("1.5", False), ("1.5", True), ("1.6", False)])
@pytest.mark.parametrize("files", [["session.json"], [{"path": "session.json", "additions": 1}]])
async def test_history_ignores_unrequested_or_unsupported_file_metadata(version, with_files, files):
    sandbox = _make_mock_sandbox(version)
    sandbox.state_history.return_value = [
        {"sha": "a" * 40, "session_id": "sess-a", "agent_id": "agent-a", "files": files},
    ]
    repo = SandboxStateRepo(sandbox, state_path="/workspace/.state", workspace_id="ws-1")
    commits = await repo.history(with_files=with_files)
    assert len(commits) == 1
    assert commits[0].session_id == "sess-a"
    assert commits[0].files is None
    sandbox.state_history.assert_awaited_once_with(session_id=None, agent_id=None, limit=100)
    assert await repo.list_session_ids() == ["sess-a"]


async def test_history_maps_requested_supported_file_counts():
    sandbox = _make_mock_sandbox("1.6")
    sandbox.state_history.return_value = [{
        "sha": "a" * 40, "session_id": "sess-a",
        "files": [{"path": "session.json", "additions": 3, "deletions": 2, "binary": False}],
    }]
    repo = SandboxStateRepo(sandbox, state_path="/workspace/.state", workspace_id="ws-1")
    commits = await repo.history(with_files=True)
    assert len(commits) == 1
    assert commits[0].files is not None
    assert [file.model_dump() for file in commits[0].files] == sandbox.state_history.return_value[0]["files"]
    sandbox.state_history.assert_awaited_once_with(session_id=None, agent_id=None, limit=100, with_files=True)


async def test_requested_file_metadata_still_rejects_invalid_rows():
    from pydantic import ValidationError

    sandbox = _make_mock_sandbox("1.6")
    sandbox.state_history.return_value = [{"sha": "a" * 40, "files": ["session.json"]}]
    repo = SandboxStateRepo(sandbox, state_path="/workspace/.state", workspace_id="ws-1")
    with pytest.raises(ValidationError, match="files.0"):
        await repo.history(with_files=True)
