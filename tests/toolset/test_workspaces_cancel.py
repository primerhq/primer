"""A cancel landing inside ``create_workspace`` / ``delete_workspace`` must not leave a half-done workspace (task 01a10e2f, A2).

Both tools are declared ``interruptible=False``, so a Stop does not cancel them (past ``NON_INTERRUPTIBLE_GRACE_S`` it
abandons the call, which runs on). The flag governs a Stop only: a hard Cancel of the turn cancels every call, whatever it
declares (``run_stoppable._unwind_after_a_hard_cancel``), after a 3 s unwind. So the handlers have to survive a cancel
themselves:

* ``create_workspace`` has two writes with no common transaction, the live instance (``materialise``) and then the durable
  row. A cancel between them orphans a live container nothing tracks (the probe loop is row-driven, there is no orphan
  sweep). The REST route rolls the live instance back on ``BaseException``; the tool does the same.
* ``delete_workspace`` is ``destroy`` of the instance, a reconcile of its sessions, then the row delete. A cancel between
  them leaves a row pointing at a dead instance. The teardown is carried to its end behind a shield; the caller still sees
  the cancellation.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from primer.api.registries import WorkspaceRegistry
from primer.model.workspace import Workspace as WorkspaceRow
from primer.toolset.workspaces import build_workspaces_toolset
from tests.toolset.test_workspaces import _SP, _provider, _StubBackend, _template_body


@pytest.fixture
def sp() -> _SP:
    return _SP()


@pytest.fixture
def workspace_registry(sp) -> WorkspaceRegistry:
    return WorkspaceRegistry(sp, factory=_StubBackend)


@pytest.fixture
def toolset(sp, workspace_registry):
    return build_workspaces_toolset(storage_provider=sp, workspace_registry=workspace_registry)


async def _seed_template(toolset) -> None:
    for tool, entity in (("create_workspace_provider", _provider().model_dump(mode="json")), ("create_workspace_template", _template_body())):
        result = await toolset.call(tool_name=tool, arguments={"entity": entity})
        assert not result.is_error, result.output


async def _until(predicate, *, timeout: float = 2.0) -> None:
    """Wait (bounded) for a teardown that carries on in the background after its caller was cancelled."""
    async with asyncio.timeout(timeout):
        while not predicate():
            await asyncio.sleep(0.01)


async def _cancel(task: asyncio.Task) -> None:
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


class TestCreateWorkspaceRollsBackOnCancel:
    async def test_a_cancel_while_the_row_is_written_destroys_the_live_instance(self, sp, workspace_registry, toolset) -> None:
        await _seed_template(toolset)
        entered = asyncio.Event()

        async def row_create_that_never_returns(row: Any) -> Any:
            entered.set()
            await asyncio.Event().wait()

        sp.get_storage(WorkspaceRow).create = row_create_that_never_returns  # type: ignore[method-assign]
        backend = await workspace_registry.get_backend("local-1")

        call = asyncio.create_task(toolset.call(tool_name="create_workspace", arguments={"id": "ws-x", "template_id": "tpl-1"}))
        await entered.wait()
        assert "ws-x" in backend._workspaces, "precondition: the live instance exists while the row is being written"

        await _cancel(call)

        assert "ws-x" not in backend._workspaces, "the cancel orphaned a live workspace no row points at"

    async def test_a_failure_while_the_row_is_written_destroys_the_live_instance_and_raises_the_original_error(
        self, sp, workspace_registry, toolset
    ) -> None:
        await _seed_template(toolset)

        async def row_create_that_fails(row: Any) -> Any:
            raise RuntimeError("storage is down")

        sp.get_storage(WorkspaceRow).create = row_create_that_fails  # type: ignore[method-assign]
        backend = await workspace_registry.get_backend("local-1")

        with pytest.raises(RuntimeError, match="storage is down"):
            await toolset.call(tool_name="create_workspace", arguments={"id": "ws-x", "template_id": "tpl-1"})

        assert "ws-x" not in backend._workspaces

    async def test_a_failed_rollback_does_not_mask_the_cancel(self, sp, workspace_registry, toolset) -> None:
        await _seed_template(toolset)
        entered = asyncio.Event()

        async def row_create_that_never_returns(row: Any) -> Any:
            entered.set()
            await asyncio.Event().wait()

        async def destroy_that_fails(workspace_id: str) -> None:
            raise RuntimeError("docker is gone")

        sp.get_storage(WorkspaceRow).create = row_create_that_never_returns  # type: ignore[method-assign]
        backend = await workspace_registry.get_backend("local-1")
        backend.destroy = destroy_that_fails  # type: ignore[method-assign]

        call = asyncio.create_task(toolset.call(tool_name="create_workspace", arguments={"id": "ws-x", "template_id": "tpl-1"}))
        await entered.wait()

        await _cancel(call)  # the CancelledError, not the RuntimeError of the failed rollback

    async def test_a_create_that_succeeds_keeps_its_live_instance(self, sp, workspace_registry, toolset) -> None:
        await _seed_template(toolset)
        backend = await workspace_registry.get_backend("local-1")

        result = await toolset.call(tool_name="create_workspace", arguments={"id": "ws-x", "template_id": "tpl-1"})

        assert not result.is_error, result.output
        assert "ws-x" in backend._workspaces
        assert await sp.get_storage(WorkspaceRow).get("ws-x") is not None


class TestDeleteWorkspaceCarriesOnAfterACancel:
    async def _seeded(self, toolset, workspace_registry):
        await _seed_template(toolset)
        created = await toolset.call(tool_name="create_workspace", arguments={"id": "ws-x", "template_id": "tpl-1"})
        assert not created.is_error, created.output
        return await workspace_registry.get_backend("local-1")

    async def test_a_cancel_while_the_row_is_deleted_still_ends_with_the_row_deleted(self, sp, workspace_registry, toolset) -> None:
        backend = await self._seeded(toolset, workspace_registry)
        store = sp.get_storage(WorkspaceRow)
        real_delete = store.delete
        entered, release = asyncio.Event(), asyncio.Event()

        async def gated_delete(workspace_id: str) -> None:
            entered.set()
            await release.wait()
            await real_delete(workspace_id)

        store.delete = gated_delete  # type: ignore[method-assign]

        call = asyncio.create_task(toolset.call(tool_name="delete_workspace", arguments={"id": "ws-x"}))
        await entered.wait()
        assert "ws-x" not in backend._workspaces, "precondition: the instance is already destroyed, only the row is left"

        await _cancel(call)
        release.set()

        await _until(lambda: "ws-x" not in store._data)  # type: ignore[attr-defined]

    async def test_a_cancel_while_the_instance_is_destroyed_still_deletes_the_row(self, sp, workspace_registry, toolset) -> None:
        backend = await self._seeded(toolset, workspace_registry)
        store = sp.get_storage(WorkspaceRow)
        real_destroy = backend.destroy
        entered, release = asyncio.Event(), asyncio.Event()

        async def gated_destroy(workspace_id: str) -> None:
            entered.set()
            await release.wait()
            await real_destroy(workspace_id)

        backend.destroy = gated_destroy  # type: ignore[method-assign]

        call = asyncio.create_task(toolset.call(tool_name="delete_workspace", arguments={"id": "ws-x"}))
        await entered.wait()

        await _cancel(call)
        release.set()

        await _until(lambda: "ws-x" not in store._data and "ws-x" not in backend._workspaces)  # type: ignore[attr-defined]

    async def test_a_delete_that_is_not_cancelled_returns_its_result(self, sp, workspace_registry, toolset) -> None:
        await self._seeded(toolset, workspace_registry)

        result = await toolset.call(tool_name="delete_workspace", arguments={"id": "ws-x"})

        assert not result.is_error, result.output
        assert await sp.get_storage(WorkspaceRow).get("ws-x") is None

    async def test_an_unknown_id_is_still_a_not_found_error(self, toolset) -> None:
        result = await toolset.call(tool_name="delete_workspace", arguments={"id": "nope"})

        assert result.is_error and '"not-found"' in result.output
