"""Tests for the WorkspaceProbeTask phase-transition driver.

The background task pings every ``pending`` / ``running`` / ``failed``
workspace at ~30s intervals: the first hit promotes ``pending`` to
``running``, three consecutive misses fail a ``pending`` or ``running``
one, and three consecutive hits bring a ``failed`` one back to ``running``.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from primer.workspace.probe import WorkspaceProbeTask
from tests._support.reconcile_guard import reconcile_query_must_not_fail  # noqa: F401  (autouse: see the module)


def _ws(phase: str, id_: str = "ws-1") -> MagicMock:
    """Build a minimal mock workspace row: an id and a phase."""
    ws = MagicMock()
    ws.id = id_
    ws.phase = phase
    return ws


def _storage_provider(items: list) -> MagicMock:
    """Build a StorageProvider mock whose Workspace storage lists ``items``."""
    storage = MagicMock()
    storage.list = AsyncMock(return_value=MagicMock(items=items))
    # The same mock serves every model, WorkspaceSession included: the session reconcile awaits find() and reads next_cursor off its page.
    storage.find = AsyncMock(return_value=MagicMock(items=[], next_cursor=None))
    # The probe writes its fields with ONE fenced patch_if (ticket 01a16d72 ruling 3), never a whole-document update: a non-None answer is
    # "the guard matched".
    storage.patch_if = AsyncMock(return_value=MagicMock())
    storage.update = AsyncMock()
    sp = MagicMock()
    sp.get_storage = MagicMock(return_value=storage)
    return sp, storage


def _patches(storage: MagicMock) -> list[dict]:
    """The field patches the probe wrote, oldest first."""
    return [c.args[1] for c in storage.patch_if.await_args_list]


def _last_update_kwargs(storage: MagicMock) -> dict:
    """Return the most recent patch the probe wrote."""
    patches = _patches(storage)
    return patches[-1] if patches else {}


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

    last_update = _last_update_kwargs(storage)
    assert last_update.get("phase") == "failed"
    assert last_update.get("failure_reason") is not None
    assert storage.patch_if.await_count == 3
    storage.update.assert_not_called()
    # Each write is fenced on the phase the verdict was made from: a destroy's terminating mark landing meanwhile is not put back.
    assert {tuple(sorted((k, tuple(v)) for k, v in c.kwargs["where"].items())) for c in storage.patch_if.await_args_list} == {
        (("phase", ("running",)),)
    }


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

    last_update = _last_update_kwargs(storage)
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
    storage.patch_if.assert_not_called()
    storage.update.assert_not_called()


@pytest.mark.asyncio
async def test_pending_to_running_on_first_hit() -> None:
    """A freshly-created workspace at phase=pending must be promoted to
    running on the FIRST successful ping — no streak required.

    Every create door writes pending (ticket 01a16d72 ruling 2), so this
    is how every new workspace becomes running.
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

    last_update = _last_update_kwargs(storage)
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
    ws_storage.patch_if = AsyncMock(return_value=MagicMock())
    sess_storage = MagicMock()
    sess_storage.find = AsyncMock(
        return_value=MagicMock(items=[sess_running, sess_ended], next_cursor=None)
    )
    sess_storage.patch_if = AsyncMock()

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

    # The RUNNING session was reconciled to ENDED/workspace_lost, by a field-scoped patch (01a11d29), never a whole-document update.
    sess_storage.update.assert_not_called()
    patch_calls = sess_storage.patch_if.await_args_list
    assert patch_calls, "expected at least one session patch"
    reconciled = {c.args[0]: c.args[1] for c in patch_calls}
    assert reconciled.get("sess-r1", {}).get("status") == "ended" and reconciled["sess-r1"].get("ended_reason") == "workspace_lost", (
        f"sess-r1 not reconciled: {reconciled}"
    )
    # The already-ENDED session was left untouched.
    assert "sess-e1" not in reconciled, (
        "sess-e1 was already ENDED and must not be re-written"
    )


@pytest.mark.asyncio
async def test_a_pending_workspace_counts_misses_like_a_running_one() -> None:
    """Lead ruling (round 2 of #917): a pending workspace that never answers is failed after three misses, as a running one is, so its
    sessions are ended workspace_lost and the console's failed banner shows. One or two misses leave it pending."""
    ws_row = _ws("pending")
    sp, storage = _storage_provider([ws_row])

    registry = MagicMock()
    handle = MagicMock()
    handle.ping = AsyncMock(return_value=False)
    registry.get_workspace = AsyncMock(return_value=handle)

    task = WorkspaceProbeTask(
        storage_provider=sp, registry=registry, interval_seconds=0.01
    )
    for _ in range(2):
        await task.tick()
    assert all("phase" not in update for update in _patches(storage)), "two misses leave it pending"

    await task.tick()

    last_update = _last_update_kwargs(storage)
    assert last_update.get("phase") == "failed"
    assert last_update.get("failure_reason")
    assert storage.patch_if.await_args.kwargs["where"]["phase"] == ["pending"]


@pytest.mark.asyncio
async def test_a_hit_between_misses_promotes_a_pending_workspace_and_resets_its_misses() -> None:
    ws_row = _ws("pending")
    sp, storage = _storage_provider([ws_row])

    registry = MagicMock()
    handle = MagicMock()
    handle.ping = AsyncMock(side_effect=[False, False, True])
    registry.get_workspace = AsyncMock(return_value=handle)

    task = WorkspaceProbeTask(storage_provider=sp, registry=registry, interval_seconds=0.01)
    for _ in range(3):
        await task.tick()

    assert _last_update_kwargs(storage).get("phase") == "running"
    assert task._miss_counts.get("ws-1", 0) == 0  # noqa: SLF001


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
    for update in _patches(storage):
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

    last_update = _last_update_kwargs(storage)
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


@pytest.mark.asyncio
async def test_start_delay_defaults_to_interval() -> None:
    """Omitting start_delay_seconds falls back to interval_seconds."""
    sp, _storage = _storage_provider([])
    registry = MagicMock()

    task = WorkspaceProbeTask(
        storage_provider=sp, registry=registry, interval_seconds=5.0
    )
    assert task._start_delay == 5.0  # noqa: SLF001


@pytest.mark.asyncio
async def test_start_delay_defers_first_tick() -> None:
    """01a0533c: a freshly-booted probe must not tick until the configured
    start delay elapses — a rolling-deploy replacement pod's probe would
    otherwise start counting its own boot-time settling as real misses."""
    sp, _storage = _storage_provider([])
    registry = MagicMock()

    task = WorkspaceProbeTask(
        storage_provider=sp,
        registry=registry,
        interval_seconds=0.05,
        start_delay_seconds=0.05,
    )
    task.tick = AsyncMock()  # type: ignore[method-assign]

    runner = asyncio.create_task(task.start())
    await asyncio.sleep(0.02)
    assert task.tick.await_count == 0, "ticked before the start delay elapsed"

    await asyncio.sleep(0.06)
    assert task.tick.await_count >= 1, "never ticked after the start delay"

    task.stop()
    await asyncio.wait_for(runner, timeout=1.0)


@pytest.mark.asyncio
async def test_stop_during_start_delay_exits_promptly() -> None:
    """stop() called mid-delay must cut the wait short — no waiting out
    the full delay just to discover the loop should never have ticked."""
    sp, _storage = _storage_provider([])
    registry = MagicMock()

    task = WorkspaceProbeTask(
        storage_provider=sp,
        registry=registry,
        interval_seconds=10.0,
        start_delay_seconds=10.0,
    )
    task.tick = AsyncMock()  # type: ignore[method-assign]

    runner = asyncio.create_task(task.start())
    await asyncio.sleep(0.02)
    task.stop()
    await asyncio.wait_for(runner, timeout=1.0)  # must not wait out the 10s delay

    task.tick.assert_not_awaited()
