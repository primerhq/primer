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
