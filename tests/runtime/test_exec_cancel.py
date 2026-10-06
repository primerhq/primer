"""The runtime's ``exec_cancel`` op (protocol 1.4): cancel one in-flight exec by its req_id.

Until now a primer-side Stop or Cancel of an exec on a docker/k8s workspace never reached the runtime: the client only
dropped its stream, and the command ran on until its own timeout or until the connection closed. ``exec_cancel`` carries
``target_req_id`` (as ``watch_cancel`` and ``pty_close`` do) and cancels that exec's task, which stops the command's whole
process group and releases the Tier-B write lock; an exec still QUEUED on the lock is cancelled before it ever starts.

Three levels: the registry, ``start_exec`` with a registry, and the real server over a WebSocket.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import signal
import subprocess
import sys
import time
from collections.abc import AsyncIterator
from pathlib import Path

import aiohttp
import pytest
import pytest_asyncio
from aiohttp.test_utils import TestServer

from primer_runtime.exec import ExecRegistry, start_exec
from primer_runtime.locks import WorkspaceLockTable

pytestmark = pytest.mark.skipif(
    sys.platform not in ("linux", "darwin") or shutil.which("setsid") is None,
    reason="process groups and ps are needed",
)


def _running(pid: int) -> bool:
    state = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True).stdout.strip()
    return bool(state) and not state.startswith("Z")


async def _gone(pid: int, within: float = 5.0) -> bool:
    deadline = time.monotonic() + within
    while time.monotonic() < deadline:
        if not _running(pid):
            return True
        await asyncio.sleep(0.05)
    return not _running(pid)


async def _pid(path: Path, within: float = 8.0) -> int:
    deadline = time.monotonic() + within
    while time.monotonic() < deadline:
        if path.exists() and path.read_text().strip():
            return int(path.read_text().strip())
        await asyncio.sleep(0.05)
    raise AssertionError(f"{path.name} was never written")


def _kill(*pids: int | None) -> None:
    for pid in pids:
        if pid:
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass


def _args(tmp_path: Path, script: str) -> dict:
    return {"cmd": ["/bin/sh", "-c", script], "workdir": str(tmp_path), "timeout_s": 60.0}


def _start(tmp_path: Path, script: str, req_id: int, locks: WorkspaceLockTable, registry: ExecRegistry) -> asyncio.Task:
    frames: list[str] = []

    async def send(frame: str) -> None:
        frames.append(frame)

    return start_exec(req_id, _args(tmp_path, script), str(tmp_path), locks, send, registry)


# --- the registry ---------------------------------------------------------------------------------------------------------


def test_cancelling_a_req_id_nothing_runs_under_says_so() -> None:
    assert ExecRegistry().cancel(99) is False


async def test_a_finished_exec_leaves_the_registry_so_a_late_cancel_finds_nothing(tmp_path: Path) -> None:
    registry = ExecRegistry()
    task = _start(tmp_path, "true", 7, WorkspaceLockTable(), registry)

    await asyncio.wait_for(task, timeout=10.0)
    await asyncio.sleep(0)                                   # the done-callback runs

    assert registry.cancel(7) is False


# --- start_exec + registry ------------------------------------------------------------------------------------------------


async def test_cancelling_a_running_exec_stops_its_group_and_frees_the_write_lock(tmp_path: Path) -> None:
    locks, registry = WorkspaceLockTable(), ExecRegistry()
    child = None
    task = _start(tmp_path, f"sleep 60 & echo $! > {tmp_path}/child; wait", 7, locks, registry)
    try:
        child = await _pid(tmp_path / "child")
        gone_soon_after_the_writer_got_in: list[bool] = []

        async def writer() -> None:
            async with locks.hold_scope(str(tmp_path.resolve())):
                gone_soon_after_the_writer_got_in.append(await _gone(child, within=0.5))

        queued = asyncio.create_task(writer())
        await asyncio.sleep(0.05)
        assert not queued.done(), "the writer got the lock while the command ran: the test is not in its situation"

        assert registry.cancel(7) is True
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=10.0)

        assert await _gone(child), "exec_cancel left the command running"
        await asyncio.wait_for(queued, timeout=10.0)
        assert gone_soon_after_the_writer_got_in == [True]
    finally:
        _kill(child)


async def test_cancelling_an_exec_that_is_still_queued_on_the_lock_means_it_never_starts(tmp_path: Path) -> None:
    """Two writes to the same directory serialise: the second waits for the first's lock. Cancelling the waiting one must end
    it without its command ever running, and must not disturb the first."""
    locks, registry = WorkspaceLockTable(), ExecRegistry()
    first = second = None
    holder = _start(tmp_path, f"sleep 60 & echo $! > {tmp_path}/first; wait", 7, locks, registry)
    try:
        first = await _pid(tmp_path / "first")
        waiting = _start(tmp_path, f"echo started > {tmp_path}/marker; sleep 60", 8, locks, registry)
        await asyncio.sleep(0.3)
        assert not waiting.done() and not (tmp_path / "marker").exists(), "the second exec did not queue on the lock"

        assert registry.cancel(8) is True
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(waiting, timeout=10.0)

        await asyncio.sleep(0.3)
        assert not (tmp_path / "marker").exists(), "a cancelled exec that was queued on the lock still started its command"
        assert _running(first), "cancelling the queued exec disturbed the one that holds the lock"
        assert not holder.done()
    finally:
        registry.cancel(7)
        await asyncio.gather(holder, return_exceptions=True)
        _kill(first, second)


async def test_a_repeated_exec_cancel_does_not_cut_the_commands_grace_short(tmp_path: Path, monkeypatch) -> None:
    """A second cancel of the exec TASK is what skips the SIGTERM grace (the stop's ``finally`` goes straight to SIGKILL), and a
    client that sends ``exec_cancel`` twice (a retry) must not be able to do that: the command still gets its time to clean up."""
    import primer_runtime.process_group as pg

    monkeypatch.setattr(pg, "TERM_GRACE_S", 3.0)
    locks, registry = WorkspaceLockTable(), ExecRegistry()
    child = None
    # Ignores SIGTERM, and so does the ``sleep`` it starts (an ignored disposition is inherited): only SIGKILL stops them.
    task = _start(tmp_path, f"trap '' TERM; sleep 60 & echo $! > {tmp_path}/child; wait", 7, locks, registry)
    try:
        child = await _pid(tmp_path / "child")

        assert registry.cancel(7) is True
        await asyncio.sleep(0.3)                             # the stop is now waiting out the grace
        assert registry.cancel(7) is True                    # the exec is still in flight (stopping), so this is not ENOENT
        await asyncio.sleep(0.8)
        assert _running(child), "a repeated exec_cancel skipped the grace the command is owed"

        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=10.0)
        assert await _gone(child), "the command was not stopped after the grace"
    finally:
        _kill(child)
        registry.cancel_all()
        await asyncio.gather(task, return_exceptions=True)


async def test_cancelling_one_exec_leaves_the_others_running(tmp_path: Path) -> None:
    locks, registry = WorkspaceLockTable(), ExecRegistry()
    a_dir, b_dir = tmp_path / "a", tmp_path / "b"
    a_dir.mkdir()
    b_dir.mkdir()
    a_pid = b_pid = None
    a = _start(a_dir, f"sleep 60 & echo $! > {a_dir}/child; wait", 1, locks, registry)
    b = _start(b_dir, f"sleep 60 & echo $! > {b_dir}/child; wait", 2, locks, registry)
    try:
        a_pid, b_pid = await _pid(a_dir / "child"), await _pid(b_dir / "child")

        assert registry.cancel(1) is True
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(a, timeout=10.0)

        assert await _gone(a_pid)
        assert _running(b_pid) and not b.done(), "cancelling exec 1 stopped exec 2"
    finally:
        registry.cancel_all()
        await asyncio.gather(a, b, return_exceptions=True)
        _kill(a_pid, b_pid)


# --- the real server, over a WebSocket ------------------------------------------------------------------------------------

TOKEN = "exec-cancel-token"


@pytest_asyncio.fixture
async def server(tmp_path: Path) -> AsyncIterator[tuple[TestServer, Path]]:
    from primer_runtime.server import build_app

    root = tmp_path / "workspace"
    root.mkdir()
    test_server = TestServer(build_app(token=TOKEN, workspace_root=str(root)))
    await test_server.start_server()
    try:
        yield test_server, root
    finally:
        await test_server.close()


async def _connect(test_server: TestServer, session: aiohttp.ClientSession) -> aiohttp.ClientWebSocketResponse:
    url = str(test_server.make_url("/")).replace("http://", "ws://")
    ws = await session.ws_connect(url, headers={"Authorization": f"Bearer {TOKEN}"})
    await ws.send_str(json.dumps({"req_id": 0, "op": "hello", "args": {"protocol": "1.4", "client": "test"}}))
    hello = json.loads((await asyncio.wait_for(ws.receive(), timeout=5.0)).data)
    assert hello["ok"] is True
    return ws


async def _response_for(ws: aiohttp.ClientWebSocketResponse, req_id: int, within: float = 10.0) -> dict:
    deadline = time.monotonic() + within
    while time.monotonic() < deadline:
        msg = await asyncio.wait_for(ws.receive(), timeout=max(0.1, deadline - time.monotonic()))
        frame = json.loads(msg.data)
        if frame.get("req_id") == req_id and "ok" in frame:
            return frame
    raise AssertionError(f"no response for req_id {req_id}")


async def test_the_server_reports_protocol_1_4_in_its_hello(server) -> None:
    test_server, _ = server
    async with aiohttp.ClientSession() as session:
        url = str(test_server.make_url("/")).replace("http://", "ws://")
        ws = await session.ws_connect(url, headers={"Authorization": f"Bearer {TOKEN}"})
        await ws.send_str(json.dumps({"req_id": 0, "op": "hello", "args": {"protocol": "1.2", "client": "test"}}))
        hello = json.loads((await asyncio.wait_for(ws.receive(), timeout=5.0)).data)
        await ws.close()

    assert hello["result"]["protocol"] == "1.4"


async def test_exec_cancel_over_the_wire_stops_the_command_and_answers_ok(server) -> None:
    test_server, root = server
    child = None
    async with aiohttp.ClientSession() as session:
        ws = await _connect(test_server, session)
        try:
            await ws.send_str(json.dumps({
                "req_id": 5, "op": "exec",
                "args": {"cmd": ["/bin/sh", "-c", f"sleep 60 & echo $! > {root}/child; wait"], "workdir": str(root)},
            }))
            child = await _pid(root / "child")

            await ws.send_str(json.dumps({"req_id": 6, "op": "exec_cancel", "args": {"target_req_id": 5}}))
            answer = await _response_for(ws, 6)

            assert answer["ok"] is True
            assert await _gone(child), "exec_cancel over the wire left the command running"
        finally:
            await ws.close()
            _kill(child)


async def test_exec_cancel_for_an_exec_that_is_not_running_answers_enoent(server) -> None:
    test_server, _ = server
    async with aiohttp.ClientSession() as session:
        ws = await _connect(test_server, session)
        try:
            await ws.send_str(json.dumps({"req_id": 6, "op": "exec_cancel", "args": {"target_req_id": 12345}}))
            answer = await _response_for(ws, 6)
        finally:
            await ws.close()

    assert answer["ok"] is False and answer["error"]["code"] == "ENOENT"


@pytest.mark.parametrize("args", [{}, {"target_req_id": "five"}, {"target_req_id": None}, {"target_req_id": True}])
async def test_a_malformed_exec_cancel_is_refused_and_the_connection_stays_usable(server, args) -> None:
    """Arguments come from client frames: whatever they hold, the message loop must survive (an exception escaping it would
    skip the connection's teardown and leak every exec on it)."""
    test_server, _ = server
    async with aiohttp.ClientSession() as session:
        ws = await _connect(test_server, session)
        try:
            await ws.send_str(json.dumps({"req_id": 6, "op": "exec_cancel", "args": args}))
            answer = await _response_for(ws, 6)
            await ws.send_str(json.dumps({"req_id": 7, "op": "health"}))
            health = await _response_for(ws, 7)
        finally:
            await ws.close()

    assert answer["ok"] is False and answer["error"]["code"] in ("EPROTOCOL", "ENOENT")
    assert health["ok"] is True, "the connection was left unusable by a malformed exec_cancel"
