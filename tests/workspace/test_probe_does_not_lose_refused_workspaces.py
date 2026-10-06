"""A workspace the deployment REFUSES is not a workspace that was LOST (ticket 01a1072f).

The probe counts any ``get_workspace`` exception as a miss, and three misses flip a row to ``failed`` and end every
open session on it ``workspace_lost``, which is permanent. A refused local workspace is intact on disk and becomes
usable again once it moves to a docker or kubernetes provider, so the probe must leave it, and its sessions, alone.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

from primer.model.workspace_refusal import WorkspaceRefusedError
from primer.workspace.probe import WorkspaceProbeTask

REFUSED = WorkspaceRefusedError("provider 'local' refused", provider_id="local", signals=("runtime_mode is 'worker'",))


def _row(id_: str, phase: str = "running") -> MagicMock:
    row = MagicMock()
    row.id = id_
    row.phase = phase
    row.model_copy.side_effect = lambda update: MagicMock(id=id_, phase=update.get("phase", phase))
    return row


def _task(rows: list[MagicMock], registry: MagicMock) -> tuple[WorkspaceProbeTask, MagicMock]:
    storage = MagicMock()
    storage.list = AsyncMock(return_value=MagicMock(items=rows))
    storage.update = AsyncMock()
    sp = MagicMock()
    sp.get_storage = MagicMock(return_value=storage)
    return WorkspaceProbeTask(storage_provider=sp, registry=registry, interval_seconds=0.01), storage


async def test_a_refused_workspace_is_never_failed_and_its_sessions_are_never_lost() -> None:
    row = _row("ws-local")
    registry = MagicMock()
    registry.get_workspace = AsyncMock(side_effect=REFUSED)
    task, storage = _task([row], registry)

    with patch("primer.workspace.probe.reconcile_sessions_to_workspace_lost", new=AsyncMock()) as reconcile:
        for _ in range(10):
            await task.tick()

    reconcile.assert_not_awaited()
    for call in row.model_copy.call_args_list:
        update = call.kwargs.get("update", call.args[0] if call.args else {})
        assert update.get("phase") != "failed"
    storage.update.assert_not_awaited()


async def test_a_refused_workspace_does_not_stop_the_others_being_probed() -> None:
    refused, healthy = _row("ws-local"), _row("ws-docker")
    handle = MagicMock()
    handle.ping = AsyncMock(return_value=True)
    registry = MagicMock()
    registry.get_workspace = AsyncMock(side_effect=lambda wid: handle if wid == "ws-docker" else (_ for _ in ()).throw(REFUSED))
    task, storage = _task([refused, healthy], registry)

    await task.tick()

    assert storage.update.await_count == 1, "only the healthy workspace's row is written"
    assert healthy.model_copy.called and not refused.model_copy.called


async def test_a_refused_workspace_does_not_poison_the_miss_counter_of_a_later_real_failure() -> None:
    """Refusals are not misses, so a workspace that is then really unreachable still needs three of its own."""
    row = _row("ws-1")
    registry = MagicMock()
    registry.get_workspace = AsyncMock(
        side_effect=[REFUSED, REFUSED, REFUSED, RuntimeError("down"), RuntimeError("down")],
    )
    task, _ = _task([row], registry)

    with patch("primer.workspace.probe.reconcile_sessions_to_workspace_lost", new=AsyncMock()) as reconcile:
        for _ in range(5):
            await task.tick()

    reconcile.assert_not_awaited()
    for call in row.model_copy.call_args_list:
        update = call.kwargs.get("update", call.args[0] if call.args else {})
        assert update.get("phase") != "failed", "three refusals plus two misses must not add up to a failure"
