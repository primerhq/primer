"""``kill_process_group``: the whole group, a process that leads none, and a process that is already gone."""

from __future__ import annotations

import asyncio
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from primer.common.process_group import NEW_SESSION, kill_process_group

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

    start = time.monotonic()
    await kill_process_group(proc)

    assert time.monotonic() - start < 3.0
    assert proc.returncode == -signal.SIGKILL


async def test_a_process_that_is_already_gone_is_not_an_error() -> None:
    proc = await asyncio.create_subprocess_exec("true", **NEW_SESSION)
    await proc.wait()

    await kill_process_group(proc)

    assert proc.returncode == 0
