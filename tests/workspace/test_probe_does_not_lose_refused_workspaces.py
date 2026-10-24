"""A workspace the deployment REFUSES is not a workspace that was LOST (ticket 01a1072f).

The probe counts any ``get_workspace`` exception as a miss, and three misses flip a row to ``failed`` and end every
open session on it ``workspace_lost``, which is permanent. A refused local workspace is intact on disk and becomes
usable again once it moves to a docker or kubernetes provider, so the probe must leave it, and its sessions, alone.
"""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

from primer.model.workspace import Workspace, WorkspaceRuntimeMeta
from primer.model.workspace_refusal import WorkspaceRefusedError
from primer.workspace.probe import WorkspaceProbeTask

REFUSED = WorkspaceRefusedError("provider 'local' refused", provider_id="local", signals=("runtime_mode is 'worker'",))


def _row(id_: str, phase: str = "running") -> Workspace:
    """A real row: the probe's write is fenced on its stored fields (``raw_generation``)."""
    return Workspace(
        id=id_, template_id="tpl-1", provider_id="local-1", created_at=datetime.now(timezone.utc), phase=phase,
        runtime_meta=WorkspaceRuntimeMeta(url="ws://unused", token="unused"),
    )


def _task(rows: list[Workspace], registry: MagicMock) -> tuple[WorkspaceProbeTask, MagicMock]:
    storage = MagicMock()
    storage.list = AsyncMock(return_value=MagicMock(items=rows))
    # The probe writes its fields with one fenced patch_if (ticket 01a16d72 ruling 3); a non-None answer is "the guard matched".
    storage.patch_if = AsyncMock(return_value=MagicMock())
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
    storage.patch_if.assert_not_awaited()


async def test_a_refused_workspace_does_not_stop_the_others_being_probed() -> None:
    refused, healthy = _row("ws-local"), _row("ws-docker")
    handle = MagicMock()
    handle.ping = AsyncMock(return_value=True)
    registry = MagicMock()
    registry.get_workspace = AsyncMock(side_effect=lambda wid: handle if wid == "ws-docker" else (_ for _ in ()).throw(REFUSED))
    task, storage = _task([refused, healthy], registry)

    await task.tick()

    assert [c.args[0] for c in storage.patch_if.await_args_list] == ["ws-docker"], "only the healthy workspace's row is written"


async def test_a_refused_workspace_does_not_poison_the_miss_counter_of_a_later_real_failure() -> None:
    """Refusals are not misses, so a workspace that is then really unreachable still needs three of its own."""
    row = _row("ws-1")
    registry = MagicMock()
    registry.get_workspace = AsyncMock(
        side_effect=[REFUSED, REFUSED, REFUSED, RuntimeError("down"), RuntimeError("down")],
    )
    task, storage = _task([row], registry)

    with patch("primer.workspace.probe.reconcile_sessions_to_workspace_lost", new=AsyncMock()) as reconcile:
        for _ in range(5):
            await task.tick()

    reconcile.assert_not_awaited()
    for call in storage.patch_if.await_args_list:
        assert call.args[1].get("phase") != "failed", "three refusals plus two misses must not add up to a failure"
