"""Unit tests for the local-workspace PTY host (BE3 bounded output queue).

These exercise the pure helpers directly — no real pseudo-terminal is
spawned — so they run on every platform.
"""

from __future__ import annotations

import asyncio

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
