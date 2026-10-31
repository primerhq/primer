"""Runtime server: terminal I/O, file writes, teardown and liveness over the real WebSocket (Part 3 Task 119).

The server runs in-process (aiohttp ``TestServer``) and the tests speak the raw protocol, so every frame is visible.
"""

from __future__ import annotations

import asyncio
import base64
import os
import signal
import sys
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from pathlib import Path
from typing import Any

import aiohttp
import pytest
import pytest_asyncio
from aiohttp import WSMsgType
from aiohttp.test_utils import TestServer

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="PTY ops need a Linux pseudo-terminal")

_TOKEN = "t"
_BIG = 256 * 1024 * 1024


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    try:
        with open(f"/proc/{pid}/stat") as fh:
            return fh.read().rsplit(")", 1)[1].split()[0] not in ("Z", "X")
    except FileNotFoundError:
        return False


async def _gone(pid: int, within: float = 10.0) -> bool:
    deadline = time.monotonic() + within
    while time.monotonic() < deadline:
        if not _alive(pid):
            return True
        await asyncio.sleep(0.05)
    return not _alive(pid)


async def _pid(path: Path, within: float = 10.0) -> int:
    deadline = time.monotonic() + within
    while time.monotonic() < deadline:
        if path.exists() and path.read_text().strip():
            return int(path.read_text().strip())
        await asyncio.sleep(0.05)
    raise AssertionError(f"{path.name} was never written")


def _kill(pid: int | None) -> None:
    if pid:
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass


class Conn:
    """One protocol connection; frames that arrive while waiting for another are kept, not lost."""

    def __init__(self, ws: aiohttp.ClientWebSocketResponse) -> None:
        self.ws = ws
        self.frames: list[dict[str, Any]] = []
        self._next = 100

    async def send(self, op: str, args: dict[str, Any] | None = None, req_id: int | None = None) -> int:
        if req_id is None:
            self._next += 1
            req_id = self._next
        await self.ws.send_json({"req_id": req_id, "op": op, "args": args or {}})
        return req_id

    async def until(self, pred: Callable[[dict[str, Any]], bool], timeout: float = 10.0) -> dict[str, Any]:
        for frame in self.frames:
            if pred(frame):
                return frame
        async with asyncio.timeout(timeout):
            while True:
                msg = await self.ws.receive()
                if msg.type != WSMsgType.TEXT:
                    raise ConnectionError(f"connection ended: {msg.type!r} {self.ws.close_code}")
                frame = msg.json()
                self.frames.append(frame)
                if pred(frame):
                    return frame

    async def reply(self, req_id: int, timeout: float = 10.0) -> dict[str, Any]:
        return await self.until(lambda f: f.get("req_id") == req_id and "ok" in f, timeout)

    def output(self, pty_id: int) -> bytes:
        return b"".join(
            base64.b64decode(f["data"]["data_b64"])
            for f in self.frames
            if f.get("req_id") == pty_id and f.get("event") == "data"
        )

    async def output_contains(self, pty_id: int, needle: bytes, timeout: float = 10.0) -> None:
        def seen(_frame: dict[str, Any]) -> bool:
            return needle in self.output(pty_id)

        await self.until(seen, timeout)

    async def pty_open(self, args: dict[str, Any]) -> int:
        pty_id = await self.send("pty_open", args)
        frame = await self.until(lambda f: f.get("req_id") == pty_id and ("event" in f or "ok" in f))
        assert frame.get("event") == "pty_open", frame
        return pty_id

    async def stdin(self, pty_id: int, data: bytes) -> dict[str, Any]:
        rid = await self.send("pty_stdin", {"target_req_id": pty_id, "data_b64": base64.b64encode(data).decode()})
        return await self.reply(rid)

    async def health(self, timeout: float = 10.0) -> dict[str, Any]:
        return await self.reply(await self.send("health"), timeout)


Connect = Callable[[], Awaitable[Conn]]


@pytest_asyncio.fixture
async def runtime(tmp_path: Path) -> AsyncIterator[tuple[Connect, Path, Any]]:
    from primer_runtime.server import PROTOCOL_VERSION, build_app

    root = tmp_path / "ws"
    root.mkdir()
    server = TestServer(build_app(token=_TOKEN, workspace_root=str(root)))
    await server.start_server()
    http = aiohttp.ClientSession()

    async def connect() -> Conn:
        ws = await http.ws_connect(server.make_url("/"), headers={"Authorization": f"Bearer {_TOKEN}"}, max_msg_size=_BIG)
        await ws.send_json({"req_id": 0, "op": "hello", "args": {"protocol": PROTOCOL_VERSION}})
        assert (await asyncio.wait_for(ws.receive_json(), 10))["ok"] is True
        return Conn(ws)

    try:
        yield connect, root, server.make_url("/")
    finally:
        await http.close()
        await server.close()


# ---------------------------------------------------------------------------
# Characterization: pinned on the unchanged code, unchanged after the fix
# ---------------------------------------------------------------------------


async def test_terminal_echo_resize_ctrl_c_and_exit(runtime) -> None:
    connect, _root, _url = runtime
    conn = await connect()
    pty_id = await conn.pty_open({"cmd": ["/bin/sh"], "cols": 80, "rows": 24})

    assert (await conn.stdin(pty_id, b"echo hi-$((40+2))\n"))["ok"] is True
    await conn.output_contains(pty_id, b"hi-42")

    rid = await conn.send("pty_resize", {"target_req_id": pty_id, "cols": 120, "rows": 40})
    assert (await conn.reply(rid))["ok"] is True
    await conn.stdin(pty_id, b"stty size\n")
    await conn.output_contains(pty_id, b"40 120")

    await conn.stdin(pty_id, b"echo go-$((0+1)); sleep 30\n")
    await conn.output_contains(pty_id, b"go-1")
    await asyncio.sleep(0.2)
    await conn.stdin(pty_id, b"\x03")  # Ctrl-C interrupts the foreground command
    await conn.output_contains(pty_id, b"^C")  # the line discipline took it (and flushed pending input) first
    await conn.stdin(pty_id, b"echo after-$((1+1))\n")
    await conn.output_contains(pty_id, b"after-2")

    await conn.stdin(pty_id, b"exit 3\n")
    exit_frame = await conn.until(lambda f: f.get("req_id") == pty_id and f.get("event") == "exit")
    assert exit_frame["data"] == {"code": 3}
    await conn.ws.close()


async def test_a_paste_the_command_reads_arrives_whole_and_in_order(runtime, tmp_path: Path) -> None:
    connect, _root, _url = runtime
    conn = await connect()
    out_file = tmp_path / "pasted.txt"
    pty_id = await conn.pty_open({"cmd": ["sh", "-c", f"stty -echo; echo READY; cat > {out_file}; echo DONE"]})
    await conn.output_contains(pty_id, b"READY")
    lines = b"".join(b"line %05d\n" % i for i in range(20000))  # ~200 KiB, past the pty's input buffer
    assert (await conn.stdin(pty_id, lines))["ok"] is True
    assert (await conn.stdin(pty_id, b"\x04"))["ok"] is True  # Ctrl-D ends cat's input
    await conn.output_contains(pty_id, b"DONE", timeout=30)
    assert out_file.read_bytes() == lines
    await conn.ws.close()


async def test_stdin_to_an_unknown_pty_answers_enoent(runtime) -> None:
    connect, _root, _url = runtime
    conn = await connect()
    frame = await conn.stdin(999, b"x")
    assert frame["ok"] is False
    assert frame["error"]["code"] == "ENOENT"
    await conn.ws.close()


async def test_write_and_read_a_file_under_the_old_frame_limit(runtime) -> None:
    connect, root, _url = runtime
    conn = await connect()
    content = os.urandom(2 * 1024 * 1024)  # 2 MiB -> ~2.7 MiB frame
    rid = await conn.send("write_file", {"path": "two.bin", "content_b64": base64.b64encode(content).decode()})
    assert (await conn.reply(rid))["ok"] is True
    assert (root / "two.bin").read_bytes() == content
    rid = await conn.send("read_file", {"path": "two.bin"})
    frame = await conn.reply(rid)
    assert base64.b64decode(frame["result"]["content_b64"]) == content
    await conn.ws.close()


async def test_a_clean_close_tears_the_terminal_down(runtime, tmp_path: Path) -> None:
    connect, _root, _url = runtime
    conn = await connect()
    pidfile = tmp_path / "pty.pid"
    pid = None
    try:
        await conn.pty_open({"cmd": ["sh", "-c", f"echo $$ > {pidfile}; exec sleep 60"]})
        pid = await _pid(pidfile)
        assert _alive(pid)
        await conn.ws.close()
        assert await _gone(pid)
    finally:
        _kill(pid)


async def test_health_answers_beside_a_live_terminal(runtime) -> None:
    connect, _root, _url = runtime
    conn = await connect()
    await conn.pty_open({"cmd": ["/bin/sh"]})
    frame = await conn.health()
    assert frame["ok"] is True
    assert frame["result"]["ok"] is True
    other = await connect()
    assert (await other.health())["result"]["ok"] is True
    await conn.ws.close()
    await other.ws.close()


async def test_a_protocol_major_mismatch_closes_4400(runtime) -> None:
    _connect, _root, url = runtime
    async with aiohttp.ClientSession() as http:
        ws = await http.ws_connect(url, headers={"Authorization": f"Bearer {_TOKEN}"})
        await ws.send_json({"req_id": 0, "op": "hello", "args": {"protocol": "99.0"}})
        msg = await asyncio.wait_for(ws.receive(), 10)
        assert msg.type == WSMsgType.CLOSE
        assert ws.close_code == 4400


# ---------------------------------------------------------------------------
# GI-3: a paste into a terminal whose command does not read stdin never blocks the runtime
# ---------------------------------------------------------------------------


def _free_port() -> int:
    import socket

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


async def test_a_paste_into_a_busy_terminal_never_freezes_the_runtime(tmp_path: Path) -> None:
    """The server runs in a subprocess: on the blocking code its whole loop freezes, which must not freeze this test."""
    import subprocess

    from primer_runtime.server import PROTOCOL_VERSION

    root = tmp_path / "ws"
    root.mkdir()
    port = _free_port()
    env = dict(
        os.environ, PRIMER_RUNTIME_TOKEN=_TOKEN, WORKSPACE_ROOT=str(root), RUNTIME_HOST="127.0.0.1",
        RUNTIME_PORT=str(port), PYTHONPATH=str(Path(__file__).resolve().parents[2] / "runtime"),
    )
    server = subprocess.Popen(
        [sys.executable, "-m", "primer_runtime.server"], env=env,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True,
    )
    pidfile = tmp_path / "busy.pid"
    pid = None
    try:
        async with aiohttp.ClientSession() as http:
            async def connect() -> Conn:
                deadline = time.monotonic() + 30
                while True:
                    try:
                        ws = await http.ws_connect(f"http://127.0.0.1:{port}/", headers={"Authorization": f"Bearer {_TOKEN}"})
                        break
                    except aiohttp.ClientConnectorError:
                        if time.monotonic() > deadline:
                            raise
                        await asyncio.sleep(0.1)
                await ws.send_json({"req_id": 0, "op": "hello", "args": {"protocol": PROTOCOL_VERSION}})
                assert (await asyncio.wait_for(ws.receive_json(), 10))["ok"] is True
                return Conn(ws)

            term = await connect()
            other = await connect()
            busy = await term.pty_open({"cmd": ["sh", "-c", f"echo $$ > {pidfile}; exec sleep 60"]})
            pid = await _pid(pidfile)
            shell = await term.pty_open({"cmd": ["/bin/sh"]})

            paste = b"echo pasted line\n" * 4096  # 64 KiB, far past the pty's input buffer
            assert (await term.stdin(busy, paste))["ok"] is True
            assert (await other.health())["result"]["ok"] is True
            assert (await term.health())["result"]["ok"] is True

            # A sibling terminal on the same connection keeps working.
            await term.stdin(shell, b"echo sibling-$((2+3))\n")
            await term.output_contains(shell, b"sibling-5")

            # Past the 1 MiB pending cap the paste is refused with an error frame, not queued without bound.
            refused = await term.stdin(busy, b"x" * (1024 * 1024))
            assert refused["ok"] is False
            assert refused["error"]["code"] == "EPROTOCOL"
            assert "stdin" in refused["error"]["message"]

            # Killing the command frees the terminal: its exit arrives and the runtime still answers.
            os.kill(pid, signal.SIGKILL)
            await term.until(lambda f: f.get("req_id") == busy and f.get("event") == "exit")
            assert (await other.health())["result"]["ok"] is True
            await term.ws.close()
            await other.ws.close()
    finally:
        _kill(pid)
        os.killpg(server.pid, signal.SIGKILL)
        server.wait()


# ---------------------------------------------------------------------------
# GI-4: the frame limit matches the platform's, and a too-big request fails alone
# ---------------------------------------------------------------------------


async def _client(url: Any):
    from primer.workspace.runtime.runtime_client import RuntimeClient

    client = RuntimeClient(url=str(url).replace("http://", "ws://"), token=_TOKEN)
    await client.connect()
    return client


async def test_a_file_over_4_mib_is_written_and_siblings_survive(runtime) -> None:
    _connect, root, url = runtime
    client = await _client(url)
    try:
        pty = await client.open_pty(cmd=["/bin/sh"], cols=80, rows=24)
        content = os.urandom(5 * 1024 * 1024)  # ~6.7 MiB frame, past aiohttp's 4 MiB default
        await client.write_file("five.bin", content)
        assert (root / "five.bin").read_bytes() == content
        assert await client.read_file("five.bin") == content
        await pty.stdin(b"echo still-$((3+4))\n")
        out = b""
        async with asyncio.timeout(10):
            async for frame in pty.events():
                out += frame.data or b""
                if b"still-7" in out:
                    break
        await pty.close()
    finally:
        await client.aclose()


def test_the_runtime_accepts_what_the_platform_client_accepts() -> None:
    from primer.workspace.runtime import runtime_client
    from primer_runtime import server

    assert server._MAX_MSG_SIZE == runtime_client._MAX_WS_MSG_SIZE


async def test_a_request_over_the_frame_limit_fails_alone(runtime, monkeypatch) -> None:
    from primer.workspace.runtime import runtime_client
    from primer.workspace.runtime.runtime_client import RuntimeError as RtError

    _connect, root, url = runtime
    monkeypatch.setattr(runtime_client, "_MAX_WS_MSG_SIZE", 1024 * 1024)
    client = await _client(url)
    try:
        pty = await client.open_pty(cmd=["/bin/sh"], cols=80, rows=24)
        with pytest.raises(RtError):
            await client.write_file("big.bin", b"a" * (1024 * 1024))  # ~1.4 MiB frame
        assert not (root / "big.bin").exists()
        await client.write_file("small.bin", b"ok")  # the same connection still serves
        assert (root / "small.bin").read_bytes() == b"ok"
        await pty.stdin(b"echo alive-$((4+4))\n")
        out = b""
        async with asyncio.timeout(10):
            async for frame in pty.events():
                out += frame.data or b""
                if b"alive-8" in out:
                    break
        await pty.close()
    finally:
        await client.aclose()
