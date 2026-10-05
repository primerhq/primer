"""``kill_process_group``: the whole group, a process that leads none, and a process that is already gone."""

from __future__ import annotations

import asyncio
import errno
import json
import logging
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

import primer.common.process_group as pg
from primer.common.process_group import NEW_SESSION, kill_process_group

_REPO_ROOT = Path(__file__).resolve().parents[2]

pytestmark = pytest.mark.skipif(sys.platform not in ("linux", "darwin"), reason="process groups and ps are needed")


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


async def _child_pid(path: Path) -> int:
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        if path.exists() and path.read_text().strip():
            return int(path.read_text().strip())
        await asyncio.sleep(0.05)
    raise AssertionError("the child never reported its pid")


async def test_it_kills_the_leader_and_everything_the_leader_started(tmp_path: Path) -> None:
    proc = await asyncio.create_subprocess_shell(
        f"sleep 60 & echo $! > {tmp_path}/child; wait", stdout=asyncio.subprocess.PIPE, **NEW_SESSION,
    )
    child = await _child_pid(tmp_path / "child")
    try:
        start = time.monotonic()
        await kill_process_group(proc)

        assert time.monotonic() - start < 3.0
        assert proc.returncode == -signal.SIGKILL
        assert await _gone(child), "a process in the group survived"
    finally:
        try:
            os.kill(child, signal.SIGKILL)
        except ProcessLookupError:
            pass


async def test_a_process_that_was_not_started_in_its_own_session_is_still_killed(tmp_path: Path) -> None:
    """It leads no group, so the group kill finds nothing: the process itself must still die, and promptly."""
    proc = await asyncio.create_subprocess_exec("sleep", "60")
    try:
        start = time.monotonic()
        await kill_process_group(proc)

        assert time.monotonic() - start < 3.0
        assert proc.returncode == -signal.SIGKILL
    finally:
        try:
            os.kill(proc.pid, signal.SIGKILL)     # a failing run must not leave the sleep behind
        except ProcessLookupError:
            pass


async def test_it_returns_only_once_every_member_of_the_group_is_gone(tmp_path: Path) -> None:
    """The caller releases a write lock after this returns: the kill must have TAKEN EFFECT, not just been sent. With the
    group empty, ``killpg(pgid, 0)`` finds nothing."""
    proc = await asyncio.create_subprocess_shell(
        f"sleep 60 & echo $! > {tmp_path}/child; sleep 60 & wait", stdout=asyncio.subprocess.PIPE, **NEW_SESSION,
    )
    child = await _child_pid(tmp_path / "child")
    try:
        await kill_process_group(proc)

        with pytest.raises(ProcessLookupError):
            os.killpg(proc.pid, 0)
        assert not _running(child)
    finally:
        try:
            os.kill(child, signal.SIGKILL)
        except ProcessLookupError:
            pass


async def test_a_detached_process_holding_the_pipes_does_not_hold_the_kill_up(tmp_path: Path) -> None:
    """``proc.wait()`` also waits for the pipes to close, and a process that left the group (setsid) can hold them for as
    long as it lives. The kill waits for the process's own exit and then closes the pipes itself."""
    proc = await asyncio.create_subprocess_shell(
        # the detached process writes its OWN pid after setsid(): ``$!`` is known the moment the shell forks, before the
        # child has exec'd setsid(1) and left the group, and a pid read then is a process the group kill still reaches
        f"setsid sh -c 'echo $$ > {tmp_path}/detached; exec sleep 60' & wait",
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, **NEW_SESSION,
    )
    detached = await _child_pid(tmp_path / "detached")
    try:
        start = time.monotonic()
        await kill_process_group(proc)

        assert time.monotonic() - start < 2.0, "the kill waited on the pipes the detached process holds"
        assert proc.returncode == -signal.SIGKILL
        assert _running(detached), "the detached process was killed"
    finally:
        try:
            os.kill(detached, signal.SIGKILL)
        except ProcessLookupError:
            pass


async def test_a_process_that_is_already_gone_is_not_an_error() -> None:
    proc = await asyncio.create_subprocess_exec("true", **NEW_SESSION)
    await proc.wait()

    await kill_process_group(proc)

    assert proc.returncode == 0


# --- the wait loop's own promises, pinned without real timing --------------------------------------------------------------


def _killpg_reporting_the_group_present(monkeypatch, *, probes_present: int | None) -> list[int]:
    """Make the emptiness probe (``killpg(pgid, 0)``) answer "present" ``probes_present`` times (forever for None) and then
    ProcessLookupError, with the ``/proc`` scan agreeing that a live member is there. Real signals pass through. Returns
    the list the probes are appended to, so a test can say how many times the kill looked before it returned."""
    real = os.killpg
    probes: list[int] = []

    def killpg(pgid: int, sig: int) -> None:
        if sig != 0:
            return real(pgid, sig)
        probes.append(pgid)
        if probes_present is not None and len(probes) > probes_present:
            raise ProcessLookupError(errno.ESRCH, "no such process")
        return None

    monkeypatch.setattr(pg.os, "killpg", killpg)
    monkeypatch.setattr(pg, "_live_member_of", lambda pgid: True)
    return probes


async def test_it_does_not_return_while_the_group_is_still_reported_present(monkeypatch) -> None:
    """The group-empty check is a promise the caller leans on (it releases a write lock after the kill). The real group
    empties too fast for a test to tell the check from the leader's exit, so the probe is scripted: present for five polls,
    then gone. The kill must have looked six times, that is, it waited for the sixth."""
    probes = _killpg_reporting_the_group_present(monkeypatch, probes_present=5)
    proc = await asyncio.create_subprocess_exec("sleep", "60", stdout=asyncio.subprocess.PIPE, **NEW_SESSION)
    try:
        await asyncio.wait_for(kill_process_group(proc), timeout=10.0)

        assert len(probes) == 6, f"returned after {len(probes)} probes: it did not wait for the group to be reported empty"
    finally:
        try:
            os.kill(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass


async def test_a_group_that_never_empties_is_given_up_on_at_the_bound(tmp_path: Path, monkeypatch, caplog) -> None:
    """A member that never leaves the group (a process in uninterruptible I/O) must not turn a timeout or a cancel into a
    hang that holds the write lock forever: the wait ends at ``reap_timeout_s``, warns ONCE, and still closes the pipes.
    The command leaves a detached process holding the pipe, so the transport is still open when the kill returns unless
    the kill closes it."""
    _killpg_reporting_the_group_present(monkeypatch, probes_present=None)
    caplog.set_level(logging.WARNING, logger="primer.common.process_group")
    proc = await asyncio.create_subprocess_shell(
        f"setsid sh -c 'echo $$ > {tmp_path}/detached; exec sleep 60' & wait", stdout=asyncio.subprocess.PIPE, **NEW_SESSION,
    )
    detached = await _child_pid(tmp_path / "detached")
    try:
        start = time.monotonic()
        await asyncio.wait_for(kill_process_group(proc, reap_timeout_s=0.2), timeout=10.0)
        elapsed = time.monotonic() - start

        assert 0.19 <= elapsed < 0.2 + 1.5, f"returned after {elapsed:.2f}s with a bound of 0.2s"
        warned = [r for r in caplog.records if r.name == "primer.common.process_group" and r.levelno == logging.WARNING]
        assert len(warned) == 1
        assert proc._transport.is_closing(), "the pipes were left open"
    finally:
        try:
            os.kill(detached, signal.SIGKILL)
        except ProcessLookupError:
            pass


async def test_a_refused_signal_to_the_leader_does_not_skip_the_wait_or_the_pipe_close(tmp_path: Path, monkeypatch) -> None:
    """A leader that changed uid (it exec'd a setuid program) refuses the direct signal with PermissionError. That must
    not replace the caller's own error, and must not skip the pipe close that runs in the ``finally``."""
    proc = await asyncio.create_subprocess_shell(
        f"setsid sh -c 'echo $$ > {tmp_path}/detached; exec sleep 60' & wait", stdout=asyncio.subprocess.PIPE, **NEW_SESSION,
    )
    detached = await _child_pid(tmp_path / "detached")

    def refuse() -> None:
        raise PermissionError(errno.EPERM, "Operation not permitted")

    real_kill = os.kill

    def refuse_the_leader(pid: int, sig: int) -> None:
        if pid == proc.pid:
            refuse()
        return real_kill(pid, sig)

    # however the helper signals the leader itself, the kernel refuses it (the group kill is untouched and still lands)
    monkeypatch.setattr(proc, "kill", refuse)
    monkeypatch.setattr(pg.os, "kill", refuse_the_leader)
    try:
        await kill_process_group(proc)

        assert proc.returncode == -signal.SIGKILL, "the group kill still took the leader down"
        assert proc._transport.is_closing(), "the pipes were left open"
    finally:
        try:
            os.kill(detached, signal.SIGKILL)
        except ProcessLookupError:
            pass


async def test_a_leader_the_group_kill_already_took_down_is_reported_as_killed_not_as_255(caplog) -> None:
    """``proc.kill()`` polls the child first (``waitpid(WNOHANG)``), so a leader the group kill had already taken down was
    reaped by that poll and asyncio then reported exit code 255 and logged "exit status already read". Here the leader is
    dead before the helper looks (the loop is blocked while it dies), the situation a fast kill is in on a busy host."""
    caplog.set_level(logging.WARNING)
    proc = await asyncio.create_subprocess_exec("sleep", "60", stdout=asyncio.subprocess.PIPE, **NEW_SESSION)
    try:
        os.killpg(proc.pid, signal.SIGKILL)
        time.sleep(0.2)                                   # the leader is dead; the loop has not yet looked at it

        await kill_process_group(proc)
        await asyncio.sleep(0.2)                          # let the child watcher report the exit, if it can

        assert proc.returncode == -signal.SIGKILL
        assert not [r for r in caplog.records if "already read" in r.getMessage()], "the child was reaped behind asyncio's back"
    finally:
        try:
            os.kill(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass


# --- the /proc scan itself, on a real process -------------------------------------------------------------------------------


def _proc_state(pid: int) -> str | None:
    try:
        with open(f"/proc/{pid}/stat", "rb") as stat:
            return stat.read().rsplit(b")", 1)[1].split()[0].decode()
    except (OSError, IndexError):
        return None


@pytest.mark.skipif(sys.platform != "linux", reason="/proc is Linux-only")
async def test_the_live_member_scan_reads_the_real_state_of_a_real_process(tmp_path: Path) -> None:
    """Every other test reaches ``_live_member_of`` through a stub or only on its False side (a zombie is not live). This
    one runs it on a real process in its own group, through its whole life: live while it runs, not live once it is killed
    but not yet reaped (the zombie nothing reaps under PID 1), not live once it is reaped. A second, NON-leader process of
    the group keeps the group live after the leader has died, and only its death empties it. Its ``comm`` is set to
    ``x) Z 1 2 (``, which a parse that splits at the FIRST ")" would read as state Z and the wrong fields: the scan must
    split at the last one. It is a ``Popen`` and not an asyncio process so that nothing reaps it behind the test's back."""
    ready = tmp_path / "ready"
    source = (
        "import ctypes, subprocess, time\n"
        "ctypes.CDLL(None).prctl(15, b'x) Z 1 2 (', 0, 0, 0)\n"        # PR_SET_NAME
        "member = subprocess.Popen(['sleep', '60'])\n"                 # a second process of the leader's group
        f"open({str(ready)!r}, 'w').write(str(member.pid))\n"
        "time.sleep(60)\n"
    )
    child = subprocess.Popen([sys.executable, "-c", source], start_new_session=True)
    member = None
    try:
        deadline = time.monotonic() + 10.0
        while not ready.exists() or not ready.read_text().strip():
            assert time.monotonic() < deadline, "the child never reported ready"
            await asyncio.sleep(0.02)
        member = int(ready.read_text().strip())
        if Path(f"/proc/{child.pid}/comm").read_text().strip() != "x) Z 1 2 (":
            pytest.skip("prctl(PR_SET_NAME) was refused: the test is not in the situation it is about")

        assert pg._live_member_of(child.pid) is True, "a running process of the group was not found"

        # The leader dies and stays a zombie, but a NON-leader member of its group is still alive: the group is not
        # empty (a scan that looked only at the leader would say it is).
        os.kill(child.pid, signal.SIGKILL)
        deadline = time.monotonic() + 5.0
        while _proc_state(child.pid) != "Z":
            assert time.monotonic() < deadline, f"the child never became a zombie (state {_proc_state(child.pid)})"
            time.sleep(0.01)
        assert pg._live_member_of(child.pid) is True, "a live member that is not the leader was not found"

        # Now the member dies too (init or a subreaper reaps it): only the leader's zombie is left, which is not live.
        os.kill(member, signal.SIGKILL)
        deadline = time.monotonic() + 5.0
        while _proc_state(member) not in (None, "Z"):
            assert time.monotonic() < deadline, f"the member never died (state {_proc_state(member)})"
            time.sleep(0.01)
        assert pg._live_member_of(child.pid) is False, "a zombie was counted as a live member"

        child.wait()
        assert pg._live_member_of(child.pid) is False, "a reaped process was counted as a live member"
    finally:
        if child.poll() is None:
            child.kill()
            child.wait()
        if member is not None:
            try:
                os.kill(member, signal.SIGKILL)
            except ProcessLookupError:
                pass


def test_a_scan_that_read_no_stat_file_says_it_cannot_tell(monkeypatch) -> None:
    """None, not False: the caller then has only ``killpg`` to go on (a /proc that lists pids but has no readable
    ``stat``, as a non-Linux procfs, must not make every group look empty)."""
    monkeypatch.setattr(pg.os, "listdir", lambda path: ["4194999"])      # a pid that is not there

    assert pg._live_member_of(12345) is None


# --- primer as PID 1: killed children are never reaped ----------------------------------------------------------------------

#: Runs in a child python. ``PR_SET_CHILD_SUBREAPER`` makes it adopt the orphans of the commands it starts, and, like
#: PID 1 in the shipped image (no init), it never reaps them: every process the group kill takes down stays a ZOMBIE in
#: the group, and ``killpg(pgid, 0)`` finds a zombie "present" for ever.
_AS_PID_ONE = r'''
import asyncio, ctypes, json, logging, os, sys, time

PR_SET_CHILD_SUBREAPER = 36
if ctypes.CDLL(None, use_errno=True).prctl(PR_SET_CHILD_SUBREAPER, 1, 0, 0, 0) != 0:
    print(json.dumps({"subreaper": False}))
    sys.exit(0)

from primer.common.process_group import NEW_SESSION, kill_process_group

warnings = []


class Collect(logging.Handler):
    def emit(self, record):
        if record.levelno >= logging.WARNING:
            warnings.append(record.getMessage())


logging.getLogger("primer.common.process_group").addHandler(Collect())


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


async def main(script, ready):
    proc = await asyncio.create_subprocess_shell(script, stdout=asyncio.subprocess.PIPE, **NEW_SESSION)
    try:
        deadline = time.monotonic() + 10
        while not os.path.exists(ready) or not open(ready).read().strip():
            if time.monotonic() > deadline:
                raise SystemExit("the command never reported ready")
            await asyncio.sleep(0.02)
        start = time.monotonic()
        await kill_process_group(proc)
        elapsed = time.monotonic() - start
        print(json.dumps({"subreaper": True, "elapsed": elapsed, "warnings": warnings, "zombies": zombie_children()}))
    finally:
        if proc.returncode is None:
            os.killpg(proc.pid, 9)


asyncio.run(main(sys.argv[1], sys.argv[2]))
'''


@pytest.mark.skipif(sys.platform != "linux", reason="PR_SET_CHILD_SUBREAPER is Linux-only")
@pytest.mark.parametrize(
    "command",
    [
        "sleep 60 & echo $! > {ready}; wait",
        "sleep 60 | (echo $$ > {ready}; cat)",
    ],
    ids=["a-forked-child", "a-pipeline"],
)
async def test_a_kill_is_not_held_up_by_zombies_nothing_reaps(tmp_path: Path, command: str) -> None:
    """The shipped image runs primer as PID 1 with no init, so the children a group kill takes down are orphaned to a
    process that never reaps them. They are dead; waiting for them to disappear waits out the whole bound on EVERY kill
    (a timeout or cancel took timeout + 2 s and the exec write lock was held 2 s longer) and logs a false 'stuck in
    uninterruptible I/O'. The check must look at a member's state and ignore a zombie."""
    probe = tmp_path / "as_pid_one.py"
    probe.write_text(_AS_PID_ONE)
    ready = tmp_path / "ready"
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(filter(None, [str(_REPO_ROOT), env.get("PYTHONPATH")]))

    done = await asyncio.to_thread(
        subprocess.run, [sys.executable, str(probe), command.format(ready=ready), str(ready)],
        capture_output=True, text=True, timeout=60, env=env,
    )

    assert done.returncode == 0, done.stderr
    out = json.loads(done.stdout.strip().splitlines()[-1])
    if not out["subreaper"]:
        pytest.skip("this environment does not allow PR_SET_CHILD_SUBREAPER")
    assert out["zombies"] >= 1, "nothing was left unreaped: the test is not in the situation it is about"
    assert out["elapsed"] < 1.0, f"the kill took {out['elapsed']:.2f}s: it waited on zombies (the bound is {pg.REAP_TIMEOUT_S}s)"
    assert out["warnings"] == [], out["warnings"]
