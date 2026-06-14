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
