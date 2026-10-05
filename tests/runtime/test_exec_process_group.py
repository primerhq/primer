"""The runtime's ``exec`` kills the command's whole PROCESS GROUP when it is stopped (the docker/k8s half of B0).

``run_exec`` used to ``terminate()`` / ``kill()`` only the direct child, ``/bin/sh -c ...``. A shell forks (dash does for
``sh -c "sleep 30"``), so its children kept running after the "kill", holding the stdout/stderr pipes and, in the runtime,
the Tier-B write lock for as long as they lived. And there were two ways out that killed nothing at all: an exec task
cancelled while it was blocked in ``send`` closes the generator at a ``yield`` (``GeneratorExit``), which neither the
``TimeoutError`` nor the ``CancelledError`` arm saw, so the process was never signalled.

The command now starts in its own session and every way out of the exec but the command finishing stops the whole group:
SIGTERM, a grace period (the commands that clean up on SIGTERM keep doing so), then SIGKILL, then a wait for the group to
be gone and a close of the pipes. As on the local backend: a process the command DELIBERATELY detached (``setsid``) is
outside the group and survives, ``nohup`` alone does not detach, and nothing is killed when the command FINISHES.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from primer_runtime.exec import ExecRegistry, run_exec, start_exec
from primer_runtime.locks import WorkspaceLockTable

pytestmark = pytest.mark.skipif(
    sys.platform not in ("linux", "darwin") or shutil.which("setsid") is None,
    reason="process groups, ps and setsid are needed",
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


async def _pid(path: Path, within: float = 5.0) -> int:
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


def _open_fds() -> int:
    import gc

    gc.collect()
    return len(os.listdir("/proc/self/fd" if os.path.isdir("/proc/self/fd") else "/dev/fd"))


def _exec(tmp_path: Path, script: str, locks: WorkspaceLockTable, *, timeout_s: float = 60.0):
    args = {"cmd": ["/bin/sh", "-c", script], "workdir": str(tmp_path), "timeout_s": timeout_s}
    return run_exec(1, args, str(tmp_path), locks)


async def _drain(agen) -> list:
    return [event async for event in agen]


async def test_a_timeout_kills_what_the_command_started_and_reports_it(tmp_path: Path) -> None:
    child = None
    try:
        events = await asyncio.wait_for(
            _drain(_exec(tmp_path, f"sleep 60 & echo $! > {tmp_path}/child; wait", WorkspaceLockTable(), timeout_s=1.0)),
            timeout=15.0,
        )
        child = await _pid(tmp_path / "child")

        assert events[-1].event == "exit" and events[-1].data == {"code": -1, "timed_out": True}
        assert await _gone(child), "a process the timed-out command started kept running"
    finally:
        _kill(child)


async def test_a_cancel_of_the_consumer_kills_the_group(tmp_path: Path) -> None:
    child = None
    task = asyncio.create_task(_drain(_exec(tmp_path, f"sleep 60 & echo $! > {tmp_path}/child; wait", WorkspaceLockTable())))
    try:
        child = await _pid(tmp_path / "child")
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        assert await _gone(child), "a cancelled exec left its command running"
    finally:
        _kill(child)


async def test_closing_the_generator_at_a_yield_kills_the_group(tmp_path: Path) -> None:
    """The path nothing covered: the consumer is cancelled while it is blocked sending a frame, so the generator is closed
    where it is suspended, at a ``yield`` (GeneratorExit), and neither the timeout nor the cancel arm ran."""
    child = None
    agen = _exec(tmp_path, f"echo started; sleep 60 & echo $! > {tmp_path}/child; wait", WorkspaceLockTable())
    try:
        first = await asyncio.wait_for(agen.__anext__(), timeout=10.0)
        assert first.event == "stdout"
        child = await _pid(tmp_path / "child")

        await agen.aclose()

        assert await _gone(child), "closing the generator left the command running"
    finally:
        _kill(child)


async def test_a_cancel_while_the_frame_is_being_sent_kills_the_group(tmp_path: Path) -> None:
    """The same path through the real task: ``send`` never returns (a stalled socket), the task is cancelled there."""
    child = None
    blocked = asyncio.Event()

    async def send(frame: str) -> None:
        blocked.set()
        await asyncio.Event().wait()

    task = start_exec(
        1, {"cmd": ["/bin/sh", "-c", f"echo hi; sleep 60 & echo $! > {tmp_path}/child; wait"], "workdir": str(tmp_path)},
        str(tmp_path), WorkspaceLockTable(), send, ExecRegistry(),
    )
    try:
        await asyncio.wait_for(blocked.wait(), timeout=10.0)
        child = await _pid(tmp_path / "child")
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        assert await _gone(child), "a cancel during send left the command running"
    finally:
        _kill(child)


async def test_the_write_lock_is_not_released_while_the_command_still_runs(tmp_path: Path) -> None:
    """A writer queued on the scope lock gets in only when the command is gone, checked at that instant, no polling."""
    locks = WorkspaceLockTable()
    task = asyncio.create_task(_drain(_exec(tmp_path, f"sleep 60 & echo $! > {tmp_path}/child; wait", locks)))
    child = await _pid(tmp_path / "child")
    running_when_the_writer_got_in: list[bool] = []

    async def writer() -> None:
        async with locks.hold_scope(str(tmp_path.resolve())):
            running_when_the_writer_got_in.append(_running(child))

    queued = asyncio.create_task(writer())
    await asyncio.sleep(0.05)
    assert not queued.done()
    try:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await asyncio.wait_for(queued, timeout=10.0)

        assert running_when_the_writer_got_in == [False], "the lock was released while the command still ran"
    finally:
        queued.cancel()
        _kill(child)


async def test_sigterm_is_still_delivered_first_so_a_command_can_clean_up(tmp_path: Path) -> None:
    """Unchanged contract: the stop is SIGTERM, a grace period, then SIGKILL. A command that cleans up on SIGTERM does."""
    events = await asyncio.wait_for(
        _drain(_exec(
            tmp_path, f"trap 'echo cleaned > {tmp_path}/marker; exit 0' TERM; sleep 60 & wait", WorkspaceLockTable(),
            timeout_s=1.0,
        )),
        timeout=15.0,
    )

    assert events[-1].data == {"code": -1, "timed_out": True}
    assert (tmp_path / "marker").read_text().strip() == "cleaned", "SIGTERM was not delivered before the kill"


async def test_a_command_that_ignores_sigterm_is_killed_after_the_grace(tmp_path: Path, monkeypatch) -> None:
    from primer_runtime import process_group     # imported here: the module is what this change adds

    monkeypatch.setattr(process_group, "TERM_GRACE_S", 0.4)
    child = None
    try:
        start = time.monotonic()
        events = await asyncio.wait_for(
            _drain(_exec(
                tmp_path, f"trap '' TERM; sleep 60 & echo $! > {tmp_path}/child; wait", WorkspaceLockTable(), timeout_s=1.0,
            )),
            timeout=15.0,
        )
        child = await _pid(tmp_path / "child")

        assert events[-1].data == {"code": -1, "timed_out": True}
        assert time.monotonic() - start < 6.0
        assert await _gone(child), "a command that ignores SIGTERM outlived the SIGKILL"
    finally:
        _kill(child)


async def test_a_detached_process_that_holds_the_pipes_survives_and_does_not_hold_the_exec_up(tmp_path: Path) -> None:
    """A setsid process is outside the group: it survives, its foreground sibling does not, and although it holds the exec's
    pipes the exec returns promptly and its descriptors are closed."""
    detached = foreground = None
    fds_before = _open_fds()
    try:
        start = time.monotonic()
        events = await asyncio.wait_for(
            _drain(_exec(
                tmp_path,
                f"setsid sleep 60 & echo $! > {tmp_path}/detached; sleep 60 & echo $! > {tmp_path}/foreground; wait",
                WorkspaceLockTable(), timeout_s=1.0,
            )),
            timeout=15.0,
        )
        detached, foreground = await _pid(tmp_path / "detached"), await _pid(tmp_path / "foreground")

        assert events[-1].data == {"code": -1, "timed_out": True}
        assert time.monotonic() - start < 1.0 + 4.0, "the exec waited on pipes a detached process holds open"
        assert await _gone(foreground)
        assert _running(detached), "the group kill reached a process that was detached on purpose"
        assert _open_fds() <= fds_before, "the exec's pipes are still open: the detached process holds them"
    finally:
        _kill(detached, foreground)


async def test_a_command_that_finishes_leaves_a_backgrounded_process_alone(tmp_path: Path) -> None:
    job = None
    try:
        events = await asyncio.wait_for(
            _drain(_exec(tmp_path, f"sleep 60 > /dev/null 2>&1 & echo $! > {tmp_path}/job", WorkspaceLockTable())),
            timeout=15.0,
        )
        job = await _pid(tmp_path / "job")

        assert events[-1].event == "exit" and events[-1].data == {"code": 0}
        await asyncio.sleep(0.3)
        assert _running(job), "a normal finish killed a background job"
    finally:
        _kill(job)
