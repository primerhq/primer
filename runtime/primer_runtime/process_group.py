"""Stop a subprocess's whole PROCESS GROUP, not just the process asyncio holds.

The runtime half of ``primer.common.process_group``. The runtime image is built from this directory alone, so it carries its
own copy instead of importing ``primer``; keep the two in step.

An ``exec`` runs ``/bin/sh -c ...``, which forks (dash does for ``sh -c "sleep 30"``), as does any pipeline or backgrounded
job. Signalling only the shell left its children running, holding the stdout/stderr pipes and the Tier-B write lock for as
long as they lived. The command now starts in its own session (:data:`NEW_SESSION`, which makes it the leader of a new
group whose id is its pid) and :func:`stop_process_group` stops that whole group.

The runtime's stop has always been SIGTERM, a grace period, then SIGKILL (a command may clean up on SIGTERM); that is kept,
applied to the group. What it does NOT reach, on purpose: a process that left the group by starting a session of its own
(``setsid``, or a daemon that double-forks and calls ``setsid``) was detached deliberately and survives; ``nohup`` alone does
not detach. Nothing here runs when the command FINISHES: a job it left behind is left alone.
"""

from __future__ import annotations

import asyncio
import logging
import os
import signal

logger = logging.getLogger(__name__)

#: ``create_subprocess_*`` keyword that puts the child in its own session and process group.
NEW_SESSION: dict[str, bool] = {"start_new_session": True}

#: How long a stopped command gets to exit on SIGTERM before the group is SIGKILLed.
TERM_GRACE_S = 5.0

#: How long to wait for the group to be gone after the SIGKILL. It cannot be ignored, so this only bounds a process stuck
#: in uninterruptible I/O.
KILL_WAIT_S = 2.0

_POLL_S = 0.01


async def stop_process_group(proc: asyncio.subprocess.Process, *, grace_s: float | None = None) -> None:
    """SIGTERM the group ``proc`` leads, give it ``grace_s`` to go, SIGKILL what is left, wait for it, close the pipes.

    ``proc`` must have been started with :data:`NEW_SESSION`, so that its pid is its group id. The first signal is sent
    synchronously, and the SIGKILL is sent in a ``finally``, so a caller that is cancelled again while this is waiting
    still delivers it. The wait is on ``proc.returncode`` and the group, NOT ``proc.wait()``: ``wait()`` also waits for the
    pipes to close, and a process that left the group (``setsid``) can hold them open for as long as it lives. The pipes
    are closed here, in a ``finally``, so the caller never inherits them. Returns once the stop has TAKEN EFFECT (the
    caller releases a write lock after it) or the bounds have run out.

    An accepted edge: once the leader is gone and its group empty, its id is no longer reserved, so a signal could in
    theory reach an unrelated group if the pid was recycled in the meantime; it needs a pid wrap inside the exec's timeout.
    """
    grace = TERM_GRACE_S if grace_s is None else grace_s
    try:
        _signal_group(proc, signal.SIGTERM)
        try:
            await _wait_gone(proc, grace)
        finally:
            if not _is_gone(proc):
                _signal_group(proc, signal.SIGKILL)
        if not await _wait_gone(proc, KILL_WAIT_S):
            logger.warning(
                "process %s (or a member of its group) was still there %gs after its SIGKILL: stuck in uninterruptible "
                "I/O? Giving up the wait; the pipes are closed regardless.", proc.pid, KILL_WAIT_S,
            )
    finally:
        _close_the_pipes(proc)


def _signal_group(proc: asyncio.subprocess.Process, sig: int) -> None:
    """Signal the group, and the process itself (a process not started in its own session leads no group)."""
    try:
        os.killpg(proc.pid, sig)
    except OSError:
        pass          # ProcessLookupError: every member is already gone; PermissionError: a member changed uid
    try:
        proc.send_signal(sig)
    except ProcessLookupError:
        pass


def _is_gone(proc: asyncio.subprocess.Process) -> bool:
    """``proc`` has exited (its exit recorded) and nothing is left in the group it led."""
    if proc.returncode is None:
        return False
    try:
        os.killpg(proc.pid, 0)
    except OSError:
        return True   # ProcessLookupError: empty; PermissionError: a member we cannot signal, nothing more to wait for
    return False


async def _wait_gone(proc: asyncio.subprocess.Process, within: float) -> bool:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + within
    while not _is_gone(proc):
        if loop.time() >= deadline:
            return False
        await asyncio.sleep(_POLL_S)
    return True


def _close_the_pipes(proc: asyncio.subprocess.Process) -> None:
    """Close the subprocess transport (its pipes). asyncio has no public call for this; ``_transport`` is the one
    attribute every supported CPython keeps, and a failure to close is not worth failing the stop for."""
    transport = getattr(proc, "_transport", None)
    if transport is None:
        return
    try:
        transport.close()
    except Exception:  # noqa: BLE001
        logger.debug("closing the pipes of process %s failed", proc.pid, exc_info=True)
