"""One dead runtime no longer freezes the health probe for every workspace (review W1).

The probe pinged the workspaces one after another, and a ping on a workspace whose runtime could not be reached (a stopped
container: every reconnect refused) never returned, so ``tick`` never finished: no workspace was failed, recovered or
reconciled again. Each ping is now bounded, and a ping that runs out of time is a miss like any other.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

from primer.model.workspace import Workspace, WorkspaceRuntimeMeta
from primer.workspace import probe as probe_mod
from primer.workspace.probe import WorkspaceProbeTask
from primer.workspace.runtime.runtime_client import RuntimeClient
from primer.workspace.runtime.ws_sandbox import WSSandbox
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


async def test_a_dead_runtime_does_not_hold_up_the_tick(monkeypatch) -> None:
    monkeypatch.setattr(probe_mod, "_MIN_PING_TIMEOUT_S", 0.2, raising=False)
    monkeypatch.setattr(RuntimeClient, "_HEALTH_TIMEOUT_S", 30.0, raising=False)    # the probe's own bound is the one that ends it
    client = RuntimeClient(url="ws://127.0.0.1:1/", token="t")                     # never connects: a stopped container
    dead = MagicMock()
    dead.ping = WSSandbox(runtime_client=client, container_id="c").ping
    healthy = MagicMock()
    healthy.ping = AsyncMock(return_value=True)
    sp, storage = _storage_provider([_ws("ws-dead"), _ws("ws-ok")])
    registry = MagicMock()
    registry.get_workspace = AsyncMock(side_effect=lambda wid: dead if wid == "ws-dead" else healthy)
    task = WorkspaceProbeTask(storage_provider=sp, registry=registry, interval_seconds=0.01)
    try:
        await asyncio.wait_for(task.tick(), timeout=3)
    finally:
        await client.aclose()

    healthy.ping.assert_awaited_once()
    patches = _patches_by_id(storage)
    assert patches["ws-dead"][-1]["last_probe_ok"] is False
    assert patches["ws-ok"][-1]["last_probe_ok"] is True


async def test_three_pings_that_run_out_of_time_fail_the_workspace(monkeypatch) -> None:
    monkeypatch.setattr(probe_mod, "_MIN_PING_TIMEOUT_S", 0.05, raising=False)

    async def _never_answers() -> bool:
        await asyncio.Event().wait()
        return True

    handle = MagicMock()
    handle.ping = _never_answers
    sp, storage = _storage_provider([_ws("ws-1")])
    registry = MagicMock()
    registry.get_workspace = AsyncMock(return_value=handle)
    task = WorkspaceProbeTask(storage_provider=sp, registry=registry, interval_seconds=0.01)
    for _ in range(3):
        await asyncio.wait_for(task.tick(), timeout=3)

    last = _patches_by_id(storage)["ws-1"][-1]
    assert last["phase"] == "failed"
    assert "did not answer" in last["failure_reason"]


async def test_a_slow_registry_lookup_is_bounded_too(monkeypatch) -> None:
    """Resolving the handle can re-attach (a connect, a pod wait): it is inside the same bound."""
    monkeypatch.setattr(probe_mod, "_MIN_PING_TIMEOUT_S", 0.05, raising=False)

    async def _hangs(wid):
        await asyncio.Event().wait()

    sp, storage = _storage_provider([_ws("ws-1")])
    registry = MagicMock()
    registry.get_workspace = _hangs
    task = WorkspaceProbeTask(storage_provider=sp, registry=registry, interval_seconds=0.01)
    await asyncio.wait_for(task.tick(), timeout=3)

    assert _patches_by_id(storage)["ws-1"][-1]["last_probe_ok"] is False


async def test_a_quick_answer_under_a_tiny_interval_is_still_a_hit() -> None:
    """The bound never undercuts a short floor, so a test-sized interval does not turn a real answer into a miss."""

    async def _answers_after_a_moment() -> bool:
        await asyncio.sleep(0.05)
        return True

    handle = MagicMock()
    handle.ping = _answers_after_a_moment
    sp, storage = _storage_provider([_ws("ws-1", phase="pending")])
    registry = MagicMock()
    registry.get_workspace = AsyncMock(return_value=handle)
    await WorkspaceProbeTask(storage_provider=sp, registry=registry, interval_seconds=0.001).tick()

    last = _patches_by_id(storage)["ws-1"][-1]
    assert (last["last_probe_ok"], last["phase"]) == (True, "running")
