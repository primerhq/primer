"""The Docker adapter starts a stopped workspace container, and removes one it could not hand out (review W21, W19).

* W21: ``get_sandbox`` answered None for an exited container (and the container had no restart policy), so after a dockerd
  restart or a host reboot every container workspace went ``failed`` for good, while the backend's comment said the adapter
  starts it. An exited or created container is now started, then reconnected.
* W19: ``remove_by_name`` removes the container and its volume by name whatever state the container is in, for the destroy
  of a workspace ``get_sandbox`` could not hand out; it reports whether either existed.

No Docker daemon: the aiodocker client and the reconnect helper are fakes.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

import primer.workspace.runtime.docker as docker_mod
from primer.model.workspace import ContainerConnectionSocket, ContainerReachabilityHostPort, ContainerWorkspaceConfig
from primer.workspace.runtime.docker import DockerRuntimeAdapter


def _adapter(docker: MagicMock) -> DockerRuntimeAdapter:
    adapter = DockerRuntimeAdapter(ContainerWorkspaceConfig(
        runtime="docker", connection=ContainerConnectionSocket(socket_path="/var/run/docker.sock"),
        reachability=ContainerReachabilityHostPort(),
    ))
    adapter._docker = docker
    return adapter


def _with_container(state: str, *, env: list[str] | None = None, start_error: Exception | None = None):
    container = MagicMock()
    container.show = AsyncMock(return_value={
        "State": {"Status": state}, "Config": {"Env": ["PRIMER_RUNTIME_TOKEN=tok"] if env is None else env},
    })
    container.start = AsyncMock(side_effect=start_error)
    docker = MagicMock()
    docker.containers.get = AsyncMock(return_value=container)
    return _adapter(docker), container


def _recording_reconnect(monkeypatch, container: MagicMock) -> list[tuple[str, int]]:
    """Replace the reconnect; record the token it was given and how often the container had been started by then."""
    made: list[tuple[str, int]] = []

    async def _fake_make(docker, cont, name, token, *, reachability):
        made.append((token, container.start.await_count))
        return MagicMock()

    monkeypatch.setattr(docker_mod, "_make_ws_sandbox", _fake_make)
    return made


# ---- characterization ------------------------------------------------------------------------------------------------------------


async def test_a_running_container_is_reconnected_without_a_start(monkeypatch) -> None:
    adapter, container = _with_container("running")
    made = _recording_reconnect(monkeypatch, container)
    sandbox = await adapter.get_sandbox("workspace-ws-1")
    assert sandbox is not None and sandbox.recovered_token == "tok"
    assert made == [("tok", 0)]
    container.start.assert_not_awaited()


@pytest.mark.parametrize("state", ["paused", "dead", "removing", "restarting"])
async def test_a_container_in_a_state_a_start_cannot_fix_is_not_handed_out(monkeypatch, state) -> None:
    adapter, container = _with_container(state)
    made = _recording_reconnect(monkeypatch, container)
    assert await adapter.get_sandbox("workspace-ws-1") is None
    assert made == []
    container.start.assert_not_awaited()


# ---- W19: remove by name ---------------------------------------------------------------------------------------------------------


def _docker_for_removal(*, container_error: Exception | None = None, volume: bool = True,
                        volume_error: Exception | None = None) -> tuple[MagicMock, MagicMock, MagicMock]:
    docker = MagicMock()
    ctr = MagicMock()
    ctr.delete = AsyncMock(side_effect=container_error)
    docker.containers.container = MagicMock(return_value=ctr)
    vol = MagicMock()
    vol.delete = AsyncMock(side_effect=volume_error)
    docker.volumes.get = AsyncMock(return_value=vol) if volume else AsyncMock(side_effect=Exception("404 no such volume"))
    return docker, ctr, vol


@pytest.mark.parametrize(("container_there", "volume_there"), [(True, True), (True, False), (False, True), (False, False)])
async def test_remove_by_name_removes_what_is_there_and_says_whether_anything_was(container_there, volume_there) -> None:
    docker, ctr, vol = _docker_for_removal(
        container_error=None if container_there else Exception("404 no such container"), volume=volume_there,
    )
    adapter = _adapter(docker)

    existed = await adapter.remove_by_name("workspace-proj", "workspace-proj-data")

    assert existed is (container_there or volume_there)
    docker.containers.container.assert_called_once_with("workspace-proj")
    ctr.delete.assert_awaited_once_with(force=True, v=False)
    if volume_there:
        docker.volumes.get.assert_awaited_with("workspace-proj-data")
        vol.delete.assert_awaited_once()


async def test_remove_by_name_raises_what_the_daemon_refused() -> None:
    docker, _, vol = _docker_for_removal(container_error=Exception("500 driver failed"))
    with pytest.raises(Exception, match="driver failed"):
        await _adapter(docker).remove_by_name("workspace-proj", "workspace-proj-data")
    vol.delete.assert_not_awaited()
