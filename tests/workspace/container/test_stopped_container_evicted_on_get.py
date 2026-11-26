"""A Docker workspace whose container has stopped no longer stays dead until primer restarts (review 5.6).

The adapter already started a stopped container on re-attach (W21), but the backend's cache kept handing out the old
handle in the meantime: a container that stopped but still exists never makes its runtime client ``gone`` (only a 404
handshake sets that; a stopped container just refuses every reconnect), so the cache miss that would run the re-attach
never came. The backend now checks the container's state on every ``get`` and evicts a cached handle whose container
is in a concrete non-running state; the re-attach that follows starts the container and reconnects. No Docker daemon:
the aiodocker client is a fake, the reconnect and the wrapper build are monkeypatched (the real ``materialise`` opens
real sandbox operations).
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

from primer.model.workspace import (
    ContainerConnectionSocket,
    ContainerReachabilityHostPort,
    ContainerTemplateConfig,
    ContainerWorkspaceConfig,
    WorkspaceTemplate,
)
from primer.workspace.container.backend import ContainerWorkspaceBackend
from primer.workspace.runtime import docker as docker_mod
from primer.workspace.runtime.docker import DockerRuntimeAdapter
from primer.workspace.sandbox.workspace import SandboxWorkspace


def _template() -> WorkspaceTemplate:
    return WorkspaceTemplate(
        id="t", provider_id="p", description="", backend=ContainerTemplateConfig(),
    )


def _config() -> ContainerWorkspaceConfig:
    return ContainerWorkspaceConfig(
        runtime="docker",
        connection=ContainerConnectionSocket(socket_path="/var/run/docker.sock"),
        reachability=ContainerReachabilityHostPort(),
    )


def _docker_with_container(state: str | Exception) -> MagicMock:
    docker = MagicMock()
    if isinstance(state, Exception):
        docker.containers.get = AsyncMock(side_effect=state)
        return docker
    container = MagicMock()
    container.show = AsyncMock(return_value={
        "State": {"Status": state},
        "Config": {"WorkingDir": "/workspace", "Env": ["PRIMER_RUNTIME_TOKEN=tok"]},
        "Mounts": [{"Type": "volume", "Destination": "/workspace"}],
    })
    container.start = AsyncMock()
    docker.containers.get = AsyncMock(return_value=container)
    return docker


def _backend(docker: MagicMock) -> ContainerWorkspaceBackend:
    backend = ContainerWorkspaceBackend(_config(), adapter=DockerRuntimeAdapter(_config()))
    backend._adapter._docker = docker
    backend._initialised = True
    return backend


def _cached_handle(backend: ContainerWorkspaceBackend, workspace_id: str) -> MagicMock:
    handle = MagicMock()
    handle.gone = False
    handle.aclose = AsyncMock()
    handle.sandbox = MagicMock()
    handle.sandbox.aclose = AsyncMock()
    backend._workspaces[workspace_id] = handle
    return handle


def _reconnect_and_materialise(monkeypatch, backend: ContainerWorkspaceBackend, new_handle: MagicMock) -> MagicMock:
    """The real re-attach path with the two leaf calls replaced: the reconnect (a real WS) and the wrapper build
    (real sandbox operations). Returns the sandbox the fake reconnect hands out."""
    new_sandbox = MagicMock()
    new_sandbox.mapped_host_port = 5960      # the host port docker published; the URL builder needs it for host_port
    new_sandbox.workspace_root = "/workspace"

    async def _fake_make(docker_, cont, name, token, *, reachability, workspace_root):
        return new_sandbox

    async def _fake_materialise(cls, *, workspace_id, template, sandbox, backend_kind, runtime_meta, workspace_root):
        return new_handle

    monkeypatch.setattr(docker_mod, "_make_ws_sandbox", _fake_make)
    monkeypatch.setattr(SandboxWorkspace, "materialise", classmethod(_fake_materialise))
    return new_sandbox


# ---- the fix -------------------------------------------------------------------------------------------------------------


async def test_an_exited_container_is_restarted_on_the_next_use(monkeypatch) -> None:
    backend = _backend(_docker_with_container("exited"))
    old = _cached_handle(backend, "ws-1")
    new = MagicMock()
    new.gone = False
    new.aclose = AsyncMock()
    new_sandbox = _reconnect_and_materialise(monkeypatch, backend, new)

    result = await backend.get("ws-1", template=_template())

    assert result is new, "the re-attached handle is handed out, not the dead one"
    assert backend._workspaces["ws-1"] is new
    old.aclose.assert_awaited_once()  # the evicted handle is released
    # ... and so is its runtime connection (the handle's close only clears sessions; a client not closed would retry its
    # dead port forever), as destroy and aclose do
    old.sandbox.aclose.assert_awaited_once()
    container = backend._adapter._docker.containers.get.return_value
    container.start.assert_awaited_once()  # the exited container is started before the reconnect


async def test_a_running_container_keeps_its_cached_handle(monkeypatch) -> None:
    docker = _docker_with_container("running")
    backend = _backend(docker)
    old = _cached_handle(backend, "ws-1")
    new = MagicMock()
    _reconnect_and_materialise(monkeypatch, backend, new)

    assert await backend.get("ws-1", template=_template()) is old
    old.aclose.assert_not_awaited()
    docker.containers.get.return_value.start.assert_not_awaited()


async def test_a_daemon_outage_keeps_the_handle(monkeypatch) -> None:
    """A transport error (the daemon cannot be reached) says nothing about the handle's liveness: a transient
    failure must not evict a live one. The absent case is the opposite (a 404 is a concrete answer):
    ``test_an_absent_container_is_evicted_on_get``."""
    docker = _docker_with_container(Exception("dial unix /var/run/docker.sock: connection refused"))
    backend = _backend(docker)
    old = _cached_handle(backend, "ws-1")
    new = MagicMock()
    _reconnect_and_materialise(monkeypatch, backend, new)

    assert await backend.get("ws-1", template=_template()) is old
    old.aclose.assert_not_awaited()


async def test_an_absent_container_is_evicted_on_get(monkeypatch) -> None:
    """A 404 from the daemon is a concrete answer, not an unknown: the container is gone (destroyed out-of-band
    or by a racing destroy), so the cached handle is evicted and the re-attach finds nothing (X3.3). Keeping the
    handle here relied on the runtime client's ``gone`` flag catching up, which it does not when the connection
    died with the container before it could 404-handshake."""
    docker = _docker_with_container(Exception("404 container workspace-ws-1 not found"))
    backend = _backend(docker)
    old = _cached_handle(backend, "ws-1")

    assert await backend.get("ws-1", template=_template()) is None
    old.aclose.assert_awaited_once()  # the evicted handle is released
    old.sandbox.aclose.assert_awaited_once()  # ... and so is its runtime connection


# ---- the adapter's state reader -------------------------------------------------------------------------------


async def test_container_state_reads_the_daemon_status() -> None:
    docker = _docker_with_container("exited")
    adapter = DockerRuntimeAdapter(_config())
    adapter._docker = docker
    assert await adapter.container_state("workspace-ws-1") == "exited"


async def test_container_state_is_absent_for_an_absent_container() -> None:
    """A 404 names the state (``absent``); the backend evicts on any concrete non-running state, so a container
    that is gone stops waiting on the runtime client's ``gone`` flag to catch up (X3.3)."""
    docker = _docker_with_container(Exception("404 container not found"))
    adapter = DockerRuntimeAdapter(_config())
    adapter._docker = docker
    assert await adapter.container_state("workspace-ws-1") == "absent"


async def test_container_state_is_none_for_a_transport_error() -> None:
    """A failure that is not a 404 (the daemon unreachable) still cannot be determined: ``None`` keeps a live
    handle, so a transient outage never evicts one."""
    docker = _docker_with_container(Exception("dial unix /var/run/docker.sock: connection refused"))
    adapter = DockerRuntimeAdapter(_config())
    adapter._docker = docker
    assert await adapter.container_state("workspace-ws-1") is None
