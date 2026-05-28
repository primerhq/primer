"""Tests for WSWatchProbe.

Uses a minimal fake RuntimeClient that lets the test inject ChangeEvents
and verifies that WSWatchProbe correctly translates them into workspace-
relative Change objects.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

import pytest

from matrix.bus.ws_watch_probe import Change, WSWatchProbe
from matrix.workspace.runtime.runtime_client import ChangeEvent


# ---------------------------------------------------------------------------
# Fake RuntimeClient
# ---------------------------------------------------------------------------


class FakeRuntimeClient:
    """Minimal RuntimeClient stand-in with an injectable ChangeEvent stream.

    Call :meth:`inject_change` to enqueue a change that will be yielded by
    :meth:`watch`.  After all injected changes have been consumed, the
    iterator blocks until more events arrive or the caller closes the iterator.
    """

    def __init__(self) -> None:
        self._queue: asyncio.Queue[ChangeEvent | None] = asyncio.Queue()

    def inject_change(
        self,
        *,
        path: str,
        event: str = "modify",
        mtime: float | None = None,
        size: int | None = None,
    ) -> None:
        """Enqueue a :class:`ChangeEvent` to be yielded by :meth:`watch`."""
        self._queue.put_nowait(ChangeEvent(path=path, event=event, mtime=mtime, size=size))

    def close(self) -> None:
        """Signal end-of-stream to any active :meth:`watch` iterator."""
        self._queue.put_nowait(None)

    async def watch(
        self, paths: list[str], events: list[str]
    ) -> AsyncIterator[ChangeEvent]:
        """Async-generator that yields injected ChangeEvents."""
        while True:
            item = await self._queue.get()
            if item is None:
                return
            yield item


# ---------------------------------------------------------------------------
# Fixture
# ---------------------------------------------------------------------------


@pytest.fixture
def fake_runtime_client() -> FakeRuntimeClient:
    return FakeRuntimeClient()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ws_watch_probe_pushes_changes(fake_runtime_client: FakeRuntimeClient) -> None:
    """WSWatchProbe yields a Change with workspace-relative path."""
    probe = WSWatchProbe(
        runtime_client=fake_runtime_client, workspace_root="/workspace"
    )
    fake_runtime_client.inject_change(path="/workspace/a", mtime=1.0, size=10)
    async for change in probe.watch(["a"]):
        assert change.path == "a"
        break


@pytest.mark.asyncio
async def test_ws_watch_probe_strips_workspace_root(fake_runtime_client: FakeRuntimeClient) -> None:
    """WSWatchProbe strips workspace root from absolute paths."""
    probe = WSWatchProbe(
        runtime_client=fake_runtime_client, workspace_root="/workspace"
    )
    fake_runtime_client.inject_change(path="/workspace/src/main.py")
    async for change in probe.watch(["src/main.py"]):
        assert change.path == "src/main.py"
        break


@pytest.mark.asyncio
async def test_ws_watch_probe_preserves_event_type(fake_runtime_client: FakeRuntimeClient) -> None:
    """WSWatchProbe preserves the event type from ChangeEvent."""
    probe = WSWatchProbe(
        runtime_client=fake_runtime_client, workspace_root="/workspace"
    )
    fake_runtime_client.inject_change(path="/workspace/file.txt", event="delete")
    async for change in probe.watch(["file.txt"]):
        assert change.event_type == "delete"
        break


@pytest.mark.asyncio
async def test_ws_watch_probe_preserves_mtime_and_size(
    fake_runtime_client: FakeRuntimeClient,
) -> None:
    """WSWatchProbe passes mtime and size through from ChangeEvent."""
    probe = WSWatchProbe(
        runtime_client=fake_runtime_client, workspace_root="/workspace"
    )
    fake_runtime_client.inject_change(path="/workspace/x.txt", mtime=42.5, size=99)
    async for change in probe.watch(["x.txt"]):
        assert change.mtime == 42.5
        assert change.size == 99
        break


@pytest.mark.asyncio
async def test_ws_watch_probe_multiple_events(fake_runtime_client: FakeRuntimeClient) -> None:
    """WSWatchProbe yields multiple sequential change events."""
    probe = WSWatchProbe(
        runtime_client=fake_runtime_client, workspace_root="/workspace"
    )
    fake_runtime_client.inject_change(path="/workspace/a.txt", event="modify")
    fake_runtime_client.inject_change(path="/workspace/b.txt", event="create")
    fake_runtime_client.close()

    changes: list[Change] = []
    async for change in probe.watch(["a.txt", "b.txt"]):
        changes.append(change)

    assert len(changes) == 2
    assert changes[0].path == "a.txt"
    assert changes[0].event_type == "modify"
    assert changes[1].path == "b.txt"
    assert changes[1].event_type == "create"


@pytest.mark.asyncio
async def test_ws_watch_probe_path_without_root_prefix(
    fake_runtime_client: FakeRuntimeClient,
) -> None:
    """Paths not under workspace_root are returned as-is."""
    probe = WSWatchProbe(
        runtime_client=fake_runtime_client, workspace_root="/workspace"
    )
    # Path from runtime that doesn't share the workspace root prefix
    fake_runtime_client.inject_change(path="/other/path.txt")
    async for change in probe.watch(["/other/path.txt"]):
        # Path is returned as-is since it doesn't start with workspace_root
        assert change.path == "/other/path.txt"
        break


@pytest.mark.asyncio
async def test_ws_watch_probe_builds_absolute_paths(
    fake_runtime_client: FakeRuntimeClient,
) -> None:
    """WSWatchProbe converts relative paths to absolute before calling watch."""
    recorded_paths: list[list[str]] = []
    original_watch = fake_runtime_client.watch

    async def recording_watch(
        paths: list[str], events: list[str]
    ) -> AsyncIterator[ChangeEvent]:
        recorded_paths.append(paths)
        # Return nothing — just check the paths that were passed
        return
        yield  # make it an async generator

    fake_runtime_client.watch = recording_watch  # type: ignore[method-assign]

    probe = WSWatchProbe(
        runtime_client=fake_runtime_client, workspace_root="/workspace"
    )

    # Consume briefly (no events, returns immediately after override)
    async for _ in probe.watch(["src/main.py", "data/"]):
        break  # pragma: no cover

    assert recorded_paths == [["/workspace/src/main.py", "/workspace/data/"]]
