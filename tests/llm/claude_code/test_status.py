"""runtime_status: the state of the claude CLI on the host (the claude_code plan, task 2).

Only the four facts a worker can check locally are produced: installed, version,
logged in and meets-minimum. The CLI's auth output carries account details
(email, org); those are dropped at the source and must never appear here.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import time

import pytest

from primer.llm.claude_code.status import runtime_status

from tests.llm.claude_code.fake_claude import fake_claude  # noqa: F401  (fixture)


async def test_status_reads_version_and_login(fake_claude):
    s = await runtime_status(fake_claude, env={"FAKE_CLAUDE_LOGGED_IN": "1"})
    assert (s.installed, s.version, s.logged_in, s.meets_minimum) == (True, "2.1.322", True, True)


async def test_status_missing_binary():
    s = await runtime_status("/nonexistent/claude", env={})
    assert s.installed is False and s.logged_in is False


async def test_status_old_version(fake_claude):
    s = await runtime_status(fake_claude, env={"FAKE_CLAUDE_VERSION": "2.1.300", "FAKE_CLAUDE_LOGGED_IN": "1"})
    assert s.meets_minimum is False


async def test_status_logged_out(fake_claude):
    s = await runtime_status(fake_claude, env={"FAKE_CLAUDE_LOGGED_IN": "0"})
    assert (s.installed, s.logged_in, s.meets_minimum) == (True, False, True)


async def test_a_cancelled_status_check_kills_the_child(tmp_path):
    """A cancelled request (a client disconnect while communicate() is awaited) must not leave the child running:
    the process group is killed before the cancellation goes on."""
    marker = tmp_path / "pid"
    script = tmp_path / "hang.sh"
    script.write_text(f"#!/bin/sh\necho $$ > {marker}\nsleep 30\n", encoding="utf-8")
    script.chmod(0o755)
    task = asyncio.create_task(runtime_status(str(script), env={}))
    # Poll for the marker BEFORE cancelling: a fixed sleep can land before the
    # child starts (a cancel-never-saw-a-child test is not a cancellation test).
    for _ in range(100):
        if marker.exists():
            break
        await asyncio.sleep(0.02)
    assert marker.exists(), "the child never started within 2 s"
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task
    pid = int(marker.read_text(encoding="utf-8").strip())
    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return  # the child is gone
        await asyncio.sleep(0.05)
    pytest.fail("the child is still alive 2 s after the cancel")
