"""The workspace container's host config carries the capability isolation (2026-11-14 item, review 5.2).

``create_sandbox`` built a host_config with only the mounts, the published port and the CPU/memory limits: every
capability the image's root user could ask for was left on, nothing refused privilege gains, and a template that asked
for ``network: "none"`` was silently ignored (the branch was a TODO). The host config now drops all capabilities
(the runtime needs none: it binds port 5959, above 1024, and does no chown/setuid/mount/ptrace) and sets
``no-new-privileges``. ``network: "none"`` is refused with a ``ConfigError`` (fix round 1): primer reaches the runtime
only over the network, so such a workspace would be unreachable. No Docker daemon: the aiodocker client is a fake that records what is sent.
"""

from __future__ import annotations

import sys
import types
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

import primer.workspace.runtime.docker as docker_mod
from primer.model.except_ import ConfigError
from primer.model.workspace import (
    ContainerConnectionSocket,
    ContainerReachabilityHostPort,
    ContainerWorkspaceConfig,
    ResourceLimits,
)
from primer.workspace.runtime.docker import DockerRuntimeAdapter



class _DockerError(Exception):
    """Mirror of ``aiodocker.DockerError`` as ``primer.workspace.runtime.docker`` reads it: ``status`` and ``message``
    (a string, or the response body's dict with a ``message`` key on older versions)."""

    def __init__(self, status: int = 0, message: Any = "") -> None:
        super().__init__(message)
        self.status = status
        self.message = message


@pytest.fixture(autouse=True)
def _aiodocker_available(monkeypatch) -> None:
    """``create_sandbox`` imports the exception class for its pull-failure branch and nothing else; where the docker
    extra is absent a stand-in keeps this unit test (which asserts on the host config the code builds) running. It is
    installed for the test only, so it never leaks into another module's import of ``aiodocker``."""
    try:
        import aiodocker  # noqa: F401
    except ModuleNotFoundError:
        stub = types.ModuleType("aiodocker")
        stub.DockerError = _DockerError
        monkeypatch.setitem(sys.modules, "aiodocker", stub)


class _FakeDocker:
    """The slice of the aiodocker client ``create_sandbox`` drives, recording what is sent to the daemon."""

    def __init__(self) -> None:
        self.captures: list[dict[str, Any]] = []
        self._volumes: dict[str, dict[str, str]] = {}
        self.images = MagicMock()
        self.images.inspect = AsyncMock(return_value={})
        self.images.pull = AsyncMock()
        self.volumes = MagicMock()
        self.volumes.create = AsyncMock(side_effect=self._volume_create)
        self.volumes.get = AsyncMock(side_effect=self._volume_get)
        self.containers = MagicMock()
        self.containers.create_or_replace = AsyncMock(side_effect=self._create_or_replace)

    async def _volume_create(self, spec: dict[str, Any]) -> None:
        self._volumes.setdefault(spec["Name"], dict(spec.get("Labels", {})))

    async def _volume_get(self, name: str) -> Any:
        if name not in self._volumes:
            raise Exception("404 volume not found")
        volume = MagicMock()
        volume.show = AsyncMock(return_value={"Labels": self._volumes[name]})
        return volume

    async def _create_or_replace(self, *, name: str, config: dict[str, Any]) -> Any:
        self.captures.append(config)
        container = MagicMock()
        container.start = AsyncMock()
        container.wait = AsyncMock(return_value={"StatusCode": 0})
        container.delete = AsyncMock()
        return container


def _adapter(docker: _FakeDocker) -> DockerRuntimeAdapter:
    adapter = DockerRuntimeAdapter(ContainerWorkspaceConfig(
        runtime="docker", connection=ContainerConnectionSocket(socket_path="/var/run/docker.sock"),
        reachability=ContainerReachabilityHostPort(),
    ))
    adapter._docker = docker
    return adapter


async def _create(
    monkeypatch, *, network: str = "full", volume_target: str = "/workspace", docker: _FakeDocker | None = None,
) -> _FakeDocker:
    async def _fake_make(docker_, cont, name, token, *, reachability, workspace_root):
        return MagicMock()

    monkeypatch.setattr(docker_mod, "_make_ws_sandbox", _fake_make)
    docker = docker or _FakeDocker()
    await _adapter(docker).create_sandbox(
        name="workspace-ws-1",
        image="primer/workspace-runtime:1.1",
        command=["python", "-m", "primer_runtime.server"],
        env={},
        workdir=volume_target,
        volume_name="workspace-ws-1-data",
        volume_target=volume_target,
        extra_mounts=[],
        user=None,
        resources=ResourceLimits(cpu_cores=1.0, memory_bytes=1024 * 1024, network=network),
        network=network,
        pull_policy="if_missing",
    )
    return docker


async def test_the_workspace_container_drops_all_capabilities_and_new_privileges(monkeypatch) -> None:
    docker = await _create(monkeypatch)
    host_config = docker.captures[0]["HostConfig"]

    assert host_config["CapDrop"] == ["ALL"]
    assert "CapAdd" not in host_config
    assert host_config["SecurityOpt"] == ["no-new-privileges"]


async def test_the_default_network_is_unchanged(monkeypatch) -> None:
    """No ``NetworkMode`` for the default: the host_port reachability publishes the runtime port exactly as before."""
    docker = await _create(monkeypatch)
    config = docker.captures[0]
    assert config["HostConfig"]["PortBindings"] == {
        "5959/tcp": [{"HostIp": "127.0.0.1", "HostPort": ""}],
    }
    assert "NetworkMode" not in config["HostConfig"]
    assert "NetworkingConfig" not in config


async def test_network_none_is_refused_before_anything_is_created(monkeypatch) -> None:
    """``network: "none"`` would create a workspace primer cannot reach, so it is a clear error, not a silent dead one."""
    docker = _FakeDocker()
    with pytest.raises(ConfigError, match="not supported"):
        await _create(monkeypatch, network="none", docker=docker)
    assert docker.captures == []
    docker.volumes.create.assert_not_called()


async def test_the_chmod_helper_drops_all_capabilities_too(monkeypatch) -> None:
    """The one-shot root helper that initialises a custom volume target gets the same treatment (it needs none: chmod is
    an owner operation)."""
    docker = await _create(monkeypatch, volume_target="/data")
    helper = docker.captures[0]
    assert helper["Entrypoint"] == ["/bin/chmod"]
    assert helper["HostConfig"]["CapDrop"] == ["ALL"]
    assert helper["HostConfig"]["NetworkMode"] == "none"
    assert helper["HostConfig"]["SecurityOpt"] == ["no-new-privileges:true"]
    assert helper["HostConfig"]["ReadonlyRootfs"] is True
