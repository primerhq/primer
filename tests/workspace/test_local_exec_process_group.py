"""The local ``exec`` tool kills the command's whole PROCESS GROUP on a timeout and on a cancel.

Before, it killed only the shell. ``sh -c "sleep 30"`` forks (dash) and a pipeline or a backgrounded job always does, so
the shell's death left its children running, holding the stdout pipe open:

* the timeout was not a bound: ``proc.wait()`` after ``proc.kill()`` returns only when the pipes close, so a command
  with a 0.5 s timeout and a 3 s child returned after 3.0 s (a ``exec sleep 3``, which does not fork, after 0.5 s);
* a CANCEL (the hard Cancel, a server shutdown, and the Stop's tool-cancel to come) killed nothing at all and released
  the workspace write lock while the command kept running, so a leaked writer no longer serialised with the others.

The command now starts in its own session (``start_new_session``, as ``LocalWorkspace.diagnostic_exec`` and the init
command already do), and a timeout or a cancel SIGKILLs that group. What this does and does not reach, pinned below:

* a process the command DELIBERATELY detached (``setsid``, a daemon that double-forks and calls ``setsid``) is in its own
  session and survives the kill: the group kill never reaches out of the group the exec started;
* ``nohup`` alone does NOT detach (it only ignores SIGHUP): such a job stays in the group and is killed on a timeout or a
  cancel (SIGKILL cannot be ignored);
* nothing is killed when the command FINISHES: a background job it left running (output redirected, so it does not hold
  the pipes) is left alone, exactly as before.
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

from primer.model.except_ import BadRequestError
from primer.workspace._locks import WorkspaceLockTable
from primer.workspace.local.tools.exec_ import Exec, ExecArgs

pytestmark = pytest.mark.skipif(
    sys.platform not in ("linux", "darwin") or shutil.which("setsid") is None,
    reason="process groups, ps and setsid are needed",
)


def _running(pid: int) -> bool:
    """True when ``pid`` is alive and not a zombie (``ps`` knows both)."""
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


def _open_fds() -> int:
    """How many file descriptors this process has open (the leak a held pipe causes). Collected first so a transport that
    is merely unreferenced does not count."""
    import gc

    gc.collect()
    return len(os.listdir("/proc/self/fd" if os.path.isdir("/proc/self/fd") else "/dev/fd"))


def _kill(pid: int) -> None:
    try:
        os.kill(pid, signal.SIGKILL)
    except ProcessLookupError:
        pass


def _tool(tmp_path: Path) -> tuple[Exec, WorkspaceLockTable]:
    locks = WorkspaceLockTable()
    return Exec(tmp_path, locks=locks), locks


def _args(command: str, *, timeout_ms: int = 60_000) -> ExecArgs:
    return ExecArgs(command=command, timeout_ms=timeout_ms, description="probe")


async def _lock_is_free(locks: WorkspaceLockTable, root: Path) -> bool:
    try:
        async with asyncio.timeout(0.5):
            async with locks.hold_scope(str(root.resolve())):
                return True
    except TimeoutError:
        return False


async def test_a_timeout_returns_at_the_timeout_not_at_the_end_of_the_childs_life(tmp_path: Path) -> None:
    """``sleep 8`` under dash forks: before, the exec took the child's full 8 s to give up on a 0.5 s timeout."""
    tool, _ = _tool(tmp_path)
    start = time.monotonic()

    try:
        with pytest.raises(BadRequestError, match="timed out"):
            await tool.execute(_args("sleep 8 & echo $! > child.pid; wait", timeout_ms=500), None)

        assert time.monotonic() - start < 4.0, "the timeout was not a bound: it waited for the child"
        assert await _gone(await _pid(tmp_path / "child.pid")), "the timed-out command's child is still running"
    finally:
        if (tmp_path / "child.pid").exists():
            _kill(int((tmp_path / "child.pid").read_text().strip()))


@pytest.mark.parametrize(
    "command",
    ["sleep 60 & echo $! > child.pid; wait", "(sleep 60 & echo $! > child.pid; wait)"],
    ids=["background-job", "subshell"],
)
async def test_a_cancel_kills_the_children_and_returns_promptly(tmp_path: Path, command: str) -> None:
    tool, locks = _tool(tmp_path)
    task = asyncio.create_task(tool.execute(_args(command), None))
    child = await _pid(tmp_path / "child.pid")
    try:
        assert _running(child)

        task.cancel()
        start = time.monotonic()
        with pytest.raises(asyncio.CancelledError):
            await task

        assert time.monotonic() - start < 3.0, "the cancel waited for the command"
        assert await _gone(child), "a cancelled command's child kept running"
        assert await _lock_is_free(locks, tmp_path), "the write lock is held after the cancel"
    finally:
        _kill(child)


@pytest.mark.parametrize("how", ["cancel", "timeout"])
async def test_the_write_lock_is_not_released_while_the_command_still_runs(tmp_path: Path, how: str) -> None:
    """The lock must outlive the process, not the task. A writer is already QUEUED on the scope lock when the command is
    stopped, and it records, at the instant it acquires and with no polling, whether the child is still running: a kill
    that is deferred, or that runs after the lock is released, lets it in while the child lives."""
    tool, locks = _tool(tmp_path)
    run = asyncio.create_task(
        tool.execute(_args("sleep 60 & echo $! > child.pid; wait", timeout_ms=800 if how == "timeout" else 60_000), None),
    )
    child = await _pid(tmp_path / "child.pid")
    still_running_when_the_writer_got_in: list[bool] = []

    async def queued_writer() -> None:
        async with locks.hold_scope(str(tmp_path.resolve())):
            still_running_when_the_writer_got_in.append(_running(child))

    writer = asyncio.create_task(queued_writer())
    await asyncio.sleep(0.05)                      # the writer is now waiting on the lock the exec holds
    assert not writer.done()
    try:
        if how == "cancel":
            run.cancel()
            with pytest.raises(asyncio.CancelledError):
                await run
        else:
            with pytest.raises(BadRequestError, match="timed out"):
                await run
        await asyncio.wait_for(writer, timeout=5.0)

        assert still_running_when_the_writer_got_in == [False], "the lock was released while the child was still running"
    finally:
        writer.cancel()
        _kill(child)


async def test_a_second_cancel_while_the_kill_is_waiting_does_not_leave_the_command_running(tmp_path: Path) -> None:
    """The signal is sent FIRST and synchronously: a caller that is cancelled again while the kill is still waiting for
    the process to go (the outer Cancel after a Stop's own cancel) must still have delivered it."""
    tool, _ = _tool(tmp_path)
    task = asyncio.create_task(tool.execute(_args("sleep 60 & echo $! > child.pid; wait"), None))
    child = await _pid(tmp_path / "child.pid")
    try:
        task.cancel()
        for _ in range(5):
            await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        assert await _gone(child), "a second cancel left the command running"
    finally:
        _kill(child)


@pytest.mark.parametrize("how", ["timeout", "cancel"])
async def test_a_process_the_command_detached_with_setsid_survives_the_kill(tmp_path: Path, how: str) -> None:
    """A deliberately detached process (setsid, i.e. what a daemon does) is in its OWN session, outside the group the
    exec started, so the group kill does not reach it; its foreground sibling in the group does die."""
    tool, _ = _tool(tmp_path)
    command = (
        # the detached process writes its OWN pid after setsid() (``$!`` can be read before the child has left the group)
        "setsid sh -c 'echo $$ > detached.pid; exec sleep 60' > /dev/null 2>&1 & "
        "sleep 60 & echo $! > foreground.pid; wait"
    )
    detached = foreground = None
    try:
        if how == "timeout":
            run = asyncio.create_task(tool.execute(_args(command, timeout_ms=1500), None))
            detached, foreground = await _pid(tmp_path / "detached.pid"), await _pid(tmp_path / "foreground.pid")
            with pytest.raises(BadRequestError, match="timed out"):
                await run
        else:
            run = asyncio.create_task(tool.execute(_args(command), None))
            detached, foreground = await _pid(tmp_path / "detached.pid"), await _pid(tmp_path / "foreground.pid")
            run.cancel()
            with pytest.raises(asyncio.CancelledError):
                await run

        assert await _gone(foreground), "the foreground job in the group was not killed"
        await asyncio.sleep(0.3)
        assert _running(detached), "the group kill reached a process the command detached on purpose"
    finally:
        for pid in (detached, foreground):
            if pid:
                _kill(pid)


@pytest.mark.parametrize("how", ["timeout", "cancel"])
async def test_a_detached_process_that_holds_the_pipes_does_not_delay_the_kill(tmp_path: Path, how: str) -> None:
    """A setsid'd child that INHERITED stdout/stderr survives the group kill and keeps the pipes open. ``proc.wait()``
    right after the kill blocks until the pipes close when the leader's exit is not yet recorded (the wait also waits
    for the pipes), so a kill that waited that way took its whole reap bound (5 s) here. The kill must wait for the exit
    itself and then close the pipes, and the exec must return at once."""
    tool, _ = _tool(tmp_path)
    command = "setsid sh -c 'echo $$ > detached.pid; exec sleep 60' & sleep 60 & echo $! > foreground.pid; wait"
    detached = foreground = None
    fds_before = _open_fds()
    try:
        run = asyncio.create_task(tool.execute(_args(command, timeout_ms=800 if how == "timeout" else 60_000), None))
        detached, foreground = await _pid(tmp_path / "detached.pid"), await _pid(tmp_path / "foreground.pid")
        start = time.monotonic()
        if how == "cancel":
            run.cancel()
            with pytest.raises(asyncio.CancelledError):
                await run
        else:
            with pytest.raises(BadRequestError, match="timed out"):
                await run

        assert time.monotonic() - start < 2.5, "the kill waited on pipes a detached process holds open"
        assert await _gone(foreground)
        assert _running(detached), "the group kill reached a process that was detached on purpose"
        assert _open_fds() <= fds_before, "the subprocess pipes are still open: the detached process holds them"
    finally:
        for pid in (detached, foreground):
            if pid:
                _kill(pid)


async def test_any_other_way_out_of_the_wait_also_kills_the_group(tmp_path: Path, monkeypatch) -> None:
    """The kill belongs in a ``finally``, not in two ``except`` arms: an exception that is neither the timeout nor a
    cancel (here a failure inside the wait) must not leave the command running either."""
    tool, locks = _tool(tmp_path)
    child = None

    async def explode(self, input=None):   # noqa: A002
        await _pid(tmp_path / "child.pid")
        raise RuntimeError("the wait failed")

    monkeypatch.setattr(asyncio.subprocess.Process, "communicate", explode)
    try:
        with pytest.raises(RuntimeError, match="the wait failed"):
            await tool.execute(_args("sleep 60 & echo $! > child.pid; wait"), None)

        child = await _pid(tmp_path / "child.pid")
        assert await _gone(child), "a command was left running after the wait failed"
        assert await _lock_is_free(locks, tmp_path)
    finally:
        if child:
            _kill(child)


async def test_nohup_alone_does_not_detach_a_job_from_the_group(tmp_path: Path) -> None:
    """``nohup`` only ignores SIGHUP; the job stays in the exec's group and SIGKILL cannot be ignored."""
    tool, _ = _tool(tmp_path)
    task = asyncio.create_task(tool.execute(_args("nohup sleep 60 > /dev/null 2>&1 & echo $! > job.pid; wait"), None))
    job = await _pid(tmp_path / "job.pid")
    try:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        assert await _gone(job), "a nohup'd job in the group survived the kill"
    finally:
        _kill(job)


async def test_a_command_that_finishes_leaves_a_backgrounded_process_alone(tmp_path: Path) -> None:
    """Nothing is killed on a normal finish: a job the command left running (output redirected so it does not hold the
    pipes) stays, exactly as before this change."""
    tool, _ = _tool(tmp_path)
    result = await tool.execute(_args("sleep 60 > /dev/null 2>&1 & echo $! > job.pid"), None)

    job = await _pid(tmp_path / "job.pid")
    try:
        assert result.metadata["exit_code"] == 0
        await asyncio.sleep(0.3)
        assert _running(job), "a normal finish killed a background job"
    finally:
        _kill(job)


async def test_a_command_that_finishes_is_unchanged(tmp_path: Path) -> None:
    tool, _ = _tool(tmp_path)

    result = await tool.execute(_args("echo hi; echo err >&2; exit 3"), None)

    assert result.metadata["exit_code"] == 3
    assert result.output.split("\n")[0] == "3" and "hi" in result.output and "err" in result.output
