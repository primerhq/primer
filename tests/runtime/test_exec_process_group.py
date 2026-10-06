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
import base64
import json
import logging
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

_REPO_ROOT = Path(__file__).resolve().parents[2]

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
                # the detached process writes its OWN pid after setsid(): ``$!`` is known the moment the shell forks, before
                # the child has left the group, and a pid read then is a process the group stop still reaches
                f"setsid sh -c 'echo $$ > {tmp_path}/detached; exec sleep 60' & sleep 60 & echo $! > {tmp_path}/foreground; wait",
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


async def test_the_leader_is_signalled_once_through_its_group_not_twice(tmp_path: Path) -> None:
    """The stop used to signal the group AND then the leader directly, so a command that traps SIGTERM got it twice (a
    program that treats a second SIGTERM as "force quit" skipped its cleanup). A leader in its own group is reached through
    the group alone; only a process that leads no group is signalled directly."""
    from primer_runtime import process_group

    direct: list[tuple[int, int]] = []
    real_kill = os.kill

    def spy(pid: int, sig: int) -> None:
        direct.append((pid, sig))
        real_kill(pid, sig)

    original = process_group.os.kill
    process_group.os.kill = spy
    try:
        events = await asyncio.wait_for(
            _drain(_exec(tmp_path, "sleep 60 & echo $! > " + str(tmp_path) + "/child; wait", WorkspaceLockTable(), timeout_s=1.0)),
            timeout=15.0,
        )
    finally:
        process_group.os.kill = original
        child = int((tmp_path / "child").read_text().strip()) if (tmp_path / "child").exists() else None
        _kill(child)

    assert events[-1].data == {"code": -1, "timed_out": True}
    assert direct == [], f"the leader was signalled directly as well as through its group: {direct}"


# --- primer_runtime as PID 1: killed children are never reaped --------------------------------------------------------------

#: Runs in a child python. ``PR_SET_CHILD_SUBREAPER`` makes it adopt the orphans of the commands it starts and, like PID 1
#: in the runtime image (no init), never reap them: every process the group stop takes down stays a ZOMBIE in the group,
#: and ``killpg(pgid, 0)`` finds a zombie "present" for ever.
_AS_PID_ONE = r'''
import asyncio, ctypes, json, logging, os, sys, time

PR_SET_CHILD_SUBREAPER = 36
if ctypes.CDLL(None, use_errno=True).prctl(PR_SET_CHILD_SUBREAPER, 1, 0, 0, 0) != 0:
    print(json.dumps({"subreaper": False}))
    sys.exit(0)

from primer_runtime.exec import run_exec
from primer_runtime.locks import WorkspaceLockTable

warnings = []


class Collect(logging.Handler):
    def emit(self, record):
        if record.levelno >= logging.WARNING:
            warnings.append(record.getMessage())


logging.getLogger("primer_runtime.process_group").addHandler(Collect())


def zombie_children():
    count = 0
    for name in os.listdir("/proc"):
        if not name.isdigit():
            continue
        try:
            data = open(f"/proc/{name}/stat", "rb").read()
        except OSError:
            continue
        fields = data[data.rindex(b")") + 2:].split()
        if fields[0] == b"Z" and int(fields[1]) == os.getpid():
            count += 1
    return count


async def main(workdir, script):
    args = {"cmd": ["/bin/sh", "-c", script], "workdir": workdir, "timeout_s": 1.0}
    start = time.monotonic()
    events = [event async for event in run_exec(1, args, workdir, WorkspaceLockTable())]
    elapsed = time.monotonic() - start
    print(json.dumps({
        "subreaper": True, "elapsed": elapsed, "last": events[-1].data, "warnings": warnings, "zombies": zombie_children(),
    }))


asyncio.run(main(sys.argv[1], sys.argv[2]))
'''


@pytest.mark.skipif(sys.platform != "linux", reason="PR_SET_CHILD_SUBREAPER is Linux-only")
@pytest.mark.parametrize(
    "script",
    ["sleep 60 & wait", "sleep 60 | cat"],
    ids=["a-forked-child", "a-pipeline"],
)
async def test_a_stop_is_not_held_up_by_zombies_nothing_reaps(tmp_path: Path, script: str) -> None:
    """The runtime image runs the runtime as PID 1 with no init, so the children a group stop takes down are orphaned to a
    process that never reaps them. They are dead; waiting for them to disappear waited out the whole SIGTERM grace (5 s)
    and then the SIGKILL wait on EVERY timeout or cancel of a forking command. The check must look at a member's state and
    ignore a zombie."""
    probe = tmp_path / "as_pid_one.py"
    probe.write_text(_AS_PID_ONE)
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(filter(None, [str(_REPO_ROOT / "runtime"), str(_REPO_ROOT), env.get("PYTHONPATH")]))

    done = await asyncio.to_thread(
        subprocess.run, [sys.executable, str(probe), str(tmp_path), script],
        capture_output=True, text=True, timeout=60, env=env,
    )

    assert done.returncode == 0, done.stderr
    out = json.loads(done.stdout.strip().splitlines()[-1])
    if not out["subreaper"]:
        pytest.skip("this environment does not allow PR_SET_CHILD_SUBREAPER")
    assert out["last"] == {"code": -1, "timed_out": True}
    assert out["zombies"] >= 1, "nothing was left unreaped: the test is not in the situation it is about"
    assert out["elapsed"] < 1.0 + 1.0, f"the exec took {out['elapsed']:.2f}s with a 1s timeout: the stop waited on zombies"
    assert out["warnings"] == [], out["warnings"]


async def test_a_leader_the_group_stop_already_took_down_is_reported_as_it_died_not_as_255(caplog) -> None:
    """Signalling the leader through ``Popen.send_signal`` polls the child first (``waitpid(WNOHANG)``), so a leader the
    group signal had already taken down was reaped by that poll and asyncio reported exit code 255 and logged "exit status
    already read". Here the leader is dead before the stop looks (the loop is blocked while it dies)."""
    from primer_runtime.process_group import NEW_SESSION, stop_process_group

    caplog.set_level(logging.WARNING)
    proc = await asyncio.create_subprocess_exec("sleep", "60", stdout=asyncio.subprocess.PIPE, **NEW_SESSION)
    try:
        os.killpg(proc.pid, signal.SIGKILL)
        time.sleep(0.2)                                   # the leader is dead; the loop has not yet looked at it

        await stop_process_group(proc)
        await asyncio.sleep(0.2)                          # let the child watcher report the exit, if it can

        assert proc.returncode == -signal.SIGKILL
        assert not [r for r in caplog.records if "already read" in r.getMessage()], "the child was reaped behind asyncio's back"
    finally:
        _kill(proc.pid)


@pytest.mark.parametrize("second_cancel_after", ["five-loop-spins", 0.3], ids=["five-loop-spins", "inside-the-grace"])
async def test_a_second_cancel_inside_the_grace_still_kills_a_command_that_ignores_sigterm(
    tmp_path: Path, monkeypatch, second_cancel_after,
) -> None:
    """The SIGKILL is sent in a ``finally`` and the first signal synchronously, so a consumer cancelled AGAIN while the stop
    is waiting out the grace still delivers the kill: a command that ignores SIGTERM must not outlive the second cancel (it
    would, with the lock released over it, if the SIGKILL were skipped by the second cancel or delayed behind an await).

    The lock is released once the SIGKILL has been SENT, not once the command is confirmed gone, so a queued writer may
    start a moment before the killed process has exited: the writer is given a short grace to see it gone."""
    from primer_runtime import process_group

    monkeypatch.setattr(process_group, "TERM_GRACE_S", 2.0)
    locks = WorkspaceLockTable()
    child = None
    task = asyncio.create_task(_drain(_exec(tmp_path, f"trap '' TERM; sleep 60 & echo $! > {tmp_path}/child; wait", locks)))
    try:
        child = await _pid(tmp_path / "child")
        gone_soon_after_the_writer_got_in: list[bool] = []

        async def writer() -> None:
            async with locks.hold_scope(str(tmp_path.resolve())):
                gone_soon_after_the_writer_got_in.append(await _gone(child, within=0.5))

        queued = asyncio.create_task(writer())
        await asyncio.sleep(0.05)
        assert not queued.done()

        task.cancel()                                     # the stop begins: SIGTERM is ignored, the grace starts
        if second_cancel_after == "five-loop-spins":
            for _ in range(5):
                await asyncio.sleep(0)
        else:
            await asyncio.sleep(second_cancel_after)
        task.cancel()                                     # ... and the consumer is cancelled again, inside the grace

        start = asyncio.get_running_loop().time()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=5.0)
        assert asyncio.get_running_loop().time() - start < 1.0, "the second cancel waited out the grace"
        assert await _gone(child, within=1.0), "a command that ignores SIGTERM outlived the second cancel"
        await asyncio.wait_for(queued, timeout=5.0)
        assert gone_soon_after_the_writer_got_in == [True], "the command still ran 0.5 s after a writer got the lock"
    finally:
        _kill(child)


async def test_a_cancel_after_the_timeout_started_the_stop_does_not_wait_out_the_grace_either(
    tmp_path: Path, monkeypatch,
) -> None:
    """The exception is not limited to a SECOND cancel: a consumer's FIRST cancel that arrives while a stop started by the
    TIMEOUT is waiting out the grace also delivers the SIGKILL at once and propagates, and the lock goes to the next writer
    without waiting the grace out."""
    from primer_runtime import process_group

    monkeypatch.setattr(process_group, "TERM_GRACE_S", 2.0)
    locks = WorkspaceLockTable()
    child = None
    task = asyncio.create_task(
        _drain(_exec(tmp_path, f"trap '' TERM; sleep 60 & echo $! > {tmp_path}/child; wait", locks, timeout_s=0.3)),
    )
    try:
        child = await _pid(tmp_path / "child")
        gone_soon_after_the_writer_got_in: list[bool] = []

        async def writer() -> None:
            async with locks.hold_scope(str(tmp_path.resolve())):
                gone_soon_after_the_writer_got_in.append(await _gone(child, within=0.5))

        queued = asyncio.create_task(writer())
        await asyncio.sleep(0.8)                          # the timeout fired at 0.3 s: the stop is waiting out its grace
        assert not task.done() and not queued.done(), "the stop was not waiting out the grace: the test is not in its situation"

        task.cancel()                                     # the consumer's FIRST cancel
        start = asyncio.get_running_loop().time()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=5.0)
        assert asyncio.get_running_loop().time() - start < 1.0, "the cancel waited out the grace"
        assert await _gone(child, within=1.0), "a command that ignores SIGTERM outlived the cancel"
        await asyncio.wait_for(queued, timeout=5.0)
        assert gone_soon_after_the_writer_got_in == [True]
    finally:
        _kill(child)


async def test_a_command_that_needs_a_second_to_clean_up_after_sigterm_gets_to_finish(tmp_path: Path) -> None:
    """Pins the SIGTERM grace at its documented length: a command that traps SIGTERM and needs about a second to clean up
    must not be SIGKILLed first (a grace shorter than that cuts it off before the marker is written)."""
    script = f"trap 'sleep 1; echo cleaned > {tmp_path}/marker; exit 0' TERM; sleep 60 & wait"

    events = await asyncio.wait_for(_drain(_exec(tmp_path, script, WorkspaceLockTable(), timeout_s=1.0)), timeout=15.0)

    assert events[-1].data == {"code": -1, "timed_out": True}
    assert (tmp_path / "marker").exists(), "the command was cut off before it could clean up: the SIGTERM grace is too short"


_BIG_STDIN = b"x" * (1024 * 1024)       # far more than a pipe holds, so a command that does not read it blocks the writer


def _exec_with_stdin(tmp_path: Path, script: str, stdin: bytes, locks: WorkspaceLockTable, *, timeout_s: float):
    args = {
        "cmd": ["/bin/sh", "-c", script], "workdir": str(tmp_path), "timeout_s": timeout_s,
        "stdin_b64": base64.b64encode(stdin).decode(),
    }
    return run_exec(1, args, str(tmp_path), locks)


async def test_a_command_that_reads_its_stdin_still_gets_it(tmp_path: Path) -> None:
    """The control for the three below: stdin that IS read arrives, whole."""
    events = await asyncio.wait_for(
        _drain(_exec_with_stdin(tmp_path, "cat", b"hello stdin", WorkspaceLockTable(), timeout_s=10.0)), timeout=15.0,
    )

    out = b"".join(base64.b64decode(e.data["data_b64"]) for e in events if e.event == "stdout")
    assert out == b"hello stdin" and events[-1].data == {"code": 0}


async def test_a_command_that_never_reads_a_large_stdin_still_times_out(tmp_path: Path) -> None:
    """The stdin write used to sit outside the timeout: ``drain()`` blocked for as long as the command did not read, so the
    timeout never fired and the exec hung."""
    child = None
    try:
        start = time.monotonic()
        events = await asyncio.wait_for(
            _drain(_exec_with_stdin(
                tmp_path, f"sleep 60 & echo $! > {tmp_path}/child; wait", _BIG_STDIN, WorkspaceLockTable(), timeout_s=1.0,
            )),
            timeout=15.0,
        )
        child = await _pid(tmp_path / "child")

        assert events[-1].data == {"code": -1, "timed_out": True}
        assert time.monotonic() - start < 8.0
        assert await _gone(child), "the timed-out command kept running"
    finally:
        written = tmp_path / "child"      # a hung exec never returned the pid above: do not leak the command it started
        _kill(child or (int(written.read_text().strip()) if written.exists() and written.read_text().strip() else None))


async def test_a_cancel_while_the_stdin_is_still_being_written_kills_the_group_before_the_lock_is_released(
    tmp_path: Path,
) -> None:
    """The stdin write also sat outside the ``finally``: a cancel there left the command running and released the lock."""
    locks = WorkspaceLockTable()
    child = None
    task = asyncio.create_task(_drain(_exec_with_stdin(
        tmp_path, f"sleep 60 & echo $! > {tmp_path}/child; wait", _BIG_STDIN, locks, timeout_s=60.0,
    )))
    try:
        child = await _pid(tmp_path / "child")
        running_when_the_writer_got_in: list[bool] = []

        async def writer() -> None:
            async with locks.hold_scope(str(tmp_path.resolve())):
                running_when_the_writer_got_in.append(_running(child))

        queued = asyncio.create_task(writer())
        await asyncio.sleep(0.3)                          # the command is running and the stdin write is blocked
        assert not queued.done()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=10.0)

        assert await _gone(child), "a cancel during the stdin write left the command running"
        await asyncio.wait_for(queued, timeout=10.0)
        assert running_when_the_writer_got_in == [False], "the lock was released while the command still ran"
    finally:
        _kill(child)


async def test_a_command_that_writes_a_lot_before_it_reads_stdin_does_not_deadlock(tmp_path: Path) -> None:
    """The stdin is fed alongside the readers: the command below writes 3 MB to stdout (blocking until something reads
    it) BEFORE it reads stdin. With the write done first and the readers started after, neither side could move."""
    script = "head -c 3000000 /dev/zero; cat > /dev/null; echo done"

    events = await asyncio.wait_for(
        _drain(_exec_with_stdin(tmp_path, script, _BIG_STDIN, WorkspaceLockTable(), timeout_s=30.0)), timeout=10.0,
    )

    out = b"".join(base64.b64decode(e.data["data_b64"]) for e in events if e.event == "stdout")
    assert events[-1].data == {"code": 0}
    assert len(out) == 3_000_000 + len(b"done\n") and out.endswith(b"done\n")


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
