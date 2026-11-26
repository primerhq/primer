"""Tests for HostInotifyProbe.

These tests use real on-disk files under ``tmp_path``.  They rely on
``watchfiles.awatch`` which uses kernel inotify (Linux) or kqueue (macOS)
to detect changes.  A short asyncio.sleep gives the watcher time to register
before the file mutation occurs.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator

import pytest

from primer.bus.host_inotify_probe import HostInotifyProbe
from primer.bus.ws_watch_probe import Change


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


# review 2026-11-23, I2, M-5: the root watch sees the runtime's own state
# tree (.state) git churn, which the container runtime's file events and the
# REST-write wakes never fire on - a "." watch must not either. A spec that
# names the state tree (or a path under it) opts in.


async def _drain_until_quiet(
    watch: AsyncIterator[Change], *, settle_seconds: float = 0.5, timeout: float = 5.0
) -> list[Change]:
    """Delivered changes until no new one arrives within ``settle_seconds``.

    FSEvents directory coalescing delivers the changed directory's own event
    (it matches the spec via ``p == w``) alongside the file's, and which one
    leads is a race, so the .state opt-in tests below accept any delivered
    path under the spec instead of the first event's identity
    (X3 round 1, B-M1).

    Changes are pulled by a feeder task into a queue: waiting on the iterator
    itself with a short timeout CANCELS the pending ``__anext__``, and
    watchfiles treats a cancelled batch wait as "stop the watcher". On Linux
    (inotify sends no initial historical batch, unlike FSEvents) the first
    quiet window would kill the watch and no later event is ever delivered.
    """
    changes: list[Change] = []
    queue: asyncio.Queue[Change | None] = asyncio.Queue()

    async def feed() -> None:
        try:
            async for change in watch:
                queue.put_nowait(change)
        finally:
            queue.put_nowait(None)

    feeder = asyncio.create_task(feed())
    start = time.monotonic()
    last_change_at = start
    try:
        while True:
            if not changes and time.monotonic() - start > timeout:
                raise AssertionError("no change received")  # pragma: no cover
            if changes and time.monotonic() - last_change_at >= settle_seconds:
                break
            try:
                item = await asyncio.wait_for(queue.get(), timeout=0.25)
            except TimeoutError:
                continue
            if item is None:
                break  # the watch iterator ended
            changes.append(item)
            last_change_at = time.monotonic()
    finally:
        feeder.cancel()
    return changes


@pytest.mark.asyncio
async def test_a_root_watch_suppresses_state_tree_churn(tmp_path) -> None:
    probe = HostInotifyProbe(root=str(tmp_path))
    state_file = tmp_path / ".state" / "sessions" / "s1" / "messages.jsonl"
    state_file.parent.mkdir(parents=True)
    state_file.write_text("initial")
    user_file = tmp_path / "out.txt"
    user_file.write_text("initial")

    async def first_change() -> Change:
        async for change in probe.watch(["."]):
            return change
        raise AssertionError("no change received")  # pragma: no cover

    task = asyncio.create_task(first_change())
    # Give watchfiles time to register the inotify watch before we mutate.
    await asyncio.sleep(0.3)
    state_file.write_text("git churn")
    await asyncio.sleep(0.1)
    user_file.write_text("changed")
    change = await asyncio.wait_for(task, timeout=5.0)
    # The state-tree churn is suppressed; the first delivered change is the
    # user file's.
    assert change.path == "out.txt"


@pytest.mark.asyncio
async def test_naming_the_state_tree_still_fires(tmp_path) -> None:
    probe = HostInotifyProbe(root=str(tmp_path))
    state_file = tmp_path / ".state" / "sessions" / "s1" / "messages.jsonl"
    state_file.parent.mkdir(parents=True)
    state_file.write_text("initial")

    task = asyncio.create_task(_drain_until_quiet(probe.watch([".state"])))
    # Give watchfiles time to register the inotify watch before we mutate.
    await asyncio.sleep(0.3)
    state_file.write_text("churn")
    changes = await asyncio.wait_for(task, timeout=5.0)
    # The spec naming the state tree opts in: a change under it is delivered.
    # The directory's own coalesced event may lead (or stand in for) the
    # file's, so any delivered path under the spec counts.
    assert changes
    assert all(c.path == ".state" or c.path.startswith(".state/") for c in changes)


@pytest.mark.asyncio
async def test_naming_a_path_inside_the_state_tree_still_fires(tmp_path) -> None:
    probe = HostInotifyProbe(root=str(tmp_path))
    state_file = tmp_path / ".state" / "sessions" / "s1" / "messages.jsonl"
    state_file.parent.mkdir(parents=True)
    state_file.write_text("initial")

    task = asyncio.create_task(_drain_until_quiet(probe.watch([".state/sessions"])))
    # Give watchfiles time to register the inotify watch before we mutate.
    await asyncio.sleep(0.3)
    state_file.write_text("churn")
    changes = await asyncio.wait_for(task, timeout=5.0)
    # A spec naming a path INSIDE the state tree opts in too, for the same
    # reason: any delivered path under the spec counts.
    assert changes
    assert all(
        c.path == ".state/sessions" or c.path.startswith(".state/sessions/")
        for c in changes
    )
