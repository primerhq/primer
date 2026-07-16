"""Unit tests for primer/workspace/_locks.py WorkspaceLockTable.

The five shared-method tests are a deliberate byte-for-byte duplicate of
runtime/tests/test_locks.py (the two copies must stay in sync); this file
adds coverage for the local-only ``hold_flock`` cross-process helper.
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from primer.workspace._locks import WorkspaceLockTable


@pytest.mark.asyncio
async def test_same_path_serializes():
    table = WorkspaceLockTable()
    order: list[str] = []

    async def worker(tag: str, hold: float) -> None:
        async with table.hold_path("/workspace/a.txt"):
            order.append(f"{tag}-enter")
            await asyncio.sleep(hold)
            order.append(f"{tag}-exit")

    await asyncio.gather(worker("A", 0.05), worker("B", 0.0))
    # No interleave: one worker fully completes before the other enters.
    assert order in (
        ["A-enter", "A-exit", "B-enter", "B-exit"],
        ["B-enter", "B-exit", "A-enter", "A-exit"],
    )


@pytest.mark.asyncio
async def test_different_paths_run_concurrently():
    table = WorkspaceLockTable()
    started = asyncio.Event()
    order: list[str] = []

    async def a() -> None:
        async with table.hold_path("/workspace/a.txt"):
            started.set()
            await asyncio.sleep(0.1)
            order.append("a")

    async def b() -> None:
        # Should NOT wait on a's lock (different path).
        await asyncio.wait_for(started.wait(), timeout=0.05)
        async with table.hold_path("/workspace/b.txt"):
            order.append("b")

    await asyncio.gather(a(), b())
    # Load-bearing: b must fully acquire AND release its own path lock while
    # a is still holding a's. With a single global lock (or any over-broad
    # key) b would park until a's sleep finished and the order would flip to
    # ["a", "b"]; merely awaiting b to completion would not catch that.
    assert order == ["b", "a"]


@pytest.mark.asyncio
async def test_hold_write_serializes_against_scope():
    table = WorkspaceLockTable()
    order: list[str] = []

    async def tool_write() -> None:
        async with table.hold_write("/workspace/d", "/workspace/d/f.txt"):
            order.append("tool-enter")
            await asyncio.sleep(0.05)
            order.append("tool-exit")

    async def exec_scope() -> None:
        await asyncio.sleep(0.01)
        async with table.hold_scope("/workspace/d"):
            order.append("exec")

    await asyncio.gather(tool_write(), exec_scope())
    assert order == ["tool-enter", "tool-exit", "exec"]


@pytest.mark.asyncio
async def test_hold_paths_sorted_is_deadlock_free():
    table = WorkspaceLockTable()

    async def w1() -> None:
        async with table.hold_paths(["/z", "/a"]):
            await asyncio.sleep(0.02)

    async def w2() -> None:
        async with table.hold_paths(["/a", "/z"]):
            await asyncio.sleep(0.02)

    # If acquisition order were arg-order rather than sorted, these two
    # could deadlock. wait_for guards against a hang.
    await asyncio.wait_for(asyncio.gather(w1(), w2()), timeout=1.0)


@pytest.mark.asyncio
async def test_hold_multi_serializes_against_scope():
    """A move (hold_multi over two dirs) serializes against a same-dir exec."""
    table = WorkspaceLockTable()
    order: list[str] = []

    async def move_writer():
        async with table.hold_multi(
            ["/workspace/a", "/workspace/b"],
            ["/workspace/a/x", "/workspace/b/x"],
        ):
            order.append("move-in")
            await asyncio.sleep(0.05)
            order.append("move-out")

    async def exec_in_b():
        await asyncio.sleep(0.01)
        async with table.hold_scope("/workspace/b"):
            order.append("exec-b")

    await asyncio.wait_for(asyncio.gather(move_writer(), exec_in_b()), timeout=1.0)
    assert order == ["move-in", "move-out", "exec-b"]


@pytest.mark.asyncio
async def test_flock_serializes_cross_table(tmp_path: Path):
    # Two independent tables (simulating two processes) contend on the file lock.
    lock_dir = tmp_path / "locks"
    t1, t2 = WorkspaceLockTable(), WorkspaceLockTable()
    order: list[str] = []

    async def w(t, tag, delay):
        async with t.hold_flock(lock_dir, "same-key"):
            order.append(f"{tag}-in")
            await asyncio.sleep(delay)
            order.append(f"{tag}-out")

    await asyncio.gather(w(t1, "A", 0.05), w(t2, "B", 0.0))
    assert order[0].endswith("-in") and order[1].endswith("-out")


@pytest.mark.asyncio
async def test_flock_degrades_when_unwritable(tmp_path: Path, caplog):
    # Point at a path that cannot be created; must not raise, only WARN.
    bad = tmp_path / "file_not_dir"
    bad.write_text("x")  # a FILE where a lock dir is expected
    t = WorkspaceLockTable()
    async with t.hold_flock(bad, "k"):
        pass
    assert any("flock" in r.message.lower() for r in caplog.records)
