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
from primer_runtime.locks import WorkspaceLockTable


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
        locks=WorkspaceLockTable(),
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
        locks=WorkspaceLockTable(),
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
        locks=WorkspaceLockTable(),
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
# Tier-B exec write-locking (run_exec directly)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_exec_read_access_takes_no_lock(tmp_path):
    """access='read' runs even while the workdir scope lock is held."""
    from primer_runtime.exec import run_exec

    locks = WorkspaceLockTable()
    frames: list = []

    async def hold_scope():
        async with locks.hold_scope(str(tmp_path)):
            await asyncio.sleep(0.2)

    async def read_exec():
        async for evt in run_exec(
            1, {"cmd": ["/bin/sh", "-c", "echo hi"], "workdir": str(tmp_path),
                "access": "read"},
            str(tmp_path), locks,
        ):
            frames.append(evt)

    holder = asyncio.create_task(hold_scope())
    await asyncio.sleep(0.01)
    # Read exec must finish without waiting for the 0.2s scope hold.
    await asyncio.wait_for(read_exec(), timeout=0.1)
    holder.cancel()
    assert any(f.event == "exit" for f in frames)


@pytest.mark.asyncio
async def test_write_exec_serializes_same_workdir(tmp_path):
    from primer_runtime.exec import run_exec

    locks = WorkspaceLockTable()
    order: list[str] = []

    async def slow_holder():
        async with locks.hold_scope(str(tmp_path)):
            order.append("holder-in")
            await asyncio.sleep(0.1)
            order.append("holder-out")

    async def write_exec():
        async for _ in run_exec(
            2, {"cmd": ["/bin/sh", "-c", "true"], "workdir": str(tmp_path)},
            str(tmp_path), locks,
        ):
            pass
        order.append("exec-done")

    h = asyncio.create_task(slow_holder())
    await asyncio.sleep(0.01)
    await write_exec()
    await h
    assert order == ["holder-in", "holder-out", "exec-done"]


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


@pytest.mark.asyncio
async def test_read_not_blocked_by_parked_write(tmp_path: Path) -> None:
    """A read must be answered WHILE a write is parked behind an exec's scope lock.

    A slow write-exec holds the (default) workdir==root scope for ~0.4s; a
    ``write_file`` to the same dir then PARKS on that Tier-A scope lock, while a
    ``read_file`` (which takes no lock and stays inline in the message loop) must
    still be answered.  The read (12) must resolve BEFORE the parked write (11).
    """
    from aiohttp.test_utils import TestClient, TestServer

    from primer_runtime.server import build_app

    (tmp_path / "seed.txt").write_bytes(b"seed")
    token = secrets.token_urlsafe(16)
    app = build_app(token=token, workspace_root=str(tmp_path))

    async with TestClient(TestServer(app)) as client:
        ws = await client.ws_connect("/", headers={"Authorization": f"Bearer {token}"})
        try:
            await _handshake(ws)
            # 1) slow exec holding the (default) workdir==root scope for ~0.4s
            await ws.send_str(json.dumps({
                "op": "exec", "req_id": 10,
                "args": {"cmd": ["/bin/sh", "-c", "sleep 0.4"]},
            }))
            await asyncio.sleep(0.05)
            # 2) a write to the same dir -> parks on the scope lock
            await ws.send_str(json.dumps({
                "op": "write_file", "req_id": 11,
                "args": {"path": "out.txt",
                         "content_b64": base64.b64encode(b"x").decode()},
            }))
            # 3) a read -> must be answered while the write is still parked
            await ws.send_str(json.dumps({
                "op": "read_file", "req_id": 12, "args": {"path": "seed.txt"},
            }))
            # Collect responses; assert read (12) resolves before write (11).
            seen_order: list[int] = []
            deadline = asyncio.get_event_loop().time() + 5.0
            while {11, 12} - set(seen_order):
                remaining = deadline - asyncio.get_event_loop().time()
                assert remaining > 0, "timed out waiting for write/read responses"
                msg = await asyncio.wait_for(ws.receive(), timeout=remaining)
                frame = json.loads(msg.data)
                if "ok" in frame and frame.get("req_id") in (11, 12):
                    seen_order.append(frame["req_id"])
            assert seen_order.index(12) < seen_order.index(11)
        finally:
            await ws.close()
