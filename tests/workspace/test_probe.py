"""Tests for the WorkspaceProbeTask phase-transition driver.

Phase 7 background task pings every ``running`` / ``failed`` workspace at
~30s intervals and flips ``phase`` after three consecutive misses (running
-> failed) or three consecutive hits (failed -> running).
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from primer.workspace.probe import WorkspaceProbeTask


def _ws(phase: str, id_: str = "ws-1") -> MagicMock:
    """Build a minimal mock workspace row with phase + model_copy + id."""
    ws = MagicMock()
    ws.id = id_
    ws.phase = phase

    def _copy(update: dict) -> MagicMock:
        new = MagicMock()
        new.id = id_
        new.phase = update.get("phase", phase)
        return new

    ws.model_copy.side_effect = _copy
    return ws


def _storage_provider(items: list) -> MagicMock:
    """Build a StorageProvider mock whose Workspace storage lists ``items``."""
    storage = MagicMock()
    storage.list = AsyncMock(return_value=MagicMock(items=items))
    storage.update = AsyncMock()
    sp = MagicMock()
    sp.get_storage = MagicMock(return_value=storage)
    return sp, storage


def _last_update_kwargs(ws_row: MagicMock) -> dict:
    """Return the most recent ``model_copy(update=...)`` keyword dict."""
    call = ws_row.model_copy.call_args
    if call.kwargs.get("update") is not None:
        return call.kwargs["update"]
    return call.args[0] if call.args else {}


@pytest.mark.asyncio
async def test_running_to_failed_after_three_misses() -> None:
    """Three consecutive ping=False flips phase: running -> failed."""
    ws_row = _ws("running")
    sp, storage = _storage_provider([ws_row])

    registry = MagicMock()
    handle = MagicMock()
    handle.ping = AsyncMock(return_value=False)
    registry.get_workspace = AsyncMock(return_value=handle)

    task = WorkspaceProbeTask(
        storage_provider=sp, registry=registry, interval_seconds=0.01
    )
    for _ in range(3):
        await task.tick()

    last_update = _last_update_kwargs(ws_row)
    assert last_update.get("phase") == "failed"
    assert last_update.get("failure_reason") is not None
    assert storage.update.await_count == 3


@pytest.mark.asyncio
async def test_failed_to_running_after_three_hits() -> None:
    """Three consecutive ping=True while failed flips phase back to running."""
    ws_row = _ws("failed")
    sp, storage = _storage_provider([ws_row])

    registry = MagicMock()
    handle = MagicMock()
    handle.ping = AsyncMock(return_value=True)
    registry.get_workspace = AsyncMock(return_value=handle)

    task = WorkspaceProbeTask(
        storage_provider=sp, registry=registry, interval_seconds=0.01
    )
    for _ in range(3):
        await task.tick()

    last_update = _last_update_kwargs(ws_row)
    assert last_update.get("phase") == "running"
    assert last_update.get("failure_reason") is None


@pytest.mark.asyncio
async def test_running_skipped_when_pending() -> None:
    """Workspaces in pending/terminating phase are not probed."""
    ws_row = _ws("pending")
    sp, storage = _storage_provider([ws_row])

    registry = MagicMock()
    registry.get_workspace = AsyncMock()

    task = WorkspaceProbeTask(
        storage_provider=sp, registry=registry, interval_seconds=0.01
    )
    await task.tick()

    registry.get_workspace.assert_not_called()
    storage.update.assert_not_called()


@pytest.mark.asyncio
async def test_two_misses_then_recover() -> None:
    """Two misses then a hit resets the miss counter (no phase flip)."""
    ws_row = _ws("running")
    sp, storage = _storage_provider([ws_row])

    registry = MagicMock()
    handle = MagicMock()
    handle.ping = AsyncMock(side_effect=[False, False, True, False])
    registry.get_workspace = AsyncMock(return_value=handle)

    task = WorkspaceProbeTask(
        storage_provider=sp, registry=registry, interval_seconds=0.01
    )
    for _ in range(4):
        await task.tick()

    # After miss, miss, hit, miss -> miss_count should be 1, not 3.
    # Phase update never set to "failed" because no 3-in-a-row miss streak.
    for call in ws_row.model_copy.call_args_list:
        update = call.kwargs.get("update", call.args[0] if call.args else {})
        assert update.get("phase") != "failed"


@pytest.mark.asyncio
async def test_registry_failure_counts_as_miss() -> None:
    """If the registry can't return a handle, that counts as a miss."""
    ws_row = _ws("running")
    sp, storage = _storage_provider([ws_row])

    registry = MagicMock()
    registry.get_workspace = AsyncMock(side_effect=RuntimeError("backend gone"))

    task = WorkspaceProbeTask(
        storage_provider=sp, registry=registry, interval_seconds=0.01
    )
    for _ in range(3):
        await task.tick()

    last_update = _last_update_kwargs(ws_row)
    assert last_update.get("phase") == "failed"
    # The failure_reason should preserve the exception type/message.
    assert "backend gone" in (last_update.get("failure_reason") or "")


@pytest.mark.asyncio
async def test_stop_breaks_loop() -> None:
    """Calling stop() before start() makes the loop exit immediately."""
    sp, _storage = _storage_provider([])
    registry = MagicMock()

    task = WorkspaceProbeTask(
        storage_provider=sp, registry=registry, interval_seconds=0.01
    )
    task.stop()
    await task.start()  # must return promptly
