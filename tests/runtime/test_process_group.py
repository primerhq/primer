"""``primer_runtime.process_group.stop_process_group``: what the exec-level tests cannot pin without real timing.

The group stop is SIGTERM, a grace period, SIGKILL, and a bounded wait for the leader to have exited and the group to hold
no live member. The real group empties too fast for a test to tell the wait from the leader's exit, so the group probe
(``killpg(pgid, 0)``) is scripted here. Real signals pass through.
"""

from __future__ import annotations

import asyncio
import errno
import logging
import os
import signal
import sys
import time
from pathlib import Path

import pytest

import primer_runtime.process_group as pg
from primer_runtime.process_group import NEW_SESSION, stop_process_group

pytestmark = pytest.mark.skipif(sys.platform not in ("linux", "darwin"), reason="process groups are needed")


async def _child_pid(path: Path) -> int:
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        if path.exists() and path.read_text().strip():
            return int(path.read_text().strip())
        await asyncio.sleep(0.05)
    raise AssertionError("the child never reported its pid")


def _kill(pid: int | None) -> None:
    if pid:
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass


def _script_the_probe(monkeypatch, *, probes_present: int | None) -> tuple[list[int], list[int]]:
    """The emptiness probe (signal 0) answers "present" ``probes_present`` times (for ever for None), then
    ProcessLookupError, and the ``/proc`` scan agrees a live member is there. Returns (probes, real signals sent)."""
    real = os.killpg
    probes: list[int] = []
    sent: list[int] = []

    def killpg(pgid: int, sig: int) -> None:
        if sig != 0:
            sent.append(sig)
            return real(pgid, sig)
        probes.append(pgid)
        if probes_present is not None and len(probes) > probes_present:
            raise ProcessLookupError(errno.ESRCH, "no such process")
        return None

    monkeypatch.setattr(pg.os, "killpg", killpg)
    monkeypatch.setattr(pg, "_live_member_of", lambda pgid: True)
    return probes, sent


async def test_it_does_not_return_while_the_group_is_still_reported_present(monkeypatch) -> None:
    """Present for five polls, then gone: the stop must have looked at least six times, that is, it waited for the sixth
    (it may look again afterwards), and the command that went down on SIGTERM is not SIGKILLed on top of it."""
    probes, sent = _script_the_probe(monkeypatch, probes_present=5)
    proc = await asyncio.create_subprocess_exec("sleep", "60", stdout=asyncio.subprocess.PIPE, **NEW_SESSION)
    try:
        await asyncio.wait_for(stop_process_group(proc), timeout=10.0)

        assert len(probes) >= 6, f"returned after {len(probes)} probes: it did not wait for the group to be reported empty"
        assert sent == [signal.SIGTERM], f"signals sent: {sent}"
    finally:
        _kill(proc.pid)


async def test_a_group_that_never_empties_is_killed_after_the_grace_and_given_up_on_at_the_bound(
    tmp_path: Path, monkeypatch, caplog,
) -> None:
    """A member that never leaves the group (uninterruptible I/O) must not turn a timeout or a cancel into a hang that
    holds the write lock for ever: SIGTERM, the grace, SIGKILL, a bounded wait, ONE warning, and the pipes closed. A detached
    process holds the pipe, so the transport is still open when the stop returns unless the stop closes it."""
    monkeypatch.setattr(pg, "TERM_GRACE_S", 0.2)
    monkeypatch.setattr(pg, "KILL_WAIT_S", 0.2)
    _, sent = _script_the_probe(monkeypatch, probes_present=None)
    caplog.set_level(logging.WARNING, logger="primer_runtime.process_group")
    proc = await asyncio.create_subprocess_shell(
        f"setsid sh -c 'echo $$ > {tmp_path}/detached; exec sleep 60' & wait", stdout=asyncio.subprocess.PIPE, **NEW_SESSION,
    )
    detached = await _child_pid(tmp_path / "detached")
    try:
        start = time.monotonic()
        await asyncio.wait_for(stop_process_group(proc), timeout=10.0)
        elapsed = time.monotonic() - start

        assert 0.39 <= elapsed < 0.4 + 1.5, f"returned after {elapsed:.2f}s with a grace of 0.2s and a bound of 0.2s"
        assert sent == [signal.SIGTERM, signal.SIGKILL], f"signals sent: {sent}"
        warned = [r for r in caplog.records if r.name == "primer_runtime.process_group" and r.levelno == logging.WARNING]
        assert len(warned) == 1
        assert proc._transport.is_closing(), "the pipes were left open"
    finally:
        _kill(detached)


async def test_a_process_that_leads_no_group_is_still_signalled_directly(monkeypatch) -> None:
    """Not started in its own session, it leads no group: the group signal finds nothing and the process itself must still
    be stopped (the mechanism for a direct signal changed, this is the control for it)."""
    monkeypatch.setattr(pg, "TERM_GRACE_S", 2.0)
    proc = await asyncio.create_subprocess_exec("sleep", "60")
    try:
        start = time.monotonic()
        await asyncio.wait_for(stop_process_group(proc), timeout=10.0)

        assert time.monotonic() - start < 1.5
        assert proc.returncode == -signal.SIGTERM
    finally:
        _kill(proc.pid)


async def test_a_refused_direct_signal_does_not_raise_and_the_pipes_are_closed(monkeypatch) -> None:
    """A process that leads no group is signalled directly; if the kernel refuses (it changed uid) that must not replace the
    caller's own error nor skip the wait and the pipe close."""
    monkeypatch.setattr(pg, "TERM_GRACE_S", 0.2)
    monkeypatch.setattr(pg, "KILL_WAIT_S", 0.2)
    proc = await asyncio.create_subprocess_exec("sleep", "60", stdout=asyncio.subprocess.PIPE)
    real_kill = os.kill

    def refuse() -> None:
        raise PermissionError(errno.EPERM, "Operation not permitted")

    def refuse_the_leader(pid: int, sig: int) -> None:
        if pid == proc.pid:
            refuse()
        return real_kill(pid, sig)

    # however the leader is signalled directly, the kernel refuses it
    monkeypatch.setattr(proc, "send_signal", lambda sig: refuse())
    monkeypatch.setattr(pg.os, "kill", refuse_the_leader)
    try:
        await asyncio.wait_for(stop_process_group(proc), timeout=10.0)

        assert proc._transport.is_closing(), "the pipes were left open"
    finally:
        monkeypatch.undo()
        _kill(proc.pid)


async def test_a_cancel_in_the_kill_wait_does_not_wait_it_out(tmp_path: Path, monkeypatch) -> None:
    """The early release is not limited to the SIGTERM grace: a cancel that lands in the KILL wait (the SIGKILL has already
    gone out, the stop is waiting to see the group gone) propagates at once, with the pipes closed, instead of waiting the
    rest of ``KILL_WAIT_S``. A member that never goes (scripted) puts the stop in that wait."""
    monkeypatch.setattr(pg, "TERM_GRACE_S", 0.1)
    monkeypatch.setattr(pg, "KILL_WAIT_S", 3.0)
    _, sent = _script_the_probe(monkeypatch, probes_present=None)
    proc = await asyncio.create_subprocess_shell(
        f"setsid sh -c 'echo $$ > {tmp_path}/detached; exec sleep 60' & wait", stdout=asyncio.subprocess.PIPE, **NEW_SESSION,
    )
    detached = await _child_pid(tmp_path / "detached")
    stop = asyncio.create_task(stop_process_group(proc))
    try:
        await asyncio.sleep(0.5)                                  # past the 0.1 s grace: the SIGKILL is out, the kill wait runs
        assert not stop.done() and sent == [signal.SIGTERM, signal.SIGKILL], f"not in the kill wait (signals: {sent})"

        stop.cancel()
        start = time.monotonic()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(stop, timeout=2.0)

        assert time.monotonic() - start < 0.5, "the cancel waited out the kill wait"
        assert proc._transport.is_closing(), "the pipes were left open"
    finally:
        _kill(detached)


def test_a_scan_that_read_no_stat_file_says_it_cannot_tell(monkeypatch) -> None:
    """None, not False: the caller then has only ``killpg`` to go on (a /proc that lists pids but has no readable ``stat``,
    as a non-Linux procfs, must not make every group look empty). The same behaviour as the local copy's."""
    monkeypatch.setattr(pg.os, "listdir", lambda path: ["4194999"])      # a pid that is not there

    assert pg._live_member_of(12345) is None


def test_the_grace_and_the_kill_wait_are_the_documented_five_and_two_seconds() -> None:
    """The dev doc states the worst-case write-lock hold as ``TERM_GRACE_S`` + ``KILL_WAIT_S`` (5 s + 2 s = 7 s) and the
    agent doc promises a command 5 seconds to clean up on SIGTERM. The behavioural test (a trap that needs a second) kills a
    grace that is too SHORT; this pins the numbers themselves, so changing either needs the docs changed with it."""
    assert pg.TERM_GRACE_S == 5.0
    assert pg.KILL_WAIT_S == 2.0
