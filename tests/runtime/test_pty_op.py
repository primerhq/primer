"""Tests for the interactive PTY op handler (pty_open/stdin/resize/close).

These drive :func:`primer_runtime.pty_op.start_pty` DIRECTLY with a fake
``send`` collector and a :class:`PtyRegistry` — no aiohttp server or Docker
container required (mirrors ``test_state_ops`` calling the op handlers
directly).

The PTY relies on ``os.openpty`` + the terminal line discipline, which is a
POSIX/Linux facility; the whole module is skipped elsewhere.
"""

from __future__ import annotations

import asyncio
import base64
import json
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(
    sys.platform != "linux" or not hasattr(__import__("os"), "openpty"),
    reason="PTY ops require a POSIX/Linux pseudo-terminal",
)

from primer_runtime.pty_op import PtyRegistry, start_pty


async def _wait_for(pred, timeout: float = 5.0) -> None:
    """Poll *pred* until it returns truthy or *timeout* elapses."""
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        if pred():
            return
        await asyncio.sleep(0.02)
    raise AssertionError("condition not met within timeout")


def _events(frames: list[str]) -> list[dict]:
    return [json.loads(f) for f in frames]


def _collected_output(frames: list[str]) -> bytes:
    out = b""
    for evt in _events(frames):
        if evt.get("event") == "data":
            out += base64.b64decode(evt["data"]["data_b64"])
    return out


@pytest.mark.asyncio
async def test_pty_open_stdin_resize_close(tmp_path: Path) -> None:
    registry = PtyRegistry()
    frames: list[str] = []

    async def send(frame: str) -> None:
        frames.append(frame)

    # Spawn a plain shell attached to the pty; feed it a command via stdin.
    start_pty(
        req_id=1,
        args={"cmd": ["/bin/sh"], "cols": 80, "rows": 24},
        workspace_root=str(tmp_path),
        send=send,
        registry=registry,
    )

    # pty_open announces readiness.
    await _wait_for(lambda: any(e.get("event") == "pty_open" for e in _events(frames)))

    # Write a command; the shell runs it and the pty echoes output.
    assert registry.write_stdin(1, b"echo hi\n") is True
    await _wait_for(lambda: b"hi" in _collected_output(frames))

    # Resize succeeds against the live session.
    assert registry.resize(1, 120, 40) is True

    # Close terminates the child, which drives the exit event.
    assert registry.close(1) is True
    await _wait_for(lambda: any(e.get("event") == "exit" for e in _events(frames)))

    exit_evt = next(e for e in _events(frames) if e.get("event") == "exit")
    assert "code" in exit_evt["data"]
    # Session self-deregisters on teardown.
    await _wait_for(lambda: registry.get(1) is None)


@pytest.mark.asyncio
async def test_pty_control_ops_unknown_target(tmp_path: Path) -> None:
    registry = PtyRegistry()
    # No session registered → control lookups report "not found".
    assert registry.write_stdin(999, b"x") is False
    assert registry.resize(999, 80, 24) is False
    assert registry.close(999) is False


@pytest.mark.asyncio
async def test_pty_resize_out_of_range_is_clamped(tmp_path: Path) -> None:
    """Out-of-uint16 cols/rows must be clamped, never raise struct.error.

    Regression: struct.pack("HHHH") raises struct.error (NOT OSError) for
    values <0 or >65535; unclamped it escaped the resize guard and could
    kill the runtime connection's message loop.
    """
    registry = PtyRegistry()
    frames: list[str] = []

    async def send(frame: str) -> None:
        frames.append(frame)

    start_pty(
        req_id=5,
        args={"cmd": ["/bin/sh"], "cols": 80, "rows": 24},
        workspace_root=str(tmp_path),
        send=send,
        registry=registry,
    )
    await _wait_for(lambda: any(e.get("event") == "pty_open" for e in _events(frames)))

    # Absurd values are clamped in _set_winsize — no exception, session alive.
    assert registry.resize(5, 10**6, -5) is True
    assert registry.resize(5, 0, 70000) is True

    # The session still works after the clamped resizes.
    assert registry.write_stdin(5, b"echo still_alive\n") is True
    await _wait_for(lambda: b"still_alive" in _collected_output(frames))

    assert registry.close(5) is True
    await _wait_for(lambda: registry.get(5) is None)


@pytest.mark.asyncio
async def test_pty_bad_workdir_emits_error(tmp_path: Path) -> None:
    registry = PtyRegistry()
    frames: list[str] = []

    async def send(frame: str) -> None:
        frames.append(frame)

    # A workdir escaping the workspace root is rejected before spawn.
    # Await the session task directly (start_pty returns it) — deterministic,
    # no scheduling-sensitive polling.
    task = start_pty(
        req_id=7,
        args={"cmd": ["/bin/sh"], "workdir": "../../etc"},
        workspace_root=str(tmp_path),
        send=send,
        registry=registry,
    )
    await task
    err = next(e for e in _events(frames) if "ok" in e and e["ok"] is False)
    assert err["error"]["code"] == "EACCES"


@pytest.mark.asyncio
async def test_pty_bad_workdir_reports_real_code_on_split_module_identity(
    tmp_path: Path, monkeypatch
) -> None:
    """A split-identity OpError must still report EACCES, not EINTERNAL.

    On CI primer_runtime is importable via BOTH the installed dist and the
    ``runtime`` pythonpath entry, so the OpError raised in
    ``primer_runtime.ops`` can be a DIFFERENT class object than the OpError
    imported in ``pty_op`` — ``except OpError`` misses it and the catch-all
    runs. Simulate that with a foreign class whose ``__name__`` is "OpError"
    but which is NOT ``pty_op.OpError``; the emitted frame must carry the
    real code (duck-typed), not the masked EINTERNAL.
    """
    import primer_runtime.pty_op as pty_op_mod

    class _ForeignOpError(Exception):
        def __init__(self, code: str, message: str) -> None:
            super().__init__(message)
            self.code = code
            self.message = message

    _ForeignOpError.__name__ = "OpError"  # mimic the split-identity class name
    _ForeignOpError.__qualname__ = "OpError"
    assert _ForeignOpError is not pty_op_mod.OpError  # genuinely foreign

    def _raise_foreign(raw_path: str, workspace_root: str):
        raise _ForeignOpError("EACCES", f"Path escapes workspace root: {raw_path!r}")

    monkeypatch.setattr(pty_op_mod, "_resolve_safe", _raise_foreign)

    registry = PtyRegistry()
    frames: list[str] = []

    async def send(frame: str) -> None:
        frames.append(frame)

    task = start_pty(
        req_id=17,
        args={"cmd": ["/bin/sh"], "workdir": "../../etc"},
        workspace_root=str(tmp_path),
        send=send,
        registry=registry,
    )
    await task
    err = next(e for e in _events(frames) if "ok" in e and e["ok"] is False)
    assert err["error"]["code"] == "EACCES"  # NOT "EINTERNAL"


@pytest.mark.asyncio
async def test_output_queue_bounded_drop_oldest() -> None:
    """The output queue never exceeds its cap under a flood; the OLDEST chunks
    are dropped and the NEWEST are retained (bounded memory, fresh tail)."""
    from primer_runtime.pty_op import (
        _OUTPUT_QUEUE_MAX_CHUNKS,
        _enqueue_output,
    )

    queue: asyncio.Queue[bytes] = asyncio.Queue(
        maxsize=_OUTPUT_QUEUE_MAX_CHUNKS
    )
    total = _OUTPUT_QUEUE_MAX_CHUNKS * 4  # flood: 4x the cap
    dropped = 0
    for i in range(total):
        if _enqueue_output(queue, i.to_bytes(4, "big")):
            dropped += 1

    # Never exceeded the cap...
    assert queue.qsize() == _OUTPUT_QUEUE_MAX_CHUNKS
    # ...and the overflow was reported as drops.
    assert dropped == total - _OUTPUT_QUEUE_MAX_CHUNKS
    # The retained chunks are the NEWEST suffix, in order.
    drained = [queue.get_nowait() for _ in range(queue.qsize())]
    expected = [
        i.to_bytes(4, "big")
        for i in range(total - _OUTPUT_QUEUE_MAX_CHUNKS, total)
    ]
    assert drained == expected


@pytest.mark.asyncio
async def test_output_queue_eof_sentinel_survives_full_queue() -> None:
    """A full queue must still admit the empty EOF sentinel (else the drain
    loop would hang), evicting one oldest chunk to make room."""
    from primer_runtime.pty_op import (
        _OUTPUT_QUEUE_MAX_CHUNKS,
        _enqueue_output,
    )

    queue: asyncio.Queue[bytes] = asyncio.Queue(
        maxsize=_OUTPUT_QUEUE_MAX_CHUNKS
    )
    for i in range(_OUTPUT_QUEUE_MAX_CHUNKS):
        _enqueue_output(queue, i.to_bytes(4, "big"))
    assert queue.full()
    assert _enqueue_output(queue, b"") is True  # dropped one to fit the EOF
    items = [queue.get_nowait() for _ in range(queue.qsize())]
    assert items[-1] == b""  # EOF landed at the tail


@pytest.mark.asyncio
async def test_pty_cancel_all_terminates(tmp_path: Path) -> None:
    registry = PtyRegistry()
    frames: list[str] = []

    async def send(frame: str) -> None:
        frames.append(frame)

    start_pty(
        req_id=3,
        args={"cmd": ["/bin/sh"]},
        workspace_root=str(tmp_path),
        send=send,
        registry=registry,
    )
    await _wait_for(lambda: any(e.get("event") == "pty_open" for e in _events(frames)))
    # cancel_all() (WS-close path) terminates the session task.
    registry.cancel_all()
    await _wait_for(lambda: registry.get(3) is None)
