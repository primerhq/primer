"""Tests for HostInotifyProbe.

These tests use real on-disk files under ``tmp_path``.  They rely on
``watchfiles.awatch`` which uses kernel inotify (Linux) or kqueue (macOS)
to detect changes.  A short asyncio.sleep gives the watcher time to register
before the file mutation occurs.
"""

from __future__ import annotations

import asyncio

import pytest

from matrix.bus.host_inotify_probe import HostInotifyProbe
from matrix.bus.ws_watch_probe import Change


@pytest.mark.asyncio
async def test_host_inotify_probe_pushes_changes(tmp_path) -> None:
    """HostInotifyProbe yields a Change when a watched file is modified."""
    probe = HostInotifyProbe(root=str(tmp_path))
    target = tmp_path / "x.txt"
    target.write_text("initial")

    async def consume() -> Change:
        async for change in probe.watch(["x.txt"]):
            return change
        raise AssertionError("no change received")  # pragma: no cover

    task = asyncio.create_task(consume())
    # Give watchfiles time to register the inotify watch before we mutate.
    await asyncio.sleep(0.1)
    target.write_text("modified")
    change = await asyncio.wait_for(task, timeout=2.0)
    assert change.path == "x.txt"


@pytest.mark.asyncio
async def test_host_inotify_probe_create_event(tmp_path) -> None:
    """HostInotifyProbe yields a 'create' change when a new file appears."""
    probe = HostInotifyProbe(root=str(tmp_path))
    new_file = tmp_path / "new.txt"

    async def consume() -> Change:
        async for change in probe.watch(["new.txt"]):
            return change
        raise AssertionError("no change received")  # pragma: no cover

    task = asyncio.create_task(consume())
    await asyncio.sleep(0.1)
    new_file.write_text("hello")
    change = await asyncio.wait_for(task, timeout=2.0)
    assert change.path == "new.txt"
    assert change.event_type == "create"


@pytest.mark.asyncio
async def test_host_inotify_probe_delete_event(tmp_path) -> None:
    """HostInotifyProbe yields a 'delete' change when a file is removed."""
    probe = HostInotifyProbe(root=str(tmp_path))
    target = tmp_path / "bye.txt"
    target.write_text("content")

    async def consume() -> Change:
        async for change in probe.watch(["bye.txt"]):
            return change
        raise AssertionError("no change received")  # pragma: no cover

    task = asyncio.create_task(consume())
    await asyncio.sleep(0.1)
    target.unlink()
    change = await asyncio.wait_for(task, timeout=2.0)
    assert change.path == "bye.txt"
    assert change.event_type == "delete"


@pytest.mark.asyncio
async def test_host_inotify_probe_ignores_other_files(tmp_path) -> None:
    """HostInotifyProbe does not yield changes for files not in the watch list."""
    probe = HostInotifyProbe(root=str(tmp_path))
    watched = tmp_path / "watched.txt"
    unwatched = tmp_path / "unwatched.txt"
    watched.write_text("initial")
    unwatched.write_text("initial")

    async def consume() -> Change:
        async for change in probe.watch(["watched.txt"]):
            return change
        raise AssertionError("no change received")  # pragma: no cover

    task = asyncio.create_task(consume())
    await asyncio.sleep(0.1)
    # Modify the unwatched file first, then the watched one.
    unwatched.write_text("should be ignored")
    await asyncio.sleep(0.05)
    watched.write_text("should trigger")
    change = await asyncio.wait_for(task, timeout=2.0)
    # We only get the change for the watched file.
    assert change.path == "watched.txt"


@pytest.mark.asyncio
async def test_host_inotify_probe_returns_relative_path(tmp_path) -> None:
    """HostInotifyProbe yields workspace-relative paths, not absolute paths."""
    probe = HostInotifyProbe(root=str(tmp_path))
    target = tmp_path / "relative.txt"
    target.write_text("v1")

    async def consume() -> Change:
        async for change in probe.watch(["relative.txt"]):
            return change
        raise AssertionError("no change received")  # pragma: no cover

    task = asyncio.create_task(consume())
    await asyncio.sleep(0.1)
    target.write_text("v2")
    change = await asyncio.wait_for(task, timeout=2.0)
    # Must be relative, not absolute.
    assert not change.path.startswith("/")
    assert change.path == "relative.txt"
