"""A destroy that never finished no longer leaves its workspace ``terminating`` for ever (review W52).

The destroy marks the row ``terminating`` before the teardown, and the probe skips such a row. A failed destroy corrects the
phase itself, but a process that crashed (or was killed) in the middle left the mark with nothing to ever move it again. The
mark now carries the time it was written, and the probe fails a mark older than the staleness bound with the words a failed
destroy uses, ending its open sessions as every move to ``failed`` does. A mark without a time (written before this field
existed) and a fresh one are left alone.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

from primer.model.workspace import Workspace, WorkspaceRuntimeMeta
from primer.storage import raw_generation
from primer.workspace.probe import WorkspaceProbeTask
from tests._support.reconcile_guard import reconcile_query_must_not_fail  # noqa: F401  (autouse: see the module)


def _terminating(since: datetime | None) -> Workspace:
    return Workspace(
        id="ws-1", template_id="tpl-1", provider_id="p-1", created_at=datetime.now(timezone.utc), phase="terminating",
        runtime_meta=WorkspaceRuntimeMeta(url="ws://unused", token="unused"), terminating_since=since,
    )


def _storage_provider(items: list) -> tuple[MagicMock, MagicMock]:
    storage = MagicMock()
    storage.list = AsyncMock(return_value=MagicMock(items=items))
    storage.find = AsyncMock(return_value=MagicMock(items=[], next_cursor=None))
    storage.patch_if = AsyncMock(return_value=MagicMock())
    sp = MagicMock()
    sp.get_storage = MagicMock(return_value=storage)
    return sp, storage


async def _tick(row: Workspace) -> tuple[MagicMock, MagicMock]:
    sp, storage = _storage_provider([row])
    registry = MagicMock()
    registry.get_workspace = AsyncMock()
    await WorkspaceProbeTask(storage_provider=sp, registry=registry, interval_seconds=0.01).tick()
    return registry, storage


async def test_a_stale_mark_is_failed_by_the_next_tick() -> None:
    row = _terminating(datetime.now(timezone.utc) - timedelta(hours=2))
    registry, storage = await _tick(row)

    registry.get_workspace.assert_not_called()      # a workspace being (or that was being) deleted is never pinged
    (call,) = storage.patch_if.await_args_list
    patch, where = call.args[1], call.kwargs["where"]
    assert patch["phase"] == "failed"
    assert patch["failure_reason"].startswith("the delete did not finish (")
    assert patch["failure_reason"].endswith("); the runtime may be partly torn down: delete it again")
    assert where == {"phase": ["terminating"], "terminating_since": [raw_generation(row, "terminating_since")]}
    storage.find.assert_awaited()                   # its open sessions are ended, as on every move to failed


async def test_a_fresh_mark_is_left_alone() -> None:
    registry, storage = await _tick(_terminating(datetime.now(timezone.utc) - timedelta(seconds=5)))

    registry.get_workspace.assert_not_called()
    storage.patch_if.assert_not_called()


async def test_a_mark_without_a_time_is_left_alone() -> None:
    registry, storage = await _tick(_terminating(None))

    registry.get_workspace.assert_not_called()
    storage.patch_if.assert_not_called()
