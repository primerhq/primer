"""Dead workspaces no longer drag the health probe through N consecutive timeouts (review 5.7).

The probe pinged workspaces one after another, so a page of N unreachable workspaces paid the full per-ping bound N
times over and one tick could outlast its own interval. The pings of one page now run concurrently under a small
concurrency bound, so N dead workspaces finish in about one timeout instead of N, with the streak counters and the
per-row writes unchanged.
"""

from __future__ import annotations

import asyncio
import time
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

from primer.model.workspace import Workspace, WorkspaceRuntimeMeta
from primer.workspace import probe as probe_mod
from primer.workspace.probe import WorkspaceProbeTask
from tests._support.reconcile_guard import reconcile_query_must_not_fail  # noqa: F401  (autouse: see the module)


def _ws(id_: str, phase: str = "running") -> Workspace:
    return Workspace(
        id=id_, template_id="tpl-1", provider_id="p-1", created_at=datetime.now(timezone.utc), phase=phase,
        runtime_meta=WorkspaceRuntimeMeta(url="ws://unused", token="unused"),
    )


def _storage_provider(items: list) -> tuple[MagicMock, MagicMock]:
    storage = MagicMock()
    storage.list = AsyncMock(return_value=MagicMock(items=items))
    storage.find = AsyncMock(return_value=MagicMock(items=[], next_cursor=None))
    storage.patch_if = AsyncMock(return_value=MagicMock())
    sp = MagicMock()
    sp.get_storage = MagicMock(return_value=storage)
    return sp, storage


def _patches_by_id(storage: MagicMock) -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = {}
    for c in storage.patch_if.await_args_list:
        out.setdefault(c.args[0], []).append(c.args[1])
    return out


async def _never_answers() -> bool:
    await asyncio.Event().wait()
    return True


def _dead_handle_registry() -> MagicMock:
    registry = MagicMock()

    async def _get(wid):
        handle = MagicMock()
        handle.ping = _never_answers
        return handle

    registry.get_workspace = _get
    return registry


async def test_a_page_of_dead_workspaces_finishes_in_about_one_timeout(monkeypatch) -> None:
    """Eight dead workspaces each take the full per-ping bound; concurrently they overlap, so the tick is about one
    bound, not eight (a serial loop needs eight consecutive bounds)."""
    monkeypatch.setattr(probe_mod, "_MIN_PING_TIMEOUT_S", 0.05, raising=False)
    rows = [_ws(f"ws-{i}") for i in range(8)]
    sp, storage = _storage_provider(rows)
    task = WorkspaceProbeTask(
        storage_provider=sp, registry=_dead_handle_registry(), interval_seconds=0.01,
    )

    started = time.monotonic()
    await asyncio.wait_for(task.tick(), timeout=5)
    elapsed = time.monotonic() - started

    assert elapsed < 0.3, f"serial pings would take {0.05 * 8:g}s or more; the tick took {elapsed:.3f}s"
    assert elapsed >= 0.04, "the per-ping bound must actually have fired (a tick this fast pinged nothing)"
    patches = _patches_by_id(storage)
    assert set(patches) == {ws.id for ws in rows}, "every row on the page was probed"
    for row in rows:
        assert patches[row.id][0]["last_probe_ok"] is False
        assert task._miss_counts[row.id] == 1, "the miss streak advanced on every row"


async def test_the_fan_out_is_bounded_when_the_page_has_more_rows_than_the_bound(monkeypatch) -> None:
    """More rows than the bound: the limit is reached (the pings do overlap) and never exceeded."""
    bound = probe_mod._PROBE_CONCURRENCY
    rows = [_ws(f"ws-{i}") for i in range(bound * 3)]
    in_flight = 0
    peak = 0

    async def _ping() -> bool:
        nonlocal in_flight, peak
        in_flight += 1
        peak = max(peak, in_flight)
        try:
            await asyncio.sleep(0.02)
            return True
        finally:
            in_flight -= 1

    registry = MagicMock()

    async def _get(wid):
        handle = MagicMock()
        handle.ping = _ping
        return handle

    registry.get_workspace = _get
    sp, storage = _storage_provider(rows)
    task = WorkspaceProbeTask(storage_provider=sp, registry=registry, interval_seconds=5)

    await asyncio.wait_for(task.tick(), timeout=10)

    assert peak == bound, f"expected the pings to overlap up to the bound {bound}, saw a peak of {peak}"
    assert set(_patches_by_id(storage)) == {ws.id for ws in rows}, "every row was still probed"
