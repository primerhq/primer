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
async def test_terminating_is_skipped() -> None:
    """Workspaces being destroyed (phase=terminating) are not probed."""
    ws_row = _ws("terminating")
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
async def test_pending_to_running_on_first_hit() -> None:
    """A freshly-created workspace at phase=pending must be promoted to
    running on the FIRST successful ping — no streak required.

    This is the recovery path for Bug 3 (workspace.phase never leaves
    pending). The fast path is handled in create_workspace which writes
    phase=running directly; this is the safety net for rows that
    somehow ended up at pending (e.g. legacy rows, manual inserts).
    """
    ws_row = _ws("pending")
    sp, storage = _storage_provider([ws_row])

    registry = MagicMock()
    handle = MagicMock()
    handle.ping = AsyncMock(return_value=True)
    registry.get_workspace = AsyncMock(return_value=handle)

    task = WorkspaceProbeTask(
        storage_provider=sp, registry=registry, interval_seconds=0.01
    )
    await task.tick()

    last_update = _last_update_kwargs(ws_row)
    assert last_update.get("phase") == "running"
    assert last_update.get("failure_reason") is None


@pytest.mark.asyncio
async def test_running_to_failed_reconciles_dependent_sessions() -> None:
    """When a workspace transitions running -> failed, every non-ENDED
    session on it MUST be marked ENDED/workspace_lost.

    This is Bug 6 from the diagnostic report — without this sweep, the
    user is stuck with immortal RUNNING / CREATED / PAUSED rows that
    can never reach ENDED on their own (worker can't re-attach to the
    dead runtime, so no turn-completion CAS ever fires)."""
    from primer.model.workspace_session import (
        AgentSessionBinding,
        SessionStatus,
        WorkspaceSession,
    )
    from datetime import datetime, timezone

    ws_row = _ws("running")

    # Two sessions belong to ws-1: one RUNNING, one already ENDED.
    sess_running = WorkspaceSession(
        id="sess-r1",
        workspace_id="ws-1",
        binding=AgentSessionBinding(agent_id="a1"),
        status=SessionStatus.RUNNING,
        created_at=datetime.now(timezone.utc),
    )
    sess_ended = WorkspaceSession(
        id="sess-e1",
        workspace_id="ws-1",
        binding=AgentSessionBinding(agent_id="a1"),
        status=SessionStatus.ENDED,
        created_at=datetime.now(timezone.utc),
        ended_reason="cancelled",
        ended_at=datetime.now(timezone.utc),
    )

    ws_storage = MagicMock()
    ws_storage.list = AsyncMock(return_value=MagicMock(items=[ws_row]))
    ws_storage.update = AsyncMock()
    sess_storage = MagicMock()
    sess_storage.find = AsyncMock(
        return_value=MagicMock(items=[sess_running, sess_ended])
    )
    sess_storage.update = AsyncMock()

    sp = MagicMock()
    def _get_storage(model_cls):
        if model_cls.__name__ == "Workspace":
            return ws_storage
        if model_cls.__name__ == "WorkspaceSession":
            return sess_storage
        raise AssertionError(f"unexpected model {model_cls!r}")
    sp.get_storage = MagicMock(side_effect=_get_storage)

    registry = MagicMock()
    handle = MagicMock()
    handle.ping = AsyncMock(return_value=False)
    registry.get_workspace = AsyncMock(return_value=handle)

    task = WorkspaceProbeTask(
        storage_provider=sp, registry=registry, interval_seconds=0.01
    )
    # Three consecutive misses to trip the flip.
    for _ in range(3):
        await task.tick()

    # The RUNNING session was reconciled to ENDED/workspace_lost.
    update_calls = sess_storage.update.await_args_list
    assert update_calls, "expected at least one session update"
    reconciled = [c.args[0] for c in update_calls]
    assert any(
        s.id == "sess-r1"
        and s.status == SessionStatus.ENDED
        and s.ended_reason == "workspace_lost"
        for s in reconciled
    ), f"sess-r1 not reconciled: {[(s.id, s.status, s.ended_reason) for s in reconciled]}"
    # The already-ENDED session was left untouched.
    assert all(s.id != "sess-e1" for s in reconciled), (
        "sess-e1 was already ENDED and must not be re-updated"
    )


@pytest.mark.asyncio
async def test_pending_stays_pending_when_ping_fails() -> None:
    """While a pending workspace is unreachable it stays pending — we
    don't escalate to failed because nothing has confirmed it was ever
    healthy."""
    ws_row = _ws("pending")
    sp, storage = _storage_provider([ws_row])

    registry = MagicMock()
    handle = MagicMock()
    handle.ping = AsyncMock(return_value=False)
    registry.get_workspace = AsyncMock(return_value=handle)

    task = WorkspaceProbeTask(
        storage_provider=sp, registry=registry, interval_seconds=0.01
    )
    await task.tick()

    last_update = _last_update_kwargs(ws_row)
    # phase MUST not change away from pending on a miss.
    assert "phase" not in last_update or last_update["phase"] == "pending"


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
