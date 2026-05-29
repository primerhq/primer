"""Tests for the Workspace.ping() liveness probe.

Phase 7 probe task calls ``Workspace.ping()`` at ~30s intervals to drive
phase transitions.  This module verifies:

* The :class:`Workspace` ABC declares ``ping`` as abstract.
* :class:`WSSandbox.ping()` returns True / False based on the underlying
  :class:`RuntimeClient.ping()` outcome.
* :class:`LocalWorkspace.ping()` returns True iff its root directory
  exists on disk.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest


# ---------------------------------------------------------------------------
# ABC contract
# ---------------------------------------------------------------------------


def test_workspace_abc_has_ping() -> None:
    """The Workspace ABC declares the ping() abstract method."""
    from primer.int.workspace import Workspace

    assert hasattr(Workspace, "ping")
    assert "ping" in Workspace.__abstractmethods__


# ---------------------------------------------------------------------------
# WSSandbox.ping (Sandbox impl backing SandboxWorkspace)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ws_sandbox_ping_returns_true_on_success() -> None:
    """WSSandbox.ping() returns True when RuntimeClient.ping() succeeds."""
    from primer.workspace.runtime.ws_sandbox import WSSandbox

    client = MagicMock()
    client.ping = AsyncMock(return_value=None)
    sb = WSSandbox(runtime_client=client, container_id="c1")

    assert await sb.ping() is True
    client.ping.assert_awaited_once()


@pytest.mark.asyncio
async def test_ws_sandbox_ping_returns_false_on_error() -> None:
    """WSSandbox.ping() returns False when RuntimeClient.ping() raises."""
    from primer.workspace.runtime.ws_sandbox import WSSandbox

    client = MagicMock()
    client.ping = AsyncMock(side_effect=RuntimeError("disconnected"))
    sb = WSSandbox(runtime_client=client, container_id="c1")

    assert await sb.ping() is False


# ---------------------------------------------------------------------------
# RuntimeClient.ping helper
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_runtime_client_ping_uses_health_op() -> None:
    """RuntimeClient.ping() sends a HEALTH request via _send_request."""
    from primer.workspace.runtime.protocol import OpName
    from primer.workspace.runtime.runtime_client import RuntimeClient

    client = RuntimeClient.__new__(RuntimeClient)
    client._send_request = AsyncMock(return_value={"version": "1.0.0"})  # type: ignore[attr-defined]

    result = await client.ping()
    assert result is None or isinstance(result, dict)
    client._send_request.assert_awaited_once()  # type: ignore[attr-defined]
    args, _ = client._send_request.await_args  # type: ignore[attr-defined]
    assert args[0] is OpName.HEALTH


# ---------------------------------------------------------------------------
# LocalWorkspace.ping
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_local_workspace_ping_when_root_exists(tmp_path: Path) -> None:
    """LocalWorkspace.ping() returns True when its root directory exists."""
    from primer.workspace.local.workspace import LocalWorkspace

    ws = LocalWorkspace.__new__(LocalWorkspace)
    ws._root = tmp_path  # type: ignore[attr-defined]

    assert await ws.ping() is True


@pytest.mark.asyncio
async def test_local_workspace_ping_when_root_missing(tmp_path: Path) -> None:
    """LocalWorkspace.ping() returns False when its root is gone."""
    from primer.workspace.local.workspace import LocalWorkspace

    ws = LocalWorkspace.__new__(LocalWorkspace)
    ws._root = tmp_path / "does-not-exist"  # type: ignore[attr-defined]

    assert await ws.ping() is False
