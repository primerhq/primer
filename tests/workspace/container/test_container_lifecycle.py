"""Container workspace lifecycle: a destroy that finds no running container, and closing the backend (review W19, W20).

* W19: when ``get_sandbox`` could not hand the container out (it is not running, its token is missing, its runtime does not
  answer), ``destroy`` raised ``NotFoundError`` and the registry deleted the row, leaving ``workspace-<id>`` and its
  ``-data`` volume behind; a same-id create then reused the volume and the old data came back. Both are now removed by
  name, and ``NotFoundError`` means neither existed.
* W20: closing the backend (every provider edit invalidates it) released each cached workspace's handle but never its
  runtime connection, whose receive, heartbeat and reconnect tasks then ran for ever.
"""

from __future__ import annotations

import shutil
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from primer.model.except_ import NotFoundError
from primer.model.workspace import WorkspaceRuntimeMeta
from primer.workspace.container.backend import ContainerWorkspaceBackend
from primer.workspace.runtime.ws_sandbox import WSSandbox
from primer.workspace.sandbox.workspace import SandboxWorkspace
from tests.workspace.container.test_container_backend import _config, _FakeAdapter, _template
from tests.workspace.container.test_teardown_closes_its_connection import _backend

pytestmark = pytest.mark.skipif(
    shutil.which("git") is None, reason="git CLI not available on PATH (SandboxStateRepo needs it)",
)


class _NotHandedOutAdapter(_FakeAdapter):
    """``get_sandbox`` answers None; what is still on the daemon is ``left`` (the container, the volume)."""

    def __init__(self, tmp_path: Path, *, container: bool, volume: bool) -> None:
        super().__init__(tmp_path)
        self.left = {"container": container, "volume": volume}
        self.removed_by_name: list[tuple[str, str]] = []

    async def get_sandbox(self, name: str):
        return None

    async def remove_by_name(self, name: str, volume_name: str) -> bool:
        self.removed_by_name.append((name, volume_name))
        existed = self.left["container"] or self.left["volume"]
        self.left = {"container": False, "volume": False}
        return existed


# ---- W19 -------------------------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(("container", "volume"), [(True, True), (True, False), (False, True)])
async def test_a_destroy_that_gets_no_sandbox_removes_the_container_and_volume_by_name(tmp_path, container, volume) -> None:
    adapter = _NotHandedOutAdapter(tmp_path, container=container, volume=volume)
    backend = ContainerWorkspaceBackend(_config(), adapter=adapter)
    await backend.initialize()

    await backend.destroy("proj")

    assert adapter.removed_by_name == [("workspace-proj", "workspace-proj-data")]
    assert adapter.left == {"container": False, "volume": False}


async def test_a_destroy_with_neither_container_nor_volume_is_not_found(tmp_path) -> None:
    adapter = _NotHandedOutAdapter(tmp_path, container=False, volume=False)
    backend = ContainerWorkspaceBackend(_config(), adapter=adapter)
    await backend.initialize()

    with pytest.raises(NotFoundError):
        await backend.destroy("proj")
    assert adapter.removed_by_name == [("workspace-proj", "workspace-proj-data")]


async def test_a_removal_that_fails_is_not_reported_as_not_found(tmp_path) -> None:
    """The row must stay (a failed destroy, "delete it again") rather than be dropped over a container that is still there."""
    adapter = _NotHandedOutAdapter(tmp_path, container=True, volume=True)
    adapter.remove_by_name = AsyncMock(side_effect=RuntimeError("daemon busy"))  # type: ignore[method-assign]
    backend = ContainerWorkspaceBackend(_config(), adapter=adapter)
    await backend.initialize()

    with pytest.raises(RuntimeError, match="daemon busy"):
        await backend.destroy("proj")


# ---- W20 -------------------------------------------------------------------------------------------------------------------------


async def test_closing_the_backend_closes_the_runtime_connection_of_every_cached_workspace(tmp_path) -> None:
    backend, adapter = await _backend(tmp_path)
    await backend.create(_template(), workspace_id="ws-a")
    await backend.create(_template(), workspace_id="ws-b")
    assert len(adapter.handed_out) == 2

    await backend.aclose()

    assert [s.closed for s in adapter.handed_out] == [1, 1]
    assert [s.removed for s in adapter.handed_out] == [0, 0], "closing the backend never removes a container"
    assert backend._workspaces == {}


async def test_closing_the_backend_closes_a_real_runtime_client() -> None:
    """The review's probe, inverted: the cached workspace's ``WSSandbox`` closes its ``RuntimeClient``."""
    adapter = MagicMock()
    adapter.aclose = AsyncMock()
    backend = ContainerWorkspaceBackend(_config(), adapter=adapter)
    client = MagicMock()
    client.aclose = AsyncMock()
    client.gone = False
    sandbox = WSSandbox(runtime_client=client, container_id="c")
    backend._workspaces["w"] = SandboxWorkspace(
        workspace_id="w", template=MagicMock(), sandbox=sandbox, state_repo=MagicMock(), truncation_store=MagicMock(),
        tools=[], backend_kind="container", runtime_meta=WorkspaceRuntimeMeta(url="ws://x", token="t"),
    )

    await backend.aclose()

    client.aclose.assert_awaited_once()
    adapter.aclose.assert_awaited_once()
