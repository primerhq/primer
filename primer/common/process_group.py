"""Kill a subprocess's whole PROCESS GROUP, not just the process asyncio holds.

A command run through a shell (``sh -c ...``), a pipeline and a backgrounded job all FORK, so killing the process that was
spawned (the shell) leaves its children running and holding the stdout/stderr pipes open. ``proc.wait()`` returns only
when the pipes close, so a "kill, then wait" after a timeout blocks for the child's whole life (a command with a 0.5 s
timeout and a 3 s child took 3.0 s), and a task that was merely cancelled killed nothing at all.

The fix has two halves, and the caller must do both: start the process in its own session
(``start_new_session=True``, which makes it the leader of a new group whose id is its pid) and, on a timeout or a cancel,
:func:`kill_process_group` it. ``LocalWorkspace.diagnostic_exec`` and the workspace init command already did this for
their timeouts; the agent-facing ``exec`` tool and the local python runner did not.

What the group kill does NOT reach, on purpose: a process that left the group by starting a session of its own
(``setsid``, or a daemon that double-forks and calls ``setsid``) was detached deliberately and survives. ``nohup`` alone
does not detach (it only ignores SIGHUP), so a job started with it stays in the group and is killed (SIGKILL cannot be
ignored). Nothing here runs when the command FINISHES: a job it left behind is left alone.
"""

from __future__ import annotations

import asyncio
import logging
import os
import signal

logger = logging.getLogger(__name__)

#: ``create_subprocess_*`` keyword that puts the child in its own session and process group (POSIX; ignored elsewhere).
NEW_SESSION: dict[str, bool] = {"start_new_session": True} if os.name == "posix" else {}

#: How long to wait for the killed group to be gone. SIGKILL cannot be ignored, so a member that is still there after it is
#: one that cannot die yet (a process in uninterruptible I/O); that is all this bounds, and the kill has been sent either
#: way. A ZOMBIE is not such a member (see :func:`_group_has_a_live_member`): it is already dead, only nobody has reaped it.
REAP_TIMEOUT_S = 2.0

_REAP_POLL_S = 0.01


async def kill_process_group(proc: asyncio.subprocess.Process, *, reap_timeout_s: float = REAP_TIMEOUT_S) -> None:
    """SIGKILL the process group ``proc`` leads, wait for ``proc``'s own exit, bounded, and close its pipes.

    ``proc`` must have been started with ``start_new_session=True`` (see :data:`NEW_SESSION`), so that its pid is its
    group id: the kill reaches the whole tree it started, and nothing outside it. The signal is sent FIRST and
    synchronously, so even a caller that is itself being cancelled has delivered the kill before any ``await`` can be
    interrupted. A group that is already gone is not an error. The process itself is killed as well: a process that was
    NOT started in its own session leads no group, so the group kill finds nothing and the process must still die.

    It returns once the kill has TAKEN EFFECT: ``proc`` has exited and no member of the group is still alive (a caller
    releases a write lock after this, and the lock must not be released while a member of the group still runs), bounded
    by ``reap_timeout_s``. A member that is a ZOMBIE does not count as alive: when primer is PID 1 with no init (the
    shipped image), the children this kills are orphaned to a process that never reaps them, and ``killpg(pgid, 0)``
    reports a zombie present for ever, which would make every kill wait out the whole bound. The wait is on
    ``proc.returncode`` and the group, NOT ``proc.wait()``: ``wait()`` also waits for the stdout/stderr pipes to close
    whenever the exit has not been recorded yet (and it has not, right after the signal), and a process that left the
    group (``setsid``) can hold those pipes open for as long as it lives. The pipes are then closed here, in a
    ``finally``, so the caller never inherits them.

    The process itself is signalled with ``os.kill``, not ``proc.kill()``: ``Popen.send_signal`` first POLLS the child
    (``waitpid(WNOHANG)``), and a leader the group kill has already taken down is reaped by that poll, so asyncio's own
    wait then finds nothing to read and reports exit code 255 (and logs "exit status already read") instead of -9.

    An accepted edge: once the leader is gone and its group empty, its id is no longer reserved, so the group signal
    (and the emptiness check) could in theory reach an unrelated group if the pid was recycled to a new group leader in
    the meantime. That needs a pid wrap inside the caller's timeout; ``LocalWorkspace.diagnostic_exec`` and the init
    command carry the same edge.
    """
    if os.name == "posix":
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except OSError:
            pass          # ProcessLookupError: every member is already gone; PermissionError: a member changed uid
    try:
        _kill_the_process_itself(proc)
        loop = asyncio.get_running_loop()
        deadline = loop.time() + reap_timeout_s
        while not _is_gone(proc):
            if loop.time() >= deadline:
                logger.warning(
                    "process %s (or a live member of its group) was still there %gs after its SIGKILL: stuck in "
                    "uninterruptible I/O? Giving up the wait; the pipes are closed regardless.",
                    proc.pid, reap_timeout_s,
                )
                break
            await asyncio.sleep(_REAP_POLL_S)
    finally:
        _close_the_pipes(proc)


def _is_gone(proc: asyncio.subprocess.Process) -> bool:
    """``proc`` has exited (its exit recorded) and, on POSIX, no live member is left in the group it led."""
    if proc.returncode is None:
        return False
    if os.name != "posix":
        return True
    return not _group_has_a_live_member(proc.pid)


def _group_has_a_live_member(pgid: int) -> bool:
    try:
        os.killpg(pgid, 0)
    except OSError:
        return False      # ProcessLookupError: empty; PermissionError: a member we cannot signal, nothing more to wait for
    # ``killpg(pgid, 0)`` also succeeds for a ZOMBIE member, so it cannot tell "still dying" from "dead, not reaped".
    live = _live_member_of(pgid)
    return True if live is None else live


def _live_member_of(pgid: int) -> bool | None:
    """Is any process of group ``pgid`` alive, that is, in a state other than zombie (``Z``) or dead (``X``)? None where
    ``/proc`` cannot say (not Linux): the caller then has only ``killpg`` to go on."""
    try:
        names = os.listdir("/proc")
    except OSError:
        return None
    for name in names:
        if not name.isdigit():
            continue
        try:
            with open(f"/proc/{name}/stat", "rb") as stat:
                data = stat.read()
            # "pid (comm) state ppid pgrp ...": comm may hold spaces and parentheses, so split after the LAST ")".
            fields = data[data.rindex(b")") + 2:].split()
            state, group = fields[0], int(fields[2])
        except (OSError, ValueError, IndexError):
            continue      # it exited while we looked, or is not ours to read
        if group == pgid and state not in (b"Z", b"X"):
            return True
    return False


def _close_the_pipes(proc: asyncio.subprocess.Process) -> None:
    """Close the subprocess transport (its pipes). asyncio has no public call for this; ``_transport`` is the one
    attribute every supported CPython keeps, and a failure to close is not worth failing the kill for."""
    transport = getattr(proc, "_transport", None)
    if transport is None:
        return
    try:
        transport.close()
    except Exception:  # noqa: BLE001
        logger.debug("closing the pipes of process %s failed", proc.pid, exc_info=True)


def _kill_the_process_itself(proc: asyncio.subprocess.Process) -> None:
    if proc.returncode is not None:
        return
    try:
        if os.name == "posix":
            os.kill(proc.pid, signal.SIGKILL)     # not proc.kill(): see kill_process_group (its poll reaps the leader)
        else:
            proc.kill()
    except OSError:
        pass              # ProcessLookupError: gone; PermissionError: it changed uid, and the group signal is all we have
