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
