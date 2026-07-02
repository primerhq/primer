"""Tests for the exec op running as a tracked task (BE4).

The exec op used to stream INLINE inside the runtime's single WS message loop,
so while a long agent exec streamed, that connection could not service any
other op (``pty_stdin`` / ``pty_resize`` / file ops) — container/k8s terminal
input froze for the duration.  ``PTY_OPEN`` was already a task; ``EXEC`` now
mirrors it.

Two layers of coverage:

* ``start_exec`` driven directly (like ``test_pty_op``) — asserts the framing
  (stdout/exit events) and registry lifecycle are preserved.
* the real aiohttp server driven over a WebSocket — asserts a second op is
  serviced *while* a slow exec is still streaming (i.e. the loop isn't blocked).
"""

from __future__ import annotations

import asyncio
import base64
import json
import secrets
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(
    sys.platform not in ("linux", "darwin"),
    reason="exec ops spawn POSIX subprocesses",
)

from primer_runtime.exec import ExecRegistry, start_exec


def _events(frames: list[str]) -> list[dict]:
    return [json.loads(f) for f in frames]


def _stdout(frames: list[str]) -> bytes:
    out = b""
    for evt in _events(frames):
        if evt.get("event") == "stdout":
            out += base64.b64decode(evt["data"]["data_b64"])
    return out


# ---------------------------------------------------------------------------
# Direct start_exec coverage (no server / no Docker)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_start_exec_streams_and_deregisters(tmp_path: Path) -> None:
    registry = ExecRegistry()
    frames: list[str] = []

    async def send(frame: str) -> None:
        frames.append(frame)

    task = start_exec(
        req_id=1,
        args={"cmd": ["/bin/sh", "-c", "echo hello"]},
        workspace_root=str(tmp_path),
        send=send,
        registry=registry,
    )
    await task

    assert b"hello" in _stdout(frames)
    exit_evt = next(e for e in _events(frames) if e.get("event") == "exit")
    assert exit_evt["req_id"] == 1
    assert exit_evt["data"]["code"] == 0
    # Done-callback deregisters the finished task.
    assert registry._tasks == set()


@pytest.mark.asyncio
async def test_start_exec_bad_cmd_emits_error_response(tmp_path: Path) -> None:
    registry = ExecRegistry()
    frames: list[str] = []

    async def send(frame: str) -> None:
        frames.append(frame)

    task = start_exec(
        req_id=2,
        args={"cmd": []},  # empty cmd → OpError(EPROTOCOL)
        workspace_root=str(tmp_path),
        send=send,
        registry=registry,
    )
    await task

    err = next(e for e in _events(frames) if "ok" in e and e["ok"] is False)
    assert err["error"]["code"] == "EPROTOCOL"


@pytest.mark.asyncio
async def test_exec_cancel_all_terminates(tmp_path: Path) -> None:
    registry = ExecRegistry()
    frames: list[str] = []

    async def send(frame: str) -> None:
        frames.append(frame)

    task = start_exec(
        req_id=3,
        args={"cmd": ["/bin/sh", "-c", "sleep 30"]},
        workspace_root=str(tmp_path),
        send=send,
        registry=registry,
    )
    # Let the subprocess actually start.
    await asyncio.sleep(0.1)
    assert registry._tasks  # in-flight
    registry.cancel_all()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert registry._tasks == set()


# ---------------------------------------------------------------------------
# End-to-end: the message loop stays free while an exec streams
# ---------------------------------------------------------------------------


async def _handshake(ws) -> None:
    await ws.send_str(json.dumps({"op": "hello", "req_id": 0, "args": {"protocol": "1.1"}}))
    resp = json.loads((await ws.receive()).data)
    assert resp["ok"] is True


@pytest.mark.asyncio
async def test_exec_does_not_block_message_loop(tmp_path: Path) -> None:
    """A second op must be serviced WHILE a slow exec is still streaming.

    The exec sleeps 0.6s before it emits stdout+exit; a ``stat`` op is sent
    right after.  If the loop were blocked (the old inline behaviour) the stat
    response could only arrive after the exec's exit event.  With exec as a
    task, the stat response arrives first.
    """
    from aiohttp.test_utils import TestClient, TestServer

    from primer_runtime.server import build_app

    token = secrets.token_urlsafe(16)
    app = build_app(token=token, workspace_root=str(tmp_path))

    async with TestClient(TestServer(app)) as client:
        ws = await client.ws_connect("/", headers={"Authorization": f"Bearer {token}"})
        try:
            await _handshake(ws)

            # Slow exec: no output until 0.6s, then stdout + exit.
            await ws.send_str(json.dumps({
                "op": "exec", "req_id": 100,
                "args": {"cmd": ["/bin/sh", "-c", "sleep 0.6; echo done"]},
            }))
            # Cheap op that must not wait behind the exec.
            await ws.send_str(json.dumps({
                "op": "stat", "req_id": 101, "args": {"path": ""},
            }))

            stat_seen = False
            exec_exit_seen = False
            deadline = asyncio.get_event_loop().time() + 10.0
            while not (stat_seen and exec_exit_seen):
                remaining = deadline - asyncio.get_event_loop().time()
                assert remaining > 0, "timed out waiting for frames"
                msg = await asyncio.wait_for(ws.receive(), timeout=remaining)
                frame = json.loads(msg.data)
                if frame.get("req_id") == 101 and frame.get("ok") is True:
                    # The stat response arrived while the exec was still
                    # sleeping — the loop was NOT blocked.
                    assert not exec_exit_seen, "stat blocked behind exec exit"
                    stat_seen = True
                elif frame.get("req_id") == 100 and frame.get("event") == "exit":
                    exec_exit_seen = True

            assert stat_seen and exec_exit_seen
        finally:
            await ws.close()
