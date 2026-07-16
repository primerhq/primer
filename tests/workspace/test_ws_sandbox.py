"""Tests for WSSandbox -- the Sandbox ABC impl backed by RuntimeClient.

Uses unittest.mock to fake the RuntimeClient so no real WS connection
is required.  Each test verifies that the sandbox method routes correctly
to the expected client method with the expected (resolved) path arguments.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import datetime, timezone
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from primer.int.sandbox import ExecResult, FileStat, SandboxInspectInfo
from primer.workspace.runtime.ws_sandbox import WSSandbox


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_file_stat(path: str = "/workspace/a.txt") -> FileStat:
    return FileStat(
        path=path,
        kind="file",
        size_bytes=4,
        mode=0o644,
        modified_at=datetime.now(tz=timezone.utc),
    )


def _make_client() -> MagicMock:
    """Return a MagicMock that looks like a RuntimeClient with async methods."""
    client = MagicMock()
    client.read_file = AsyncMock(return_value=b"hello")
    client.write_file = AsyncMock(return_value=None)
    client.append_line = AsyncMock(return_value=5)
    client.list_dir = AsyncMock(return_value=[_make_file_stat()])
    client.stat = AsyncMock(return_value=_make_file_stat())
    client.delete = AsyncMock(return_value=None)
    client.exec = AsyncMock(
        return_value=ExecResult(exit_code=0, stdout="out", stderr="", duration_seconds=0.1)
    )
    client._send_request = AsyncMock(
        return_value={
            "version": "1.0.0",
            "uptime_s": 42.0,
            "watches_active": 0,
            "execs_running": 0,
        }
    )

    # archive is an async generator; create a helper
    async def _fake_archive(paths: list[str]) -> AsyncIterator[bytes]:
        yield b"chunk1"
        yield b"chunk2"

    client.archive = _fake_archive
    return client


def _make_sandbox(
    workspace_root: str = "/workspace",
    container_id: str = "test-container",
) -> tuple[WSSandbox, MagicMock]:
    client = _make_client()
    sb = WSSandbox(
        runtime_client=client,
        container_id=container_id,
        workspace_root=workspace_root,
    )
    return sb, client


# ---------------------------------------------------------------------------
# id
# ---------------------------------------------------------------------------


def test_id_returns_container_id() -> None:
    sb, _ = _make_sandbox(container_id="abc123")
    assert sb.id == "abc123"


# ---------------------------------------------------------------------------
# Path resolution
# ---------------------------------------------------------------------------


def test_resolve_absolute_path_unchanged() -> None:
    sb, _ = _make_sandbox()
    assert sb._resolve("/etc/hosts") == "/etc/hosts"


def test_resolve_relative_path_prepends_workspace_root() -> None:
    sb, _ = _make_sandbox(workspace_root="/workspace")
    assert sb._resolve("foo.txt") == "/workspace/foo.txt"


def test_resolve_relative_path_custom_root() -> None:
    sb, _ = _make_sandbox(workspace_root="/data/ws")
    assert sb._resolve("subdir/bar.txt") == "/data/ws/subdir/bar.txt"


def test_resolve_trailing_slash_stripped_from_root() -> None:
    """workspace_root with trailing slash should not produce double slash."""
    sb, _ = _make_sandbox(workspace_root="/workspace/")
    assert sb._resolve("file.txt") == "/workspace/file.txt"


# ---------------------------------------------------------------------------
# exec
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_exec_routes_to_client() -> None:
    sb, client = _make_sandbox()
    result = await sb.exec(["ls", "-la"], workdir="/tmp", timeout_seconds=10.0)
    client.exec.assert_awaited_once_with(
        ["ls", "-la"],
        workdir="/tmp",
        env=None,
        timeout_s=10.0,
        stdin=None,
        abort=None,
        access="write",
        writes=None,
    )
    assert result.exit_code == 0
    assert result.stdout == "out"


@pytest.mark.asyncio
async def test_exec_passes_env_and_stdin() -> None:
    sb, client = _make_sandbox()
    abort = asyncio.Event()
    await sb.exec(
        "echo hi",
        env={"FOO": "bar"},
        stdin=b"input",
        abort=abort,
    )
    client.exec.assert_awaited_once_with(
        "echo hi",
        workdir="/workspace",
        env={"FOO": "bar"},
        timeout_s=None,
        stdin=b"input",
        abort=abort,
        access="write",
        writes=None,
    )


# ---------------------------------------------------------------------------
# read_file
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_read_file_absolute_path() -> None:
    sb, client = _make_sandbox()
    data = await sb.read_file("/workspace/hello.txt")
    client.read_file.assert_awaited_once_with("/workspace/hello.txt")
    assert data == b"hello"


@pytest.mark.asyncio
async def test_read_file_relative_path_resolved() -> None:
    sb, client = _make_sandbox(workspace_root="/workspace")
    await sb.read_file("hello.txt")
    client.read_file.assert_awaited_once_with("/workspace/hello.txt")


# ---------------------------------------------------------------------------
# write_file
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_write_file_absolute_path() -> None:
    sb, client = _make_sandbox()
    await sb.write_file("/workspace/out.txt", b"data")
    client.write_file.assert_awaited_once_with("/workspace/out.txt", b"data", mode=None)


@pytest.mark.asyncio
async def test_write_file_relative_path_resolved() -> None:
    sb, client = _make_sandbox(workspace_root="/workspace")
    await sb.write_file("out.txt", b"data", mode=0o600)
    client.write_file.assert_awaited_once_with("/workspace/out.txt", b"data", mode=0o600)


# ---------------------------------------------------------------------------
# append_line (native atomic op via client)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_append_line_routes_to_client() -> None:
    sb, client = _make_sandbox()
    offset = await sb.append_line("log.txt", b"a log entry")
    client.append_line.assert_awaited_once_with("/workspace/log.txt", b"a log entry")
    assert offset == 5


@pytest.mark.asyncio
async def test_append_line_absolute_path_not_modified() -> None:
    sb, client = _make_sandbox()
    await sb.append_line("/var/log/app.log", b"msg")
    client.append_line.assert_awaited_once_with("/var/log/app.log", b"msg")


# ---------------------------------------------------------------------------
# append_file (read-modify-write fallback through runtime)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_append_file_reads_then_writes() -> None:
    """append_file fetches existing content and writes concatenation."""
    sb, client = _make_sandbox()
    # Seed existing content via the mock
    client.read_file = AsyncMock(return_value=b"existing\n")
    await sb.append_file("data.txt", b"appended")
    client.read_file.assert_awaited_once_with("/workspace/data.txt")
    client.write_file.assert_awaited_once_with(
        "/workspace/data.txt", b"existing\nappended", mode=None
    )


@pytest.mark.asyncio
async def test_append_file_creates_on_missing() -> None:
    """append_file starts from empty when read_file raises."""
    sb, client = _make_sandbox()
    client.read_file = AsyncMock(side_effect=FileNotFoundError("not found"))
    await sb.append_file("new.txt", b"first write")
    client.write_file.assert_awaited_once_with(
        "/workspace/new.txt", b"first write", mode=None
    )


# ---------------------------------------------------------------------------
# list_dir
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_dir_routes_to_client() -> None:
    sb, client = _make_sandbox()
    entries = await sb.list_dir("subdir")
    client.list_dir.assert_awaited_once_with("/workspace/subdir")
    assert len(entries) == 1


# ---------------------------------------------------------------------------
# stat
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stat_routes_to_client_and_returns_filestat() -> None:
    sb, client = _make_sandbox()
    info = await sb.stat("file.txt")
    client.stat.assert_awaited_once_with("/workspace/file.txt")
    assert info is not None
    assert info.size_bytes == 4


@pytest.mark.asyncio
async def test_stat_returns_none_for_missing() -> None:
    sb, client = _make_sandbox()
    client.stat = AsyncMock(return_value=None)
    result = await sb.stat("missing.txt")
    assert result is None


# ---------------------------------------------------------------------------
# delete
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_delete_routes_to_client() -> None:
    sb, client = _make_sandbox()
    await sb.delete("old.txt")
    client.delete.assert_awaited_once_with("/workspace/old.txt")


# ---------------------------------------------------------------------------
# archive
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_archive_resolves_paths_and_streams_chunks() -> None:
    sb, client = _make_sandbox()
    chunks = []
    async for chunk in sb.archive(["a.tar", "/abs/b.tar"]):
        chunks.append(chunk)
    assert b"chunk1" in chunks
    assert b"chunk2" in chunks


# ---------------------------------------------------------------------------
# inspect
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_inspect_returns_running_when_health_ok() -> None:
    sb, client = _make_sandbox()
    info = await sb.inspect()
    assert info.state == "running"
    assert info.detail.get("version") == "1.0.0"
    assert info.detail.get("uptime_s") == 42.0


@pytest.mark.asyncio
async def test_inspect_returns_unknown_when_health_fails() -> None:
    sb, client = _make_sandbox()
    client._send_request = AsyncMock(side_effect=RuntimeError("connection lost"))
    info = await sb.inspect()
    assert info.state == "unknown"


# ---------------------------------------------------------------------------
# stop / remove raise NotImplementedError
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stop_raises_not_implemented() -> None:
    sb, _ = _make_sandbox()
    with pytest.raises(NotImplementedError):
        await sb.stop()


@pytest.mark.asyncio
async def test_remove_raises_not_implemented() -> None:
    sb, _ = _make_sandbox()
    with pytest.raises(NotImplementedError):
        await sb.remove()


# ---------------------------------------------------------------------------
# Sandbox ABC conformance: WSSandbox is a concrete Sandbox subclass
# ---------------------------------------------------------------------------


def test_ws_sandbox_is_sandbox_subclass() -> None:
    from primer.int.sandbox import Sandbox

    assert issubclass(WSSandbox, Sandbox)


def test_ws_sandbox_instantiates_without_error() -> None:
    client = _make_client()
    sb = WSSandbox(
        runtime_client=client,
        container_id="cid-xyz",
        workspace_root="/workspace",
    )
    assert sb.id == "cid-xyz"
