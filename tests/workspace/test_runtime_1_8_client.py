"""RuntimeClient tests for the protocol 1.8 client-side behaviour.

Review 2026-11-23: 5.3 (exec output retained capped per stream while still
fully drained), 5.5 (a timed-out exec raises a visible TimeoutError),
5.9 (the write_file_cas round-trip), 5.11 (the walk/search round-trips),
5.12 (read_file offset+limit goes on the wire only to a 1.8 runtime, and
is sliced locally for an older one), and 5.13 (the reconnect loop is
event-driven: zero idle wakeups, reconnection unchanged).

The fake runtime below is self-contained (it reports its own protocol
version and honours offset/limit only when it is new enough), so the
shared 1.0 harness in tests/workspace/test_runtime_client.py stays
untouched.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import time

import aiohttp
import pytest
from aiohttp import web

from primer.workspace.runtime.protocol import ErrorCode
from primer.workspace.runtime.runtime_client import (
    _CappedStream,
    _HEAD_BYTES,
    _TAIL_BYTES,
    RuntimeClient,
    RuntimeError as RuntimeOpError,
)


class _FakeRT:
    """In-process runtime: hello (configurable protocol version), health,
    read_file (honours offset/limit only when protocol >= 1.5/1.8), walk,
    search, write_file_cas and exec (normal / bigout / timed_out modes)."""

    def __init__(self, protocol: str = "1.8") -> None:
        self.protocol = protocol
        self.files: dict[str, bytes] = {}
        self.exec_mode: str = "normal"  # normal | bigout | timed_out
        self.exec_stdout_chunks: int = 40  # 1 MiB each
        self.exec_stderr_chunks: int = 4
        self.received: list[dict] = []
        self.walk_result: tuple[list[dict], bool] | None = None
        self.search_result: dict | None = None
        self.close_on_next = False
        self.events_sent: int = 0
        self.read_bytes_sent: int = 0  # file bytes put on the wire by read_file answers

    def _reply(self, req_id: int, result: dict) -> str:
        return json.dumps({"req_id": req_id, "ok": True, "result": result})

    async def handler(self, request: web.Request) -> web.WebSocketResponse:
        auth = request.headers.get("Authorization", "")
        if auth != "Bearer test-token":
            raise web.HTTPUnauthorized()
        ws = web.WebSocketResponse(max_msg_size=256 * 1024 * 1024)
        await ws.prepare(request)
        async for msg in ws:
            if msg.type != aiohttp.WSMsgType.TEXT:
                continue
            frame = json.loads(msg.data)
            req_id = frame.get("req_id")
            op = frame.get("op")
            args = frame.get("args") or {}
            self.received.append({"op": op, "args": args, "req_id": req_id})
            if self.close_on_next:
                self.close_on_next = False
                await ws.close()
                continue
            if op == "hello":
                await ws.send_str(self._reply(req_id, {"protocol": self.protocol, "runtime": "test/1"}))
            elif op == "health":
                await ws.send_str(self._reply(req_id, {"ok": True}))
            elif op == "read_file":
                path = args.get("path", "")
                data = self.files.get(path, b"")
                # An old runtime IGNORES offset/limit (the platform then slices).
                if self.protocol >= "1.5" and args.get("offset"):
                    data = data[args["offset"]:]
                if self.protocol >= "1.8" and args.get("limit"):
                    data = data[: args["limit"]]
                self.read_bytes_sent += len(data)
                await ws.send_str(
                    self._reply(
                        req_id,
                        {"content_b64": base64.b64encode(data).decode()},
                    )
                )
            elif op == "stat":
                path = args.get("path", "")
                stat = (
                    {"path": path, "is_dir": False, "size": len(self.files[path]), "mtime": 0.0}
                    if path in self.files else None
                )
                await ws.send_str(self._reply(req_id, {"stat": stat}))
            elif op == "walk":
                entries, truncated = self.walk_result if self.walk_result is not None else ([], False)
                await ws.send_str(self._reply(req_id, {"entries": entries, "truncated": truncated}))
            elif op == "search":
                result = self.search_result if self.search_result is not None else {
                    "lines": [], "match_count": 0, "file_count": 0,
                    "truncated": False, "stop_reason": None,
                }
                await ws.send_str(self._reply(req_id, result))
            elif op == "write_file_cas":
                path = args.get("path", "")
                expected_sha = args.get("expected_sha256", "")
                content = base64.b64decode(args.get("content_b64", ""))
                if path not in self.files:
                    # The real runtime answers ENOENT for a missing file.
                    await ws.send_str(json.dumps({
                        "req_id": req_id, "ok": False,
                        "error": {"code": "ENOENT", "message": path},
                    }))
                else:
                    applied = hashlib.sha256(self.files[path]).hexdigest() == expected_sha
                    if applied:
                        self.files[path] = content
                    await ws.send_str(self._reply(req_id, {"applied": applied}))
            elif op == "exec":
                if self.exec_mode == "timed_out":
                    await ws.send_str(json.dumps({
                        "req_id": req_id, "event": "exit",
                        "data": {"code": -1, "timed_out": True},
                    }))
                    self.events_sent += 1
                elif self.exec_mode == "bigout":
                    stdout_chunk = base64.b64encode(b"O" * (1 << 20)).decode()
                    stderr_chunk = base64.b64encode(b"E" * (1 << 20)).decode()
                    for _ in range(self.exec_stdout_chunks):
                        await ws.send_str(json.dumps({
                            "req_id": req_id, "event": "stdout",
                            "data": {"data_b64": stdout_chunk},
                        }))
                        self.events_sent += 1
                    for _ in range(self.exec_stderr_chunks):
                        await ws.send_str(json.dumps({
                            "req_id": req_id, "event": "stderr",
                            "data": {"data_b64": stderr_chunk},
                        }))
                        self.events_sent += 1
                    await ws.send_str(json.dumps({
                        "req_id": req_id, "event": "exit", "data": {"code": 0},
                    }))
                    self.events_sent += 1
                else:
                    await ws.send_str(json.dumps({
                        "req_id": req_id, "event": "exit", "data": {"code": 0},
                    }))
                    self.events_sent += 1
            else:
                await ws.send_str(json.dumps({
                    "req_id": req_id, "ok": False,
                    "error": {"code": "EUNSUPPORTED", "message": op},
                }))
        return ws


async def _make_server(fake: _FakeRT) -> tuple[web.AppRunner, web.TCPSite]:
    app = web.Application()
    app.router.add_get("/", fake.handler)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    return runner, site


def _url(runner: web.AppRunner) -> str:
    host, port = runner.addresses[0]
    return f"ws://{host}:{port}/"


async def _client(url: str) -> RuntimeClient:
    client = RuntimeClient(url=url, token="test-token")
    await client.connect()
    return client


# ---------------------------------------------------------------------------
# 5.3: exec output is retained capped per stream, and fully drained
# ---------------------------------------------------------------------------


async def test_exec_output_is_capped_per_stream(rt18):
    """40 MiB stdout + 4 MiB stderr: the RESULT carries at most
    _HEAD_BYTES + marker + _TAIL_BYTES per stream (the local exec tool's
    budget), with the head and tail preserved and the omitted middle named
    by the marker."""
    fake, url = rt18
    fake.exec_mode = "bigout"
    client = await _client(url)
    try:
        result = await client.exec(["true"])
    finally:
        await client.aclose()

    assert result.exit_code == 0
    # Every chunk reached the client (the stream was drained to the end):
    assert fake.events_sent == fake.exec_stdout_chunks + fake.exec_stderr_chunks + 1
    for stream in (result.stdout, result.stderr):
        encoded = stream.encode("utf-8")
        assert "[... " in stream and " bytes omitted ...]" in stream
        assert len(encoded) < _HEAD_BYTES + _TAIL_BYTES + 64
    assert result.stdout.startswith("O" * _HEAD_BYTES)
    assert result.stdout.endswith("O" * _TAIL_BYTES)
    assert result.stderr.startswith("E" * _HEAD_BYTES)
    assert result.stderr.endswith("E" * _TAIL_BYTES)
    # The marker names the omitted middle per stream.
    assert f"[... {40 * 1024 * 1024 - _HEAD_BYTES - _TAIL_BYTES} bytes omitted ...]" in result.stdout
    assert f"[... {4 * 1024 * 1024 - _HEAD_BYTES - _TAIL_BYTES} bytes omitted ...]" in result.stderr


def test_capped_stream_keeps_head_and_tail_only():
    cap = _CappedStream()
    cap.add(b"A" * (100 * 1024))  # one 100 KiB chunk
    data = cap.data()
    assert data.startswith(b"A" * _HEAD_BYTES)
    assert data.endswith(b"A" * _TAIL_BYTES)
    assert cap.dropped == 100 * 1024 - _HEAD_BYTES - _TAIL_BYTES
    assert data.count(b"[... ") == 1


def test_capped_stream_small_stream_is_untouched():
    cap = _CappedStream()
    cap.add(b"hello")
    cap.add(b" world")
    assert cap.data() == b"hello world"
    assert cap.dropped == 0


# ---------------------------------------------------------------------------
# 5.5: a timed-out exec raises a visible TimeoutError
# ---------------------------------------------------------------------------


async def test_exec_timed_out_raises_timeout_error(rt18):
    fake, url = rt18
    fake.exec_mode = "timed_out"
    client = await _client(url)
    try:
        with pytest.raises(TimeoutError) as exc:
            await client.exec(["sleep", "10"])
        assert "timed out" in str(exc.value)
        assert "default timeout" in str(exc.value)
    finally:
        await client.aclose()


async def test_exec_timed_out_message_names_the_explicit_timeout(rt18):
    fake, url = rt18
    fake.exec_mode = "timed_out"
    client = await _client(url)
    try:
        with pytest.raises(TimeoutError) as exc:
            await client.exec(["sleep", "10"], timeout_s=5)
        assert "after 5s" in str(exc.value)
    finally:
        await client.aclose()


# ---------------------------------------------------------------------------
# 5.9: write_file_cas round-trip
# ---------------------------------------------------------------------------


async def test_write_file_cas_applies_on_match_and_refuses_on_mismatch(rt18):
    fake, url = rt18
    fake.files["/workspace/a"] = b"orig"
    client = await _client(url)
    try:
        assert await client.write_file_cas("/workspace/a", b"orig", b"new") is True
        assert fake.files["/workspace/a"] == b"new"
        assert await client.write_file_cas("/workspace/a", b"WRONG", b"x") is False
        assert fake.files["/workspace/a"] == b"new"
    finally:
        await client.aclose()


async def test_write_file_cas_sends_a_digest_not_the_old_content(rt18):
    fake, url = rt18
    old = b"o" * (2 << 20)  # a 2 MiB file
    fake.files["/workspace/a"] = old
    client = await _client(url)
    try:
        assert await client.write_file_cas("/workspace/a", old, b"new") is True
    finally:
        await client.aclose()
    frame = [r for r in fake.received if r["op"] == "write_file_cas"][-1]["args"]
    assert frame["expected_sha256"] == hashlib.sha256(old).hexdigest()
    assert "expected_b64" not in frame
    assert len(json.dumps(frame)) < 1024  # the old bytes never went on the wire


async def test_write_file_cas_missing_file_is_enoent(rt18):
    fake, url = rt18
    client = await _client(url)
    try:
        with pytest.raises(RuntimeOpError) as exc:
            await client.write_file_cas("/workspace/nope", b"", b"x")
        assert exc.value.code == ErrorCode.ENOENT
    finally:
        await client.aclose()


# ---------------------------------------------------------------------------
# 5.11: walk / search round-trips
# ---------------------------------------------------------------------------


async def test_walk_round_trip(rt18):
    fake, url = rt18
    fake.walk_result = (
        [{"path": "a.py", "is_dir": False, "size": 5, "mtime": 1.5}],
        True,
    )
    client = await _client(url)
    try:
        entries, truncated = await client.walk("/workspace")
    finally:
        await client.aclose()
    assert entries[0]["path"] == "a.py"
    assert truncated is True
    args = [r for r in fake.received if r["op"] == "walk"][-1]["args"]
    assert args["path"] == "/workspace"
    assert args["max_entries"] == 25000
    assert args["timeout_s"] == 30.0


async def test_search_round_trip(rt18):
    fake, url = rt18
    fake.search_result = {
        "lines": ["a.py"], "match_count": 1, "file_count": 1,
        "truncated": True, "stop_reason": "file_cap",
    }
    client = await _client(url)
    try:
        result = await client.search(
            "/workspace", "TODO", glob="*.py", context=1, skip=(".state",)
        )
    finally:
        await client.aclose()
    assert result["lines"] == ["a.py"]
    assert result["stop_reason"] == "file_cap"
    args = [r for r in fake.received if r["op"] == "search"][-1]["args"]
    assert args["path"] == "/workspace"
    assert args["pattern"] == "TODO"
    assert args["glob"] == "*.py"
    assert args["context"] == 1
    assert args["skip"] == [".state"]


async def test_search_forwards_head_limit_and_the_output_byte_cap(rt18):
    fake, url = rt18
    client = await _client(url)
    try:
        await client.search("/workspace", "x", head_limit=7, max_output_bytes=1000)
        await client.search("/workspace", "x")
    finally:
        await client.aclose()
    first, second = [r["args"] for r in fake.received if r["op"] == "search"]
    assert first["head_limit"] == 7 and first["max_output_bytes"] == 1000
    assert "head_limit" not in second and "max_output_bytes" not in second


@pytest.mark.parametrize("version", ["1.0", "1.7"])
async def test_new_ops_are_refused_locally_by_a_runtime_below_1_8(version):
    """Below 1.8 the client answers walk/search/write_file_cas EUNSUPPORTED
    itself: no request goes out for an op the runtime would not know."""
    fake = _FakeRT(protocol=version)
    runner, _site = await _make_server(fake)
    client = await _client(_url(runner))
    try:
        for call in (
            client.walk("/workspace"),
            client.search("/workspace", "x"),
            client.write_file_cas("/workspace/a", b"a", b"b"),
        ):
            with pytest.raises(RuntimeOpError) as exc:
                await call
            assert exc.value.code == ErrorCode.EUNSUPPORTED
        assert [r["op"] for r in fake.received if r["op"] in ("walk", "search", "write_file_cas")] == []
    finally:
        await client.aclose()
        await runner.cleanup()


# ---------------------------------------------------------------------------
# 5.12: read_file offset+limit on the wire (1.8) vs local slicing (older)
# ---------------------------------------------------------------------------


async def test_read_file_offset_limit_goes_on_the_wire_to_a_1_8_runtime(rt18):
    fake, url = rt18
    fake.files["/workspace/big"] = b"0123456789"
    client = await _client(url)
    try:
        data = await client.read_file("/workspace/big", offset=3, limit=4)
    finally:
        await client.aclose()
    assert data == b"3456"
    args = [r for r in fake.received if r["op"] == "read_file"][-1]["args"]
    assert args.get("offset") == 3
    assert args.get("limit") == 4


async def test_read_file_offset_limit_slices_locally_for_an_older_runtime(rt10):
    fake, url = rt10
    fake.files["/workspace/big"] = b"0123456789"
    client = await _client(url)
    try:
        data = await client.read_file("/workspace/big", offset=3, limit=4)
    finally:
        await client.aclose()
    assert data == b"3456"
    args = [r for r in fake.received if r["op"] == "read_file"][-1]["args"]
    # A 1.0 runtime would ignore the keys: the client sends neither and
    # slices the whole-file answer itself.
    assert "offset" not in args
    assert "limit" not in args


# ---------------------------------------------------------------------------
# Fix round 1: the paged sandbox read is gated on the runtime version
# ---------------------------------------------------------------------------


async def _read_through_a_runtime(protocol: str, size: int, offset: int) -> tuple[_FakeRT, str]:
    from primer.workspace.local.tools import ReadArgs
    from primer.workspace.runtime.ws_sandbox import WSSandbox
    from primer.workspace.sandbox.tools import SandboxRead
    from primer.workspace.tool import ToolCallContext
    import primer.workspace  # noqa: F401  (ToolCallContext model_rebuild)

    class _Session:
        def mark_read(self, path: str) -> None: ...

    fake = _FakeRT(protocol=protocol)
    line = b"0123456789abcdef" * 4 + b"\n"  # 65 bytes
    fake.files["/workspace/big.txt"] = line * (size // len(line))
    runner, _site = await _make_server(fake)
    client = await _client(_url(runner))
    try:
        sandbox = WSSandbox(runtime_client=client, container_id="c1")
        ctx = ToolCallContext.model_construct(
            workspace_id="w", session_id="s", agent_id="a", call_id="c",
            abort=asyncio.Event(), session=_Session(),
        )
        res = await SandboxRead(sandbox, workspace_root="/workspace").execute(
            ReadArgs(path="big.txt", offset=offset, limit=3), ctx
        )
    finally:
        await client.aclose()
        await runner.cleanup()
    return fake, res.output


async def test_read_of_a_big_file_does_not_page_through_a_pre_1_8_runtime():
    """Below 1.8 the runtime ignores the byte limit, so every 128 KiB page of
    a paged read would download the file again (about 100 MB for this 4.3 MB
    file): the tool reads it whole, once."""
    size = 4_300_000
    fake, output = await _read_through_a_runtime("1.7", size, offset=60_000)  # a window near the end
    assert fake.read_bytes_sent <= size + 4096  # one whole read, not one download per page
    assert "60001" in output  # the window is served


async def test_read_of_a_big_file_is_paged_on_a_1_8_runtime():
    size = 4_300_000
    fake, output = await _read_through_a_runtime("1.8", size, offset=100)
    assert fake.read_bytes_sent < 1_000_000  # only the pages the window needs
    assert "   101" in output


# ---------------------------------------------------------------------------
# 5.13: event-driven reconnect (no idle polling; reconnection unchanged)
# ---------------------------------------------------------------------------


async def test_connected_client_wakes_zero_times_while_idle(rt18, monkeypatch):
    """The old loop polled ``_connected`` every 0.1 s while idle (10 Hz).
    The new loop parks on ``_disconnected``: during a 0.35 s idle window a
    connected client makes NO small sleeps (the old code made ~3-4)."""
    fake, url = rt18
    import primer.workspace.runtime.runtime_client as rc_mod

    real_sleep = asyncio.sleep
    small: list[float] = []

    async def spy(delay, *a, **k):
        if delay <= 0.15:
            small.append(delay)
        return await real_sleep(delay, *a, **k)

    monkeypatch.setattr(rc_mod.asyncio, "sleep", spy)
    client = await _client(url)
    try:
        await real_sleep(0.35)
        assert small == []
    finally:
        await client.aclose()


async def test_reconnect_after_drop_still_works(rt18):
    """A real drop still wakes the loop at once: the first backoff (1.0 s)
    later the client reconnects and ops work again."""
    fake, url = rt18
    fake.files["/workspace/a.txt"] = b"v1"
    client = await _client(url)
    try:
        assert await client.read_file("/workspace/a.txt") == b"v1"

        fake.close_on_next = True
        with pytest.raises(RuntimeOpError) as exc:
            # The server closes the WS on this message; the pending read
            # fails with EPROTOCOL (connection lost).
            await asyncio.wait_for(client.read_file("/workspace/a.txt"), 5.0)
        assert exc.value.code == ErrorCode.EPROTOCOL

        fake.files["/workspace/a.txt"] = b"v2"
        deadline = time.monotonic() + 5.0
        while not client._connected.is_set() and time.monotonic() < deadline:
            await asyncio.sleep(0.1)
        assert client._connected.is_set()
        assert await client.read_file("/workspace/a.txt") == b"v2"
    finally:
        await client.aclose()


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
async def rt18() -> tuple[_FakeRT, str]:
    fake = _FakeRT(protocol="1.8")
    runner, _site = await _make_server(fake)
    try:
        yield fake, _url(runner)
    finally:
        await runner.cleanup()


@pytest.fixture
async def rt10() -> tuple[_FakeRT, str]:
    fake = _FakeRT(protocol="1.0")
    runner, _site = await _make_server(fake)
    try:
        yield fake, _url(runner)
    finally:
        await runner.cleanup()
