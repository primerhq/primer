"""``exec_cancel`` end to end: the REAL ``RuntimeClient`` against the REAL in-process runtime server.

The pieces are tested apart (``test_exec_cancel.py`` for the runtime, ``tests/workspace/test_runtime_client_exec_cancel.py``
for the client with a fake socket); this pairs them, so that what a primer-side Stop does to a command in a container is
checked as one thing: the command's process group is stopped and the workspace write lock is free again.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import signal
import subprocess
import sys
import time
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
import pytest_asyncio
from aiohttp.test_utils import TestServer

from primer.workspace.runtime.runtime_client import RuntimeClient

pytestmark = pytest.mark.skipif(
    sys.platform not in ("linux", "darwin") or shutil.which("setsid") is None,
    reason="process groups and ps are needed",
)


def _running(pid: int) -> bool:
    state = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True).stdout.strip()
    return bool(state) and not state.startswith("Z")


async def _gone(pid: int, within: float = 10.0) -> bool:
    deadline = time.monotonic() + within
    while time.monotonic() < deadline:
        if not _running(pid):
            return True
        await asyncio.sleep(0.05)
    return not _running(pid)


async def _pid(path: Path, within: float = 10.0) -> int:
    deadline = time.monotonic() + within
    while time.monotonic() < deadline:
        if path.exists() and path.read_text().strip():
            return int(path.read_text().strip())
        await asyncio.sleep(0.05)
    raise AssertionError(f"{path.name} was never written")


def _kill(pid: int | None) -> None:
    if pid:
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass


@pytest_asyncio.fixture
async def live(tmp_path: Path) -> AsyncIterator[tuple[RuntimeClient, Path]]:
    from primer_runtime.server import build_app

    token = "exec-cancel-live-token"
    root = tmp_path / "workspace"
    root.mkdir()
    test_server = TestServer(build_app(token=token, workspace_root=str(root)))
    await test_server.start_server()
    url = str(test_server.make_url("/")).replace("http://", "ws://")
    client = RuntimeClient(url=url, token=token)
    await client.connect()
    try:
        yield client, root
    finally:
        await client.aclose()
        await test_server.close()


async def test_the_client_reports_a_runtime_that_has_exec_cancel(live) -> None:
    client, _ = live

    assert client.negotiated_version == "1.4"


async def test_cancelling_the_clients_exec_stops_the_command_in_the_runtime_and_frees_the_write_lock(live) -> None:
    client, root = live
    child = None
    task = asyncio.create_task(client.exec(
        ["/bin/sh", "-c", f"sleep 60 & echo $! > {root}/child; wait"], workdir=str(root),
    ))
    try:
        child = await _pid(root / "child")

        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        assert await _gone(child), "a primer-side cancel did not stop the command on the runtime"
        # The lock the first exec held is free: a second write in the same directory completes at once. (It would wait for
        # the first command's lock, i.e. for its timeout, if the cancel had not reached the runtime.)
        second = await asyncio.wait_for(client.exec(["/bin/sh", "-c", "echo ok"], workdir=str(root)), timeout=10.0)
        assert second.exit_code == 0 and "ok" in second.stdout
    finally:
        _kill(child)


async def test_a_runtime_that_reported_an_older_version_is_not_sent_the_op_and_still_stops_when_the_connection_closes(
    live,
) -> None:
    """The fallback for a runtime image that has not been rebuilt: the client does not send ``exec_cancel`` (it would be
    answered EUNSUPPORTED), the command keeps running and keeps its write lock, and closing the connection is what stops it."""
    client, root = live
    client._negotiated_version = "1.3"                  # what an image from before exec_cancel reports in its hello
    child = None
    task = asyncio.create_task(client.exec(
        ["/bin/sh", "-c", f"sleep 60 & echo $! > {root}/child; wait"], workdir=str(root),
    ))
    try:
        child = await _pid(root / "child")

        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await asyncio.sleep(1.0)
        assert _running(child), "the command was stopped although the op was not sent: the test is not in its situation"

        waiter = asyncio.create_task(client.exec(["/bin/sh", "-c", "echo ok"], workdir=str(root)))
        await asyncio.sleep(0.5)
        assert not waiter.done(), "the second exec did not wait for the first one's write lock"

        await client.aclose()                           # the connection closing is what stops it
        assert await _gone(child), "closing the connection did not stop the command"
        await asyncio.gather(waiter, return_exceptions=True)
    finally:
        _kill(child)
