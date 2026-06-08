"""Tests for RuntimeClient state_commit / state_read / state_history methods.

Uses monkeypatching of _send_request to avoid a real WS connection.
"""

from __future__ import annotations

import base64
from typing import Any
from unittest.mock import AsyncMock

import pytest

from primer.workspace.runtime.protocol import OpName
from primer.workspace.runtime.runtime_client import RuntimeClient


@pytest.fixture()
def client() -> RuntimeClient:
    return RuntimeClient(url="ws://localhost:0/", token="test-token")


# ---------------------------------------------------------------------------
# state_commit
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_state_commit_encodes_files_and_returns_sha(client: RuntimeClient) -> None:
    mock = AsyncMock(return_value={"sha": "abc123def456abc123def456abc123def456abc1"})
    client._send_request = mock  # type: ignore[method-assign]

    sha = await client.state_commit(
        files={"a.txt": b"hello"},
        deletes=["b.txt"],
        message="m",
        allow_empty=True,
    )

    assert sha == "abc123def456abc123def456abc123def456abc1"
    mock.assert_called_once()
    op, args = mock.call_args.args
    assert op == OpName.STATE_COMMIT
    assert args["files"]["a.txt"] == base64.b64encode(b"hello").decode()
    assert args["deletes"] == ["b.txt"]
    assert args["message"] == "m"
    assert args["allow_empty"] is True


@pytest.mark.asyncio
async def test_state_commit_allow_empty_defaults_false(client: RuntimeClient) -> None:
    mock = AsyncMock(return_value={"sha": "0" * 40})
    client._send_request = mock  # type: ignore[method-assign]

    await client.state_commit(files={}, deletes=[], message="empty")

    _, args = mock.call_args.args
    assert args["allow_empty"] is False


# ---------------------------------------------------------------------------
# state_read
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_state_read_decodes_content_and_preserves_none(client: RuntimeClient) -> None:
    hi_b64 = base64.b64encode(b"hi").decode()
    mock = AsyncMock(return_value={"files": {"a.txt": hi_b64, "missing": None}})
    client._send_request = mock  # type: ignore[method-assign]

    result = await client.state_read(["a.txt", "missing"])

    mock.assert_called_once()
    op, args = mock.call_args.args
    assert op == OpName.STATE_READ
    assert args == {"paths": ["a.txt", "missing"]}
    assert result == {"a.txt": b"hi", "missing": None}


# ---------------------------------------------------------------------------
# state_history
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_state_history_passes_args_and_returns_commits(client: RuntimeClient) -> None:
    commits = [
        {
            "sha": "a" * 40,
            "subject": "init",
            "committed_at": "2026-06-08T00:00:00Z",
            "workspace_id": "ws1",
            "session_id": "s1",
            "agent_id": None,
            "op": "state_commit",
            "tool": None,
            "call_id": None,
        }
    ]
    mock = AsyncMock(return_value={"commits": commits})
    client._send_request = mock  # type: ignore[method-assign]

    result = await client.state_history(session_id="s1", limit=5)

    mock.assert_called_once()
    op, args = mock.call_args.args
    assert op == OpName.STATE_HISTORY
    assert args == {"limit": 5, "session_id": "s1", "agent_id": None}
    assert result == commits


@pytest.mark.asyncio
async def test_state_history_defaults(client: RuntimeClient) -> None:
    mock = AsyncMock(return_value={"commits": []})
    client._send_request = mock  # type: ignore[method-assign]

    result = await client.state_history()

    _, args = mock.call_args.args
    assert args == {"limit": 50, "session_id": None, "agent_id": None}
    assert result == []
