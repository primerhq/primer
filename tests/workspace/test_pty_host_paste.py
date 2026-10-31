"""Local workspace terminal (``LocalPtySession``): echo, resize, Ctrl-C, exit and teardown on a real pty (Part 3 Task 119)."""

from __future__ import annotations

import asyncio
import os
import signal
import sys
import time
from pathlib import Path

import pytest

from primer.workspace.local.pty_host import LocalPtySession

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="needs a Linux pseudo-terminal")


class _Reader:
    """Drains ``session.output()`` in the background so the pty never stalls on unread output."""

    def __init__(self, session: LocalPtySession) -> None:
        self.buf = bytearray()
        self.task = asyncio.ensure_future(self._run(session))

    async def _run(self, session: LocalPtySession) -> None:
        async for chunk in session.output():
            self.buf += chunk

    async def contains(self, needle: bytes, within: float = 10.0) -> None:
        deadline = time.monotonic() + within
        while time.monotonic() < deadline:
            if needle in self.buf:
                return
            await asyncio.sleep(0.02)
        raise AssertionError(f"{needle!r} never appeared; output tail: {bytes(self.buf[-300:])!r}")


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


async def test_echo_resize_ctrl_c_and_exit(tmp_path: Path) -> None:
    session = LocalPtySession(root=tmp_path, cols=80, rows=24, cmd=["/bin/sh"])
    await session.start()
    reader = _Reader(session)
    try:
        await session.write(b"echo hi-$((40+2))\n")
        await reader.contains(b"hi-42")

        await session.resize(120, 40)
        await session.write(b"stty size\n")
        await reader.contains(b"40 120")

        await session.write(b"echo go-$((0+1)); sleep 30\n")
        await reader.contains(b"go-1")
        await asyncio.sleep(0.2)
        await session.write(b"\x03")  # Ctrl-C interrupts the foreground command
        await reader.contains(b"^C")  # the line discipline took it (and flushed pending input) before the next line
        await session.write(b"echo after-$((1+1))\n")
        await reader.contains(b"after-2")

        await session.write(b"exit 3\n")
        await asyncio.wait_for(reader.task, 10)
        assert session.exit_code == 3
    finally:
        await session.close()


async def test_close_frees_the_child(tmp_path: Path) -> None:
    pidfile = tmp_path / "pty.pid"
    session = LocalPtySession(root=tmp_path, cmd=["sh", "-c", f"echo $$ > {pidfile}; exec sleep 60"])
    await session.start()
    reader = _Reader(session)
    pid = None
    try:
        pid = await _pid(pidfile)
        assert _alive(pid)
        await session.close()
        assert not _alive(pid)
        assert session.exit_code is not None
        await session.write(b"after close\n")  # a write after close is dropped, never raised
    finally:
        reader.task.cancel()
        _kill(pid)


async def test_a_paste_the_command_reads_arrives_whole_and_in_order(tmp_path: Path) -> None:
    """Past the pty's input buffer, the queued remainder is delivered in order once the command reads it."""
    out_file = tmp_path / "pasted.txt"
    session = LocalPtySession(root=tmp_path, cmd=["sh", "-c", f"stty -echo; echo READY; cat > {out_file}; echo DONE"])
    await session.start()
    reader = _Reader(session)
    try:
        await reader.contains(b"READY")
        lines = b"".join(b"line %05d\n" % i for i in range(20000))  # ~200 KiB
        await session.write(lines)
        await session.write(b"\x04")  # Ctrl-D ends cat's input
        await reader.contains(b"DONE", within=30)
        assert out_file.read_bytes() == lines
    finally:
        await session.close()
