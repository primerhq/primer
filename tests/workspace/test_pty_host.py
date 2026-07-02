"""Unit tests for the local-workspace PTY host (BE3 bounded output queue,
BE7 write remainder loop).

These exercise the pure helpers / methods directly — no real pseudo-terminal
is spawned — so they run on every platform.
"""

from __future__ import annotations

import asyncio

import pytest

from primer.workspace.local.pty_host import (
    _OUTPUT_QUEUE_MAX_CHUNKS,
    _enqueue_output,
)


def test_output_queue_bounded_drop_oldest() -> None:
    """The output queue never exceeds its cap under a flood; the OLDEST chunks
    are dropped and the NEWEST are retained (bounded memory, fresh tail)."""
    queue: asyncio.Queue[bytes] = asyncio.Queue(
        maxsize=_OUTPUT_QUEUE_MAX_CHUNKS
    )
    total = _OUTPUT_QUEUE_MAX_CHUNKS * 4
    dropped = 0
    for i in range(total):
        if _enqueue_output(queue, i.to_bytes(4, "big")):
            dropped += 1

    assert queue.qsize() == _OUTPUT_QUEUE_MAX_CHUNKS
    assert dropped == total - _OUTPUT_QUEUE_MAX_CHUNKS
    drained = [queue.get_nowait() for _ in range(queue.qsize())]
    expected = [
        i.to_bytes(4, "big")
        for i in range(total - _OUTPUT_QUEUE_MAX_CHUNKS, total)
    ]
    assert drained == expected


def test_output_queue_eof_sentinel_survives_full_queue() -> None:
    """A full queue must still admit the empty EOF sentinel so ``output()``
    can terminate instead of hanging."""
    queue: asyncio.Queue[bytes] = asyncio.Queue(
        maxsize=_OUTPUT_QUEUE_MAX_CHUNKS
    )
    for i in range(_OUTPUT_QUEUE_MAX_CHUNKS):
        _enqueue_output(queue, i.to_bytes(4, "big"))
    assert queue.full()
    assert _enqueue_output(queue, b"") is True
    items = [queue.get_nowait() for _ in range(queue.qsize())]
    assert items[-1] == b""


@pytest.mark.asyncio
async def test_write_loops_over_short_writes(tmp_path, monkeypatch) -> None:
    """A large stdin write is fully delivered even when os.write accepts only
    a few bytes per call (BE7: loop over the remainder)."""
    import os as _os

    from primer.workspace.local.pty_host import LocalPtySession

    _SENTINEL_FD = 0x7FED_1234
    real_write = _os.write
    delivered = bytearray()
    calls = 0

    def fake_write(fd, buf):
        nonlocal calls
        if fd != _SENTINEL_FD:
            return real_write(fd, buf)
        calls += 1
        chunk = bytes(buf[:4])
        delivered.extend(chunk)
        return len(chunk)

    monkeypatch.setattr(_os, "write", fake_write)

    session = LocalPtySession(root=tmp_path)
    session._master_fd = _SENTINEL_FD  # bypass a real pty
    payload = b"a-large-paste-exceeding-one-write-buffer" * 3
    await session.write(payload)

    assert bytes(delivered) == payload
    assert calls > 1


@pytest.mark.asyncio
async def test_write_drops_on_dead_pty(tmp_path, monkeypatch) -> None:
    """OSError from os.write (dead pty) is swallowed, not raised."""
    import os as _os

    from primer.workspace.local.pty_host import LocalPtySession

    _SENTINEL_FD = 0x7FED_5678
    real_write = _os.write

    def fake_write(fd, buf):
        if fd != _SENTINEL_FD:
            return real_write(fd, buf)
        raise OSError("pty gone")

    monkeypatch.setattr(_os, "write", fake_write)
    session = LocalPtySession(root=tmp_path)
    session._master_fd = _SENTINEL_FD
    await session.write(b"data")  # must not raise
