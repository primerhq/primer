"""Tests for FakeSandbox itself.

Validates that the test seam behaves like a real sandbox to the
contract the :class:`Sandbox` ABC promises.
"""
from __future__ import annotations

import asyncio
import io
import tarfile
from pathlib import Path

import pytest

from matrix.workspace.sandbox.fake import FakeSandbox


@pytest.mark.asyncio
async def test_exec_echo(tmp_path: Path) -> None:
    sb = FakeSandbox(root=tmp_path)
    res = await sb.exec("echo hi")
    assert res.exit_code == 0
    assert res.stdout.strip() == "hi"
    assert res.stderr == ""


@pytest.mark.asyncio
async def test_write_and_read(tmp_path: Path) -> None:
    sb = FakeSandbox(root=tmp_path)
    await sb.write_file("/workspace/hello.txt", b"world")
    body = await sb.read_file("/workspace/hello.txt")
    assert body == b"world"


@pytest.mark.asyncio
async def test_list_dir(tmp_path: Path) -> None:
    sb = FakeSandbox(root=tmp_path)
    await sb.write_file("/workspace/a.txt", b"1")
    await sb.write_file("/workspace/b.txt", b"22")
    entries = await sb.list_dir("/workspace")
    names = sorted(e.path for e in entries)
    assert names == ["a.txt", "b.txt"]


@pytest.mark.asyncio
async def test_stat_missing_returns_none(tmp_path: Path) -> None:
    sb = FakeSandbox(root=tmp_path)
    assert await sb.stat("/workspace/missing") is None


@pytest.mark.asyncio
async def test_stat_file(tmp_path: Path) -> None:
    sb = FakeSandbox(root=tmp_path)
    await sb.write_file("/workspace/x.txt", b"hello")
    info = await sb.stat("/workspace/x.txt")
    assert info is not None
    assert info.kind == "file"
    assert info.size_bytes == 5


@pytest.mark.asyncio
async def test_delete(tmp_path: Path) -> None:
    sb = FakeSandbox(root=tmp_path)
    await sb.write_file("/workspace/x.txt", b"hi")
    await sb.delete("/workspace/x.txt")
    assert await sb.stat("/workspace/x.txt") is None


import sys

_SLEEP_5_ARGV = [sys.executable, "-c", "import time; time.sleep(5)"]


@pytest.mark.asyncio
async def test_exec_timeout(tmp_path: Path) -> None:
    sb = FakeSandbox(root=tmp_path)
    with pytest.raises(TimeoutError):
        await sb.exec(_SLEEP_5_ARGV, timeout_seconds=0.2)


@pytest.mark.asyncio
async def test_exec_abort(tmp_path: Path) -> None:
    sb = FakeSandbox(root=tmp_path)
    abort = asyncio.Event()

    async def trigger() -> None:
        await asyncio.sleep(0.2)
        abort.set()

    asyncio.create_task(trigger())
    res = await sb.exec(_SLEEP_5_ARGV, abort=abort, timeout_seconds=5.0)
    assert res.exit_code != 0  # killed


@pytest.mark.asyncio
async def test_archive_streams_tar(tmp_path: Path) -> None:
    sb = FakeSandbox(root=tmp_path)
    await sb.write_file("/workspace/x.txt", b"hi")
    chunks: list[bytes] = []
    async for c in sb.archive(["/workspace"]):
        chunks.append(c)
    buf = io.BytesIO(b"".join(chunks))
    with tarfile.open(fileobj=buf, mode="r") as tf:
        names = tf.getnames()
    # The archive includes the directory entry plus the file inside.
    assert any(n.endswith("x.txt") for n in names)


@pytest.mark.asyncio
async def test_inspect_running(tmp_path: Path) -> None:
    sb = FakeSandbox(root=tmp_path)
    info = await sb.inspect()
    assert info.state == "running"


@pytest.mark.asyncio
async def test_stop_then_exec_fails(tmp_path: Path) -> None:
    sb = FakeSandbox(root=tmp_path)
    await sb.stop()
    with pytest.raises(RuntimeError):
        await sb.exec("echo hi")


@pytest.mark.asyncio
async def test_stop_then_inspect_says_stopped(tmp_path: Path) -> None:
    sb = FakeSandbox(root=tmp_path)
    await sb.stop()
    info = await sb.inspect()
    assert info.state == "stopped"


@pytest.mark.asyncio
async def test_write_with_mode(tmp_path: Path) -> None:
    sb = FakeSandbox(root=tmp_path)
    await sb.write_file("/workspace/x.sh", b"echo hi", mode=0o755)
    info = await sb.stat("/workspace/x.sh")
    assert info is not None
    # Mode setting is best-effort; just confirm we don't crash.
