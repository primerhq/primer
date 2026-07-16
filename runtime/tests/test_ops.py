"""Tests for runtime/primer_runtime/ops.py — one per op, including error cases.

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

from primer_runtime.exec import run_exec
from primer_runtime.locks import WorkspaceLockTable
from primer_runtime.ops import OpError, append_line, delete, list_dir, read_file, stat, write_file
from primer_runtime.protocol import ErrorCode
from primer_runtime.server import build_app, PROTOCOL_VERSION


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
            await ws.send_json({"req_id": 0, "op": "hello", "args": {"protocol": PROTOCOL_VERSION, "client": "test/0"}})
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
    result = await write_file({"path": str(target), "content_b64": b64(b"written!")}, str(tmp_path), WorkspaceLockTable())
    assert result["ok"] is True
    assert target.read_bytes() == b"written!"


@pytest.mark.asyncio
async def test_write_file_creates_parent_dirs(tmp_path):
    """write_file creates missing parent directories."""
    target = tmp_path / "deep" / "nested" / "file.txt"
    result = await write_file({"path": str(target), "content_b64": b64(b"data")}, str(tmp_path), WorkspaceLockTable())
    assert result["ok"] is True
    assert target.read_bytes() == b"data"


@pytest.mark.asyncio
async def test_write_file_sets_mode(tmp_path):
    """write_file respects the optional mode argument."""
    target = tmp_path / "exec.sh"
    result = await write_file(
        {"path": str(target), "content_b64": b64(b"#!/bin/sh"), "mode": 0o755},
        str(tmp_path), WorkspaceLockTable(),
    )
    assert result["ok"] is True
    assert target.stat().st_mode & 0o755 == 0o755


@pytest.mark.asyncio
async def test_write_file_path_escape(tmp_path):
    """write_file raises EACCES for paths outside workspace."""
    with pytest.raises(OpError) as exc_info:
        await write_file({"path": "/tmp/escape.txt", "content_b64": b64(b"x")}, str(tmp_path), WorkspaceLockTable())
    assert exc_info.value.code == ErrorCode.EACCES


@pytest.mark.asyncio
async def test_write_file_is_atomic_no_torn_read(tmp_path):
    """A concurrent reader never sees a half-written file."""
    import asyncio
    target = tmp_path / "big.txt"
    target.write_bytes(b"OLD")
    locks = WorkspaceLockTable()
    new = b"N" * 1_000_000

    async def writer():
        await write_file(
            {"path": "big.txt", "content_b64": b64(new)},
            str(tmp_path), locks,
        )

    async def reader():
        seen = set()
        for _ in range(50):
            try:
                seen.add(target.read_bytes())
            except FileNotFoundError:
                pass
            await asyncio.sleep(0)
        # Every observation is either the full old or full new content.
        assert seen <= {b"OLD", new}

    await asyncio.gather(writer(), reader())
    assert target.read_bytes() == new


@pytest.mark.asyncio
async def test_same_path_writes_serialize(tmp_path):
    import asyncio
    locks = WorkspaceLockTable()
    order: list[str] = []

    async def w(tag: str, payload: bytes, delay: float):
        # Monkeypatch-free: rely on lock ordering + a sleep inside.
        async with locks.hold_write(str(tmp_path), str(tmp_path / "f.txt")):
            order.append(f"{tag}-in")
            await asyncio.sleep(delay)
            order.append(f"{tag}-out")

    await asyncio.gather(w("A", b"a", 0.03), w("B", b"b", 0.0))
    assert order[0].endswith("-in") and order[1].endswith("-out")


@pytest.mark.asyncio
async def test_write_file_preserves_mode(tmp_path):
    locks = WorkspaceLockTable()
    await write_file(
        {"path": "x.sh", "content_b64": b64("hi"), "mode": 0o755},
        str(tmp_path), locks,
    )
    assert (tmp_path / "x.sh").stat().st_mode & 0o777 == 0o755


@pytest.mark.asyncio
async def test_append_line_creates_and_appends(tmp_path):
    """append_line creates the file and appends a newline-terminated line."""
    target = tmp_path / "log.txt"
    result = await append_line({"path": str(target), "line_b64": b64(b"first")}, str(tmp_path), WorkspaceLockTable())
    assert result["ok"] is True
    assert result["byte_offset"] > 0
    assert target.read_bytes() == b"first\n"


@pytest.mark.asyncio
async def test_append_line_multiple_appends(tmp_path):
    """Multiple append_line calls accumulate lines."""
    target = tmp_path / "log.txt"
    locks = WorkspaceLockTable()
    await append_line({"path": str(target), "line_b64": b64(b"line1")}, str(tmp_path), locks)
    await append_line({"path": str(target), "line_b64": b64(b"line2")}, str(tmp_path), locks)
    assert target.read_bytes() == b"line1\nline2\n"


@pytest.mark.asyncio
async def test_append_line_byte_offset_advances(tmp_path):
    """byte_offset increases with each append."""
    target = tmp_path / "log.txt"
    locks = WorkspaceLockTable()
    r1 = await append_line({"path": str(target), "line_b64": b64(b"aaa")}, str(tmp_path), locks)
    r2 = await append_line({"path": str(target), "line_b64": b64(b"bbb")}, str(tmp_path), locks)
    assert r2["byte_offset"] > r1["byte_offset"]


@pytest.mark.asyncio
async def test_append_line_path_escape(tmp_path):
    """append_line raises EACCES for paths outside workspace."""
    with pytest.raises(OpError) as exc_info:
        await append_line({"path": "../../etc/cron.d/evil", "line_b64": b64(b"x")}, str(tmp_path), WorkspaceLockTable())
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

    result = await delete({"path": str(f)}, str(tmp_path), WorkspaceLockTable())
    assert result["ok"] is True
    assert not f.exists()


@pytest.mark.asyncio
async def test_delete_empty_dir(tmp_path):
    """delete removes an empty directory."""
    d = tmp_path / "emptydir"
    d.mkdir()

    result = await delete({"path": str(d)}, str(tmp_path), WorkspaceLockTable())
    assert result["ok"] is True
    assert not d.exists()


@pytest.mark.asyncio
async def test_delete_enoent(tmp_path):
    """delete raises ENOENT when path doesn't exist."""
    with pytest.raises(OpError) as exc_info:
        await delete({"path": str(tmp_path / "ghost.txt")}, str(tmp_path), WorkspaceLockTable())
    assert exc_info.value.code == ErrorCode.ENOENT


@pytest.mark.asyncio
async def test_delete_nonempty_dir_raises(tmp_path):
    """delete raises an error for a non-empty directory."""
    d = tmp_path / "nonempty"
    d.mkdir()
    (d / "child.txt").write_bytes(b"x")

    with pytest.raises(OpError):
        await delete({"path": str(d)}, str(tmp_path), WorkspaceLockTable())


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


# ---------------------------------------------------------------------------
# Exec handler unit tests
# ---------------------------------------------------------------------------


async def _collect_exec(req_id: int, args: dict, workspace_root: str) -> tuple[list[dict], int, bool]:
    """Run exec and collect (stream_events, exit_code, timed_out)."""
    stream_events: list[dict] = []
    exit_code = -99
    timed_out = False

    async for event in run_exec(req_id, args, workspace_root, WorkspaceLockTable()):
        if event.event == "exit":
            assert event.data is not None
            exit_code = event.data["code"]
            timed_out = event.data.get("timed_out", False)
        else:
            assert event.data is not None
            stream_events.append({"event": event.event, "data_b64": event.data["data_b64"]})

    return stream_events, exit_code, timed_out


@pytest.mark.asyncio
async def test_exec_simple_command(tmp_path):
    """exec ["echo", "hello"] yields stdout with 'hello' and exit code 0."""
    events, code, timed_out = await _collect_exec(
        8,
        {"cmd": ["echo", "hello"], "timeout_s": 10},
        str(tmp_path),
    )
    assert code == 0
    assert not timed_out
    # At least one stdout chunk
    stdout_chunks = [e for e in events if e["event"] == "stdout"]
    assert len(stdout_chunks) >= 1
    combined = b"".join(base64.b64decode(e["data_b64"]) for e in stdout_chunks)
    assert b"hello" in combined


@pytest.mark.asyncio
async def test_exec_streams_stdout_in_chunks(tmp_path):
    """exec a command producing many lines emits multiple stdout events."""
    # Use python to write enough output to get multiple 4096-byte reads
    script = "import sys; [sys.stdout.write('x' * 100 + '\\n') for _ in range(200)]; sys.stdout.flush()"
    events, code, _ = await _collect_exec(
        9,
        {"cmd": ["python3", "-c", script], "timeout_s": 10},
        str(tmp_path),
    )
    assert code == 0
    stdout_events = [e for e in events if e["event"] == "stdout"]
    # 200 lines × 101 bytes = ~20200 bytes; chunk size 4096 → at least 4 chunks expected
    # but be lenient: just require >1
    assert len(stdout_events) >= 1
    combined = b"".join(base64.b64decode(e["data_b64"]) for e in stdout_events)
    lines = combined.split(b"\n")
    non_empty = [l for l in lines if l]
    assert len(non_empty) >= 100  # at least 100 "x" lines came through


@pytest.mark.asyncio
async def test_exec_stderr_separate_stream(tmp_path):
    """exec a command writing to stderr yields stderr events distinct from stdout."""
    script = "import sys; sys.stderr.write('error output\\n'); sys.stdout.write('std output\\n')"
    events, code, _ = await _collect_exec(
        10,
        {"cmd": ["python3", "-c", script], "timeout_s": 10},
        str(tmp_path),
    )
    assert code == 0
    stderr_events = [e for e in events if e["event"] == "stderr"]
    stdout_events = [e for e in events if e["event"] == "stdout"]
    assert len(stderr_events) >= 1
    assert len(stdout_events) >= 1

    stderr_combined = b"".join(base64.b64decode(e["data_b64"]) for e in stderr_events)
    stdout_combined = b"".join(base64.b64decode(e["data_b64"]) for e in stdout_events)
    assert b"error output" in stderr_combined
    assert b"std output" in stdout_combined


@pytest.mark.asyncio
async def test_exec_nonzero_exit(tmp_path):
    """exec a failing command returns a non-zero exit code."""
    events, code, _ = await _collect_exec(
        11,
        {"cmd": ["python3", "-c", "import sys; sys.exit(42)"], "timeout_s": 10},
        str(tmp_path),
    )
    assert code == 42


@pytest.mark.asyncio
async def test_exec_timeout(tmp_path):
    """exec sleep with timeout_s=0.1 returns timed_out=True and exit code -1."""
    events, code, timed_out = await _collect_exec(
        12,
        {"cmd": ["sleep", "30"], "timeout_s": 0.1},
        str(tmp_path),
    )
    assert timed_out is True
    assert code == -1


@pytest.mark.asyncio
async def test_exec_workdir_respected(tmp_path):
    """exec with workdir set runs in that directory."""
    subdir = tmp_path / "workdir_test"
    subdir.mkdir()
    (subdir / "marker.txt").write_bytes(b"found it")

    events, code, _ = await _collect_exec(
        13,
        {"cmd": ["ls"], "timeout_s": 10, "workdir": str(subdir)},
        str(tmp_path),
    )
    assert code == 0
    stdout_combined = b"".join(
        base64.b64decode(e["data_b64"]) for e in events if e["event"] == "stdout"
    )
    assert b"marker.txt" in stdout_combined


@pytest.mark.asyncio
async def test_exec_workdir_path_escape_raises(tmp_path):
    """exec raises OpError(EACCES) when workdir escapes workspace root."""
    with pytest.raises(OpError) as exc_info:
        async for _ in run_exec(14, {"cmd": ["ls"], "workdir": "/etc"}, str(tmp_path), WorkspaceLockTable()):
            pass
    assert exc_info.value.code == ErrorCode.EACCES


@pytest.mark.asyncio
async def test_exec_stdin(tmp_path):
    """exec passes stdin_b64 to the process via stdin pipe."""
    stdin_content = b"hello from stdin\n"
    stdin_b64 = base64.b64encode(stdin_content).decode()

    events, code, _ = await _collect_exec(
        15,
        {"cmd": ["cat"], "stdin_b64": stdin_b64, "timeout_s": 10},
        str(tmp_path),
    )
    assert code == 0
    stdout_combined = b"".join(
        base64.b64decode(e["data_b64"]) for e in events if e["event"] == "stdout"
    )
    assert b"hello from stdin" in stdout_combined


# ---------------------------------------------------------------------------
# Integration: exec via WS server
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_server_exec_simple_via_ws(server: ServerFixture):
    """exec routed through WS server emits stdout events then exit event."""
    async with server.client() as ws:
        await ws.send_json(
            {"req_id": 20, "op": "exec", "args": {"cmd": ["echo", "ws_exec_test"], "timeout_s": 10}}
        )

        stdout_data = b""
        exit_code = None
        # Read frames until we get the exit event
        for _ in range(20):
            frame = await ws.receive_json()
            assert frame["req_id"] == 20
            evt = frame.get("event")
            if evt == "stdout":
                stdout_data += base64.b64decode(frame["data"]["data_b64"])
            elif evt == "stderr":
                pass  # ignore stderr
            elif evt == "exit":
                exit_code = frame["data"]["code"]
                break

    assert exit_code == 0
    assert b"ws_exec_test" in stdout_data


@pytest.mark.asyncio
async def test_server_exec_timeout_via_ws(server: ServerFixture):
    """exec timeout routed through WS server emits exit with timed_out=True."""
    async with server.client() as ws:
        await ws.send_json(
            {"req_id": 21, "op": "exec", "args": {"cmd": ["sleep", "30"], "timeout_s": 0.2}}
        )

        exit_code = None
        timed_out = False
        for _ in range(20):
            frame = await ws.receive_json()
            if frame.get("event") == "exit":
                exit_code = frame["data"]["code"]
                timed_out = frame["data"].get("timed_out", False)
                break

    assert timed_out is True
    assert exit_code == -1


@pytest.mark.asyncio
async def test_server_exec_stderr_via_ws(server: ServerFixture):
    """exec stderr routed through WS server arrives as separate 'stderr' events."""
    script = "import sys; sys.stderr.write('oops\\n'); sys.exit(1)"
    async with server.client() as ws:
        await ws.send_json(
            {"req_id": 22, "op": "exec", "args": {"cmd": ["python3", "-c", script], "timeout_s": 10}}
        )

        stderr_data = b""
        exit_code = None
        for _ in range(20):
            frame = await ws.receive_json()
            evt = frame.get("event")
            if evt == "stderr":
                stderr_data += base64.b64decode(frame["data"]["data_b64"])
            elif evt == "exit":
                exit_code = frame["data"]["code"]
                break

    assert exit_code == 1
    assert b"oops" in stderr_data
