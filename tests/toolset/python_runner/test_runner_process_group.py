"""The local python runner kills the shim's whole PROCESS GROUP on a timeout and on a cancel.

Before, a CANCEL (the hard Cancel, a server shutdown, and the Stop's tool-cancel to come) killed nothing: the ``wait_for``
that enforced the wall clock was gone, so a tool blocked in ``sleep`` (RLIMIT_CPU only stops CPU-bound work) ran on
unbounded. And a timeout killed only the shim, so anything the shim had started outlived it.

The shim now starts in its own session and a timeout or a cancel SIGKILLs that group. As for ``exec``, a process
DELIBERATELY detached into its own session (``setsid``, what a daemon does) is outside the group and survives; nothing is
killed when the shim finishes.

Two kinds of test. The REAL shim covers the cancel (the tool writes its own pid, then sleeps). The hardened shim cannot
start subprocesses where seccomp is available (it denies ``fork`` and ``execve``), so the grandchild and the detach cases
swap the runner's argv for a shell that does fork: what is under test there is the runner's process handling, not the shim.
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

from primer.toolset.python_runner.protocol import build_request
from primer.toolset.python_runner.runners import LocalHardenedRunner

pytestmark = pytest.mark.skipif(
    sys.platform not in ("linux", "darwin") or shutil.which("setsid") is None,
    reason="process groups, ps and setsid are needed",
)

MODULE = (
    "import os, time\n"
    "def sleeper(x: str) -> str:\n"
    "    open(x, 'w').write(str(os.getpid()))\n"
    "    time.sleep(30)\n"
    "    return 'woke'\n"
)


def _req(fn: str, report: Path) -> dict:
    return build_request(
        module=MODULE, fn=fn, phase="call", args={"x": str(report)}, ctx={"tool_call_id": "tc-1"},
        cpu_seconds=5, address_space_bytes=1024 * 1024 * 1024,
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


def _shell_as_the_shim(monkeypatch, script: str) -> None:
    """Run ``script`` where the shim would run: same spawn, same pipes, same kill paths, but a process that forks."""
    monkeypatch.setattr(LocalHardenedRunner, "_argv", lambda self: ["/bin/sh", "-c", script])


async def test_a_cancel_kills_the_shim_and_returns_promptly(tmp_path: Path) -> None:
    """The REAL shim, blocked in a sleep: before, a cancel left it running to the end of its sleep."""
    report = tmp_path / "pid"
    shim = None
    try:
        run = asyncio.create_task(LocalHardenedRunner().run(_req("sleeper", report), timeout_seconds=60.0))
        shim = await _pid(report)

        run.cancel()
        start = time.monotonic()
        with pytest.raises(asyncio.CancelledError):
            await run

        assert time.monotonic() - start < 3.0, "the cancel waited for the tool"
        assert await _gone(shim), "a cancelled tool's shim kept running"
    finally:
        _kill(shim)


async def test_a_timeout_still_kills_the_shim(tmp_path: Path) -> None:
    report = tmp_path / "pid"
    shim = None
    try:
        run = asyncio.create_task(LocalHardenedRunner().run(_req("sleeper", report), timeout_seconds=2.0))
        shim = await _pid(report)

        out = await run

        assert out.ok is False and out.error["type"] == "TimeoutError"
        assert await _gone(shim), "the timed-out shim is still running"
    finally:
        _kill(shim)


async def test_a_timeout_kills_what_the_shim_started_too(tmp_path: Path, monkeypatch) -> None:
    _shell_as_the_shim(monkeypatch, f"sleep 60 & echo $! > {tmp_path}/child; wait")
    child = None
    try:
        out = await LocalHardenedRunner().run(_req("sleeper", tmp_path / "unused"), timeout_seconds=1.5)
        child = await _pid(tmp_path / "child")

        assert out.ok is False and out.error["type"] == "TimeoutError"
        assert await _gone(child), "a process the shim started outlived the timeout"
    finally:
        _kill(child)


async def test_a_cancel_kills_what_the_shim_started_too(tmp_path: Path, monkeypatch) -> None:
    _shell_as_the_shim(monkeypatch, f"sleep 60 & echo $! > {tmp_path}/child; wait")
    child = None
    try:
        run = asyncio.create_task(LocalHardenedRunner().run(_req("sleeper", tmp_path / "unused"), timeout_seconds=60.0))
        child = await _pid(tmp_path / "child")

        run.cancel()
        with pytest.raises(asyncio.CancelledError):
            await run

        assert await _gone(child), "a process a cancelled shim started kept running"
    finally:
        _kill(child)


@pytest.mark.parametrize("how", ["timeout", "cancel"])
async def test_a_process_detached_into_its_own_session_survives_the_kill(tmp_path: Path, monkeypatch, how: str) -> None:
    """A deliberately detached process (setsid) is outside the group; its foreground sibling in the group dies."""
    _shell_as_the_shim(
        monkeypatch,
        f"setsid sleep 60 > /dev/null 2>&1 & echo $! > {tmp_path}/detached; "
        f"sleep 60 & echo $! > {tmp_path}/foreground; wait",
    )
    detached = foreground = None
    try:
        run = asyncio.create_task(
            LocalHardenedRunner().run(_req("sleeper", tmp_path / "unused"), timeout_seconds=1.5 if how == "timeout" else 60.0),
        )
        detached, foreground = await _pid(tmp_path / "detached"), await _pid(tmp_path / "foreground")
        if how == "timeout":
            out = await run
            assert out.ok is False and out.error["type"] == "TimeoutError"
        else:
            run.cancel()
            with pytest.raises(asyncio.CancelledError):
                await run

        assert await _gone(foreground), "the foreground process in the group was not killed"
        await asyncio.sleep(0.3)
        assert _running(detached), "the group kill reached a process that was detached on purpose"
    finally:
        _kill(detached, foreground)


async def test_a_shim_that_finishes_leaves_a_process_it_started_alone(tmp_path: Path, monkeypatch) -> None:
    """Nothing is killed on a normal finish."""
    _shell_as_the_shim(
        monkeypatch,
        f"sleep 60 > /dev/null 2>&1 & echo $! > {tmp_path}/child; echo '{{\"ok\": true, \"value\": \"done\"}}'",
    )
    child = None
    try:
        out = await LocalHardenedRunner().run(_req("sleeper", tmp_path / "unused"), timeout_seconds=30.0)
        child = await _pid(tmp_path / "child")

        assert out.ok is True and out.value == "done"
        await asyncio.sleep(0.3)
        assert _running(child), "a normal finish killed a process the shim started"
    finally:
        _kill(child)
