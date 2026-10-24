"""The runtime's ``read_file`` reads only a regular file, and checks the file it opened, not the path it stat'ed (the G5 audit, G5-09).

The platform refuses a FIFO before it asks the runtime (``SandboxWorkspace.read_file`` stats the path first), but that is two runtime
round trips, and the runtime's own read was ``stat`` then ``open(path)``: a process in the workspace that swaps a FIFO in between pins
the reading thread in ``open()`` (a FIFO with no writer blocks there), and the platform's 5 s timeout only abandons the thread, so enough
of them exhaust the pool (ticket ce24). The runtime now opens with ``O_NONBLOCK`` and ``fstat``s the descriptor.

Every read here is bounded, and a reader the code under test left blocked in ``open()`` is released by opening the FIFO for writing, so
a regression fails these tests instead of hanging the lane. The op and its error are looked up on the module at call time and the error
is matched by name and code: ``tests/workspace/test_subprocess_timeout.py`` reloads ``primer_runtime.ops``, and CI can import the
package twice (see ``test_pty_op.py``), so a class imported here at collection need not be the one raised.
"""

from __future__ import annotations

import asyncio
import base64
import os
import socket
from pathlib import Path

import pytest

import primer_runtime.ops as ops
from primer_runtime.protocol import ErrorCode

pytestmark = pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="needs POSIX FIFOs")


def _release_a_blocked_reader(path: Path) -> None:
    """A reader blocked in open() of the FIFO is released by a writer that opens and closes it (it then reads EOF)."""
    try:
        fd = os.open(path, os.O_WRONLY | os.O_NONBLOCK)
    except OSError:
        return              # no reader is waiting (ENXIO), or the path is no FIFO
    os.close(fd)


async def _read(path: Path, root: Path) -> dict:
    try:
        return await asyncio.wait_for(ops.read_file({"path": str(path)}, str(root)), timeout=2)
    finally:
        _release_a_blocked_reader(path)


async def _refused(path: Path, root: Path) -> Exception:
    """The op error the read raised (matched by name, see the module docstring)."""
    with pytest.raises(Exception) as caught:
        await _read(path, root)
    assert type(caught.value).__name__ == "OpError", repr(caught.value)
    return caught.value


async def test_a_fifo_is_refused_without_blocking(tmp_path: Path) -> None:
    fifo = tmp_path / "agents.md"
    os.mkfifo(fifo)

    refused = await _refused(fifo, tmp_path)

    assert refused.code == ErrorCode.EUNSUPPORTED
    assert "Not a regular file" in refused.message and "agents.md" in refused.message


async def test_a_socket_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "s.sock"
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        server.bind(str(path))
        refused = await _refused(path, tmp_path)
    finally:
        server.close()
    assert refused.code == ErrorCode.EUNSUPPORTED


class _SwapsInAFifoAfterTheStat:
    """``os`` for the module under test, except that ``stat`` of ``target`` replaces it with a FIFO right after it answers: the swap a
    process in the workspace makes between the check and the read."""

    def __init__(self, target: Path) -> None:
        self._target = target

    def __getattr__(self, name: str):
        return getattr(os, name)

    def stat(self, path, *args, **kwargs):
        answer = os.stat(path, *args, **kwargs)
        if Path(path) == self._target:
            os.unlink(path)
            os.mkfifo(path)
        return answer


async def test_a_fifo_swapped_in_after_the_stat_is_refused_on_the_descriptor(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    target = tmp_path / "agents.md"
    target.write_text("# instructions\n")
    monkeypatch.setattr(ops, "os", _SwapsInAFifoAfterTheStat(target))

    refused = await _refused(target, tmp_path)

    assert refused.code == ErrorCode.EUNSUPPORTED
    assert target.is_fifo(), "the swap happened, so the refusal came from the descriptor"


async def test_a_directory_swapped_in_after_the_stat_is_still_eisdir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    target = tmp_path / "notes"
    target.write_text("x")

    class _SwapsInADirectory(_SwapsInAFifoAfterTheStat):
        def stat(self, path, *args, **kwargs):
            answer = os.stat(path, *args, **kwargs)
            if Path(path) == self._target:
                os.unlink(path)
                os.mkdir(path)
            return answer

    monkeypatch.setattr(ops, "os", _SwapsInADirectory(target))
    assert (await _refused(target, tmp_path)).code == ErrorCode.EISDIR


async def test_a_regular_file_larger_than_one_read_is_read_whole(tmp_path: Path) -> None:
    data = os.urandom(3 * (1 << 20) + 17)
    target = tmp_path / "big.bin"
    target.write_bytes(data)

    result = await _read(target, tmp_path)

    assert base64.b64decode(result["content_b64"]) == data


async def test_an_empty_regular_file_reads_as_empty(tmp_path: Path) -> None:
    target = tmp_path / "empty"
    target.write_bytes(b"")
    assert base64.b64decode((await _read(target, tmp_path))["content_b64"]) == b""
