"""The probe keeps the count of workspaces on local providers for /v1/health (ticket 01a1072f).

/v1/health reads in-process state only (it must stay 200 with the database gone), so the count is taken where the
database is already being read on a timer: the probe pages every workspace row each tick.
"""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

from primer.model.workspace import Workspace as WorkspaceRow
from primer.model.workspace import WorkspaceProvider, WorkspaceRuntimeMeta
from primer.workspace.probe import WorkspaceProbeTask


def _provider(id_: str, kind: str) -> MagicMock:
    provider = MagicMock()
    provider.id = id_
    provider.provider = kind
    return provider


def _row(id_: str, provider_id: str, phase: str = "terminating") -> WorkspaceRow:
    """A real row: the probe pings the running and failed ones and fences its write on their stored fields (``raw_generation``)."""
    return WorkspaceRow(
        id=id_, template_id="tpl-1", provider_id=provider_id, created_at=datetime.now(timezone.utc), phase=phase,
        runtime_meta=WorkspaceRuntimeMeta(url="ws://unused", token="unused"),
    )


def _probe(rows: list, providers: list) -> WorkspaceProbeTask:
    def storage_for(model):
        storage = MagicMock()
        storage.list = AsyncMock(return_value=MagicMock(items=rows if model is WorkspaceRow else providers))
        storage.patch_if = AsyncMock(return_value=MagicMock())
        return storage

    sp = MagicMock()
    sp.get_storage = MagicMock(side_effect=storage_for)
    return WorkspaceProbeTask(storage_provider=sp, registry=MagicMock(), interval_seconds=0.01)


async def test_the_count_is_unknown_until_the_first_tick() -> None:
    assert _probe([], []).local_workspace_count is None


async def test_the_count_covers_every_phase_on_a_local_provider() -> None:
    probe = _probe(
        [_row("a", "local", "running"), _row("b", "local", "failed"), _row("c", "local", "terminating"), _row("d", "docker")],
        [_provider("local", "local"), _provider("docker", "container")],
    )
    await probe.tick()
    assert probe.local_workspace_count == 3


async def test_a_deployment_with_no_local_provider_counts_zero() -> None:
    probe = _probe([_row("a", "docker")], [_provider("docker", "container")])
    await probe.tick()
    assert probe.local_workspace_count == 0


async def test_a_failed_provider_listing_keeps_the_last_count_and_does_not_break_the_tick() -> None:
    rows = [_row("a", "local")]
    probe = _probe(rows, [_provider("local", "local")])
    await probe.tick()
    assert probe.local_workspace_count == 1

    broken = MagicMock()
    broken.list = AsyncMock(side_effect=RuntimeError("db gone"))
    original = probe._sp.get_storage.side_effect
    probe._sp.get_storage.side_effect = lambda model: broken if model is WorkspaceProvider else original(model)
    await probe.tick()

    assert probe.local_workspace_count == 1
