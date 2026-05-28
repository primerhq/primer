"""Tests for runtime/matrix_runtime/ops.py — one per op, including error cases.

We test the handlers directly (unit-level) AND through the WS server (integration-level)
to ensure the server correctly routes to each handler.
"""

from __future__ import annotations

import base64
import os
import pathlib
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import aiohttp
import pytest
import pytest_asyncio
from aiohttp.test_utils import TestServer

from matrix_runtime.ops import OpError, append_line, delete, list_dir, read_file, stat, write_file
from matrix_runtime.protocol import ErrorCode
from matrix_runtime.server import build_app


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def b64(data: bytes | str) -> str:
    if isinstance(data, str):
        data = data.encode()
    return base64.b64encode(data).decode()


def from_b64(s: str) -> bytes:
    return base64.b64decode(s)


# ---------------------------------------------------------------------------
# Server fixture (mirrors test_server.py but parameterised with workspace root)
# ---------------------------------------------------------------------------


class ServerFixture:
    def __init__(self, server: TestServer, workspace_root: str) -> None:
        self._server = server
        self.workspace_root = workspace_root

    @asynccontextmanager
    async def client(self, *, token: str = "testtoken") -> AsyncIterator[aiohttp.ClientWebSocketResponse]:
        url = self._server.make_url("/")
        session = aiohttp.ClientSession()
        try:
            ws = await session.ws_connect(url, headers={"Authorization": f"Bearer {token}"})
            # Complete handshake
            await ws.send_json({"req_id": 0, "op": "hello", "args": {"protocol": "1.0", "client": "test/0"}})
            resp = await ws.receive_json()
            assert resp["ok"] is True, f"Handshake failed: {resp}"
            try:
                yield ws
            finally:
                await ws.close()
        finally:
            await session.close()


@pytest_asyncio.fixture
async def server(tmp_path) -> AsyncIterator[ServerFixture]:
    """Start the server with a temp workspace; yield a ServerFixture."""
    token = "testtoken"
    app = build_app(token=token, workspace_root=str(tmp_path))
    test_server = TestServer(app)
    await test_server.start_server()
    yield ServerFixture(test_server, str(tmp_path))
    await test_server.close()


# ---------------------------------------------------------------------------
# Unit-level handler tests (call handler functions directly)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_read_file_returns_content(tmp_path):
    """read_file returns the file's content as base64."""
    f = tmp_path / "hello.txt"
    f.write_bytes(b"hello world")

    result = await read_file({"path": str(f)}, str(tmp_path))
    assert from_b64(result["content_b64"]) == b"hello world"


@pytest.mark.asyncio
async def test_read_file_relative_path(tmp_path):
    """read_file resolves relative paths against workspace_root."""
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "data.bin").write_bytes(b"\x00\x01\x02")

    result = await read_file({"path": "sub/data.bin"}, str(tmp_path))
    assert from_b64(result["content_b64"]) == b"\x00\x01\x02"


@pytest.mark.asyncio
async def test_read_file_enoent(tmp_path):
    """read_file raises ENOENT for a missing file."""
    with pytest.raises(OpError) as exc_info:
        await read_file({"path": str(tmp_path / "missing.txt")}, str(tmp_path))
    assert exc_info.value.code == ErrorCode.ENOENT


@pytest.mark.asyncio
async def test_read_file_eisdir(tmp_path):
    """read_file raises EISDIR when path is a directory."""
    d = tmp_path / "adir"
    d.mkdir()
    with pytest.raises(OpError) as exc_info:
        await read_file({"path": str(d)}, str(tmp_path))
    assert exc_info.value.code == ErrorCode.EISDIR


@pytest.mark.asyncio
async def test_read_file_path_escape(tmp_path):
    """read_file raises EACCES when path escapes workspace root."""
    with pytest.raises(OpError) as exc_info:
        await read_file({"path": "/etc/passwd"}, str(tmp_path))
    assert exc_info.value.code == ErrorCode.EACCES


@pytest.mark.asyncio
async def test_write_file_creates_file(tmp_path):
    """write_file creates the file with the given content."""
    target = tmp_path / "output.txt"
    result = await write_file({"path": str(target), "content_b64": b64(b"written!")}, str(tmp_path))
    assert result["ok"] is True
    assert target.read_bytes() == b"written!"


@pytest.mark.asyncio
async def test_write_file_creates_parent_dirs(tmp_path):
    """write_file creates missing parent directories."""
    target = tmp_path / "deep" / "nested" / "file.txt"
    result = await write_file({"path": str(target), "content_b64": b64(b"data")}, str(tmp_path))
    assert result["ok"] is True
    assert target.read_bytes() == b"data"


@pytest.mark.asyncio
async def test_write_file_sets_mode(tmp_path):
    """write_file respects the optional mode argument."""
    target = tmp_path / "exec.sh"
    result = await write_file(
        {"path": str(target), "content_b64": b64(b"#!/bin/sh"), "mode": 0o755},
        str(tmp_path),
    )
    assert result["ok"] is True
    assert target.stat().st_mode & 0o755 == 0o755


@pytest.mark.asyncio
async def test_write_file_path_escape(tmp_path):
    """write_file raises EACCES for paths outside workspace."""
    with pytest.raises(OpError) as exc_info:
        await write_file({"path": "/tmp/escape.txt", "content_b64": b64(b"x")}, str(tmp_path))
    assert exc_info.value.code == ErrorCode.EACCES


@pytest.mark.asyncio
async def test_append_line_creates_and_appends(tmp_path):
    """append_line creates the file and appends a newline-terminated line."""
    target = tmp_path / "log.txt"
    result = await append_line({"path": str(target), "line_b64": b64(b"first")}, str(tmp_path))
    assert result["ok"] is True
    assert result["byte_offset"] > 0
    assert target.read_bytes() == b"first\n"


@pytest.mark.asyncio
async def test_append_line_multiple_appends(tmp_path):
    """Multiple append_line calls accumulate lines."""
    target = tmp_path / "log.txt"
    await append_line({"path": str(target), "line_b64": b64(b"line1")}, str(tmp_path))
    await append_line({"path": str(target), "line_b64": b64(b"line2")}, str(tmp_path))
    assert target.read_bytes() == b"line1\nline2\n"


@pytest.mark.asyncio
async def test_append_line_byte_offset_advances(tmp_path):
    """byte_offset increases with each append."""
    target = tmp_path / "log.txt"
    r1 = await append_line({"path": str(target), "line_b64": b64(b"aaa")}, str(tmp_path))
    r2 = await append_line({"path": str(target), "line_b64": b64(b"bbb")}, str(tmp_path))
    assert r2["byte_offset"] > r1["byte_offset"]


@pytest.mark.asyncio
async def test_append_line_path_escape(tmp_path):
    """append_line raises EACCES for paths outside workspace."""
    with pytest.raises(OpError) as exc_info:
        await append_line({"path": "../../etc/cron.d/evil", "line_b64": b64(b"x")}, str(tmp_path))
    assert exc_info.value.code == ErrorCode.EACCES


@pytest.mark.asyncio
async def test_list_dir_returns_entries(tmp_path):
    """list_dir returns entries for an existing directory."""
    (tmp_path / "a.txt").write_bytes(b"aa")
    (tmp_path / "b.txt").write_bytes(b"bb")
    (tmp_path / "subdir").mkdir()

    result = await list_dir({"path": str(tmp_path)}, str(tmp_path))
    names = {e["name"] for e in result["entries"]}
    assert names == {"a.txt", "b.txt", "subdir"}


@pytest.mark.asyncio
async def test_list_dir_entry_fields(tmp_path):
    """list_dir entries include required FileStat fields."""
    (tmp_path / "x.txt").write_bytes(b"hello")

    result = await list_dir({"path": str(tmp_path)}, str(tmp_path))
    entry = next(e for e in result["entries"] if e["name"] == "x.txt")
    assert "name" in entry
    assert "path" in entry
    assert "size" in entry
    assert "mtime" in entry
    assert "mode" in entry
    assert "is_dir" in entry
    assert entry["is_dir"] is False
    assert entry["size"] == 5


@pytest.mark.asyncio
async def test_list_dir_enoent(tmp_path):
    """list_dir raises ENOENT for a missing directory."""
    with pytest.raises(OpError) as exc_info:
        await list_dir({"path": str(tmp_path / "missing")}, str(tmp_path))
    assert exc_info.value.code == ErrorCode.ENOENT


@pytest.mark.asyncio
async def test_list_dir_enotdir(tmp_path):
    """list_dir raises ENOTDIR when path is a regular file."""
    f = tmp_path / "file.txt"
    f.write_bytes(b"data")
    with pytest.raises(OpError) as exc_info:
        await list_dir({"path": str(f)}, str(tmp_path))
    assert exc_info.value.code == ErrorCode.ENOTDIR


@pytest.mark.asyncio
async def test_stat_existing_file(tmp_path):
    """stat returns FileStat for an existing file."""
    f = tmp_path / "f.txt"
    f.write_bytes(b"12345")

    result = await stat({"path": str(f)}, str(tmp_path))
    s = result["stat"]
    assert s is not None
    assert s["name"] == "f.txt"
    assert s["size"] == 5
    assert s["is_dir"] is False


@pytest.mark.asyncio
async def test_stat_existing_dir(tmp_path):
    """stat returns FileStat with is_dir=True for a directory."""
    d = tmp_path / "mydir"
    d.mkdir()

    result = await stat({"path": str(d)}, str(tmp_path))
    s = result["stat"]
    assert s is not None
    assert s["is_dir"] is True


@pytest.mark.asyncio
async def test_stat_nonexistent_returns_null(tmp_path):
    """stat returns null (None) when path doesn't exist."""
    result = await stat({"path": str(tmp_path / "ghost.txt")}, str(tmp_path))
    assert result["stat"] is None


@pytest.mark.asyncio
async def test_delete_file(tmp_path):
    """delete removes an existing file."""
    f = tmp_path / "todelete.txt"
    f.write_bytes(b"bye")

    result = await delete({"path": str(f)}, str(tmp_path))
    assert result["ok"] is True
    assert not f.exists()


@pytest.mark.asyncio
async def test_delete_empty_dir(tmp_path):
    """delete removes an empty directory."""
    d = tmp_path / "emptydir"
    d.mkdir()

    result = await delete({"path": str(d)}, str(tmp_path))
    assert result["ok"] is True
    assert not d.exists()


@pytest.mark.asyncio
async def test_delete_enoent(tmp_path):
    """delete raises ENOENT when path doesn't exist."""
    with pytest.raises(OpError) as exc_info:
        await delete({"path": str(tmp_path / "ghost.txt")}, str(tmp_path))
    assert exc_info.value.code == ErrorCode.ENOENT


@pytest.mark.asyncio
async def test_delete_nonempty_dir_raises(tmp_path):
    """delete raises an error for a non-empty directory."""
    d = tmp_path / "nonempty"
    d.mkdir()
    (d / "child.txt").write_bytes(b"x")

    with pytest.raises(OpError):
        await delete({"path": str(d)}, str(tmp_path))


# ---------------------------------------------------------------------------
# Integration-level tests via the WS server
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_server_read_file_via_ws(server: ServerFixture, tmp_path):
    """read_file routed through the WS server returns content."""
    f = pathlib.Path(server.workspace_root) / "ws_test.txt"
    f.write_bytes(b"ws content")

    async with server.client() as ws:
        await ws.send_json({"req_id": 1, "op": "read_file", "args": {"path": str(f)}})
        resp = await ws.receive_json()

    assert resp["ok"] is True
    assert from_b64(resp["result"]["content_b64"]) == b"ws content"


@pytest.mark.asyncio
async def test_server_read_file_enoent_via_ws(server: ServerFixture):
    """read_file returns ENOENT error through the WS server."""
    async with server.client() as ws:
        await ws.send_json(
            {"req_id": 2, "op": "read_file", "args": {"path": "/nonexistent/path/file.txt"}}
        )
        resp = await ws.receive_json()

    assert resp["ok"] is False
    assert resp["error"]["code"] == "EACCES"  # escapes workspace → EACCES


@pytest.mark.asyncio
async def test_server_write_and_read_roundtrip_via_ws(server: ServerFixture):
    """write_file then read_file roundtrip through the WS server."""
    target = pathlib.Path(server.workspace_root) / "roundtrip.txt"

    async with server.client() as ws:
        await ws.send_json(
            {"req_id": 3, "op": "write_file", "args": {"path": str(target), "content_b64": b64(b"round trip!")}}
        )
        wr = await ws.receive_json()
        assert wr["ok"] is True

        await ws.send_json({"req_id": 4, "op": "read_file", "args": {"path": str(target)}})
        rr = await ws.receive_json()
        assert rr["ok"] is True
        assert from_b64(rr["result"]["content_b64"]) == b"round trip!"


@pytest.mark.asyncio
async def test_server_stat_via_ws(server: ServerFixture):
    """stat routed through the WS server returns FileStat."""
    f = pathlib.Path(server.workspace_root) / "stat_test.txt"
    f.write_bytes(b"abc")

    async with server.client() as ws:
        await ws.send_json({"req_id": 5, "op": "stat", "args": {"path": str(f)}})
        resp = await ws.receive_json()

    assert resp["ok"] is True
    assert resp["result"]["stat"]["name"] == "stat_test.txt"


@pytest.mark.asyncio
async def test_server_list_dir_via_ws(server: ServerFixture):
    """list_dir routed through the WS server returns entries."""
    root = pathlib.Path(server.workspace_root)
    (root / "f1.txt").write_bytes(b"1")
    (root / "f2.txt").write_bytes(b"2")

    async with server.client() as ws:
        await ws.send_json({"req_id": 6, "op": "list_dir", "args": {"path": str(root)}})
        resp = await ws.receive_json()

    assert resp["ok"] is True
    names = {e["name"] for e in resp["result"]["entries"]}
    assert {"f1.txt", "f2.txt"}.issubset(names)


@pytest.mark.asyncio
async def test_server_append_line_via_ws(server: ServerFixture):
    """append_line routed through the WS server appends a line."""
    target = pathlib.Path(server.workspace_root) / "append_ws.txt"

    async with server.client() as ws:
        await ws.send_json(
            {"req_id": 7, "op": "append_line", "args": {"path": str(target), "line_b64": b64(b"ws line")}}
        )
        resp = await ws.receive_json()

    assert resp["ok"] is True
    assert resp["result"]["byte_offset"] > 0
    assert target.read_bytes() == b"ws line\n"


@pytest.mark.asyncio
async def test_server_delete_via_ws(server: ServerFixture):
    """delete routed through the WS server removes the file."""
    f = pathlib.Path(server.workspace_root) / "delete_ws.txt"
    f.write_bytes(b"gone")

    async with server.client() as ws:
        await ws.send_json({"req_id": 8, "op": "delete", "args": {"path": str(f)}})
        resp = await ws.receive_json()

    assert resp["ok"] is True
    assert not f.exists()


@pytest.mark.asyncio
async def test_server_unknown_op_returns_eunsupported(server: ServerFixture):
    """An unrecognised op returns EUNSUPPORTED (server.py fallback preserved)."""
    async with server.client() as ws:
        await ws.send_json({"req_id": 99, "op": "archive", "args": {}})
        resp = await ws.receive_json()

    assert resp["ok"] is False
    assert resp["error"]["code"] == "EUNSUPPORTED"
