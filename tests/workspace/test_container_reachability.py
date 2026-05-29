"""Tests for ContainerWorkspaceBackend.create() reachability handling.

These exercise that the backend honours the provider's reachability mode
when it asks the runtime adapter to create the container:

* ``host_port`` -- publish 5959 on the configured ``bind_host``, then
  read the actual mapped port back and feed it into ``build_runtime_url``.
* ``bridge_network`` -- attach the container to the named docker
  network, give it a stable hostname (``workspace-<workspace_id>``),
  and build a URL that uses the container name as the host.

The tests stub the runtime adapter so they exercise the backend's
plumbing without needing a live Docker daemon. The contract under test
is the call shape (``create_sandbox`` kwargs + the URL/token threaded
into the :class:`WSSandbox` it returns) -- not the on-the-wire docker
API.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from primer.model.workspace import (
    ContainerConnectionSocket,
    ContainerReachabilityBridge,
    ContainerReachabilityHostPort,
    ContainerTemplateConfig,
    ContainerWorkspaceConfig,
    WorkspaceTemplate,
)
from primer.workspace.container.backend import ContainerWorkspaceBackend


def _host_port_cfg() -> ContainerWorkspaceConfig:
    return ContainerWorkspaceConfig(
        runtime="docker",
        connection=ContainerConnectionSocket(
            socket_path="/var/run/docker.sock",
        ),
        reachability=ContainerReachabilityHostPort(bind_host="127.0.0.1"),
    )


def _bridge_cfg() -> ContainerWorkspaceConfig:
    return ContainerWorkspaceConfig(
        runtime="docker",
        connection=ContainerConnectionSocket(
            socket_path="/var/run/docker.sock",
        ),
        reachability=ContainerReachabilityBridge(network_name="primer-net"),
    )


def _template() -> WorkspaceTemplate:
    return WorkspaceTemplate(
        id="t1",
        provider_id="c1",
        description="",
        backend=ContainerTemplateConfig(image="primer/workspace-runtime:1.0"),
    )


def _stub_adapter(*, host_port: int = 32100) -> AsyncMock:
    """Return an adapter mock that satisfies the backend's create flow.

    ``create_sandbox`` resolves with a sandbox stub that has the methods
    the backend uses for file/init-command/cleanup. ``host_port`` is the
    port the adapter reports back via the ``mapped_host_port`` attribute
    of the sandbox object -- mirroring how a real docker adapter would
    look up ``NetworkSettings.Ports`` after the container starts.
    """
    adapter = AsyncMock()
    sandbox = AsyncMock()
    sandbox.id = "container-id"
    sandbox.exec = AsyncMock(
        return_value=MagicMock(exit_code=0, stdout=b"", stderr=b""),
    )
    sandbox.write_file = AsyncMock()
    sandbox.mapped_host_port = host_port
    adapter.create_sandbox = AsyncMock(return_value=sandbox)
    adapter.initialize = AsyncMock()
    adapter.aclose = AsyncMock()
    adapter.remove_volume = AsyncMock()
    return adapter


@pytest.mark.asyncio
async def test_host_port_mode_passes_bind_host_to_adapter() -> None:
    """The backend should pass the configured ``bind_host`` to
    ``adapter.create_sandbox`` so docker publishes the runtime port on
    that interface (not a hard-coded loopback)."""
    cfg = _host_port_cfg()
    adapter = _stub_adapter()
    backend = ContainerWorkspaceBackend(cfg, adapter=adapter)
    await backend.initialize()
    with patch(
        "primer.workspace.container.backend.SandboxWorkspace"
    ) as ws_cls:
        ws_cls.materialise = AsyncMock(return_value=MagicMock(id="ws-x"))
        await backend.create(_template())

    kwargs = adapter.create_sandbox.await_args.kwargs
    # The reachability info reaches the adapter so it can publish 5959
    # on the configured host interface (random host port).
    reach = kwargs["reachability"]
    assert reach.kind == "host_port"
    assert reach.bind_host == "127.0.0.1"
    # A runtime bearer token is generated per workspace.
    assert "token" in kwargs and len(kwargs["token"]) >= 32


@pytest.mark.asyncio
async def test_bridge_network_mode_passes_network_and_name() -> None:
    """In bridge_network mode the backend should hand the adapter the
    network name AND a container name shaped ``workspace-<id>`` so the
    docker bridge DNS lookup the URL builder uses actually resolves."""
    cfg = _bridge_cfg()
    adapter = _stub_adapter()
    backend = ContainerWorkspaceBackend(cfg, adapter=adapter)
    await backend.initialize()

    with patch(
        "primer.workspace.container.backend.SandboxWorkspace"
    ) as ws_cls:
        ws_cls.materialise = AsyncMock(return_value=MagicMock(id="ws-y"))
        await backend.create(_template())

    kwargs = adapter.create_sandbox.await_args.kwargs
    reach = kwargs["reachability"]
    assert reach.kind == "bridge_network"
    assert reach.network_name == "primer-net"
    # Container name MUST start with workspace- so docker bridge DNS
    # matches ``ws://workspace-<id>:5959/``.
    name = kwargs["name"]
    assert name.startswith("workspace-")


@pytest.mark.asyncio
async def test_host_port_mode_url_uses_mapped_port_from_adapter() -> None:
    """After the container starts the backend MUST read the actual host
    port the runtime got mapped to (the adapter returns it) and feed it
    into ``build_runtime_url``. We capture the URL by intercepting the
    URL-builder call."""
    cfg = _host_port_cfg()
    adapter = _stub_adapter(host_port=32987)
    backend = ContainerWorkspaceBackend(cfg, adapter=adapter)
    await backend.initialize()

    captured: dict[str, Any] = {}

    def _capture_url(*, provider_config, workspace_id, mapped_host_port=None,
                     k8s_object_name=None):
        captured["provider_config"] = provider_config
        captured["workspace_id"] = workspace_id
        captured["mapped_host_port"] = mapped_host_port
        return f"ws://127.0.0.1:{mapped_host_port}/"

    with patch(
        "primer.workspace.container.backend.build_runtime_url", _capture_url,
    ), patch(
        "primer.workspace.container.backend.SandboxWorkspace"
    ) as ws_cls:
        ws_cls.materialise = AsyncMock(return_value=MagicMock(id="ws-z"))
        await backend.create(_template())

    assert captured["mapped_host_port"] == 32987
    assert captured["provider_config"] is cfg


@pytest.mark.asyncio
async def test_bridge_mode_url_uses_workspace_hostname() -> None:
    """In bridge_network mode the URL is purely DNS-based -- the backend
    MUST NOT need the adapter's mapped port (there is none)."""
    cfg = _bridge_cfg()
    adapter = _stub_adapter()
    # Pretend the adapter reports no mapped port in bridge mode.
    adapter.create_sandbox.return_value.mapped_host_port = None
    backend = ContainerWorkspaceBackend(cfg, adapter=adapter)
    await backend.initialize()

    captured: dict[str, Any] = {}

    def _capture_url(*, provider_config, workspace_id, mapped_host_port=None,
                     k8s_object_name=None):
        captured["mapped_host_port"] = mapped_host_port
        captured["workspace_id"] = workspace_id
        # Real builder ignores mapped_host_port in bridge mode.
        return f"ws://workspace-{workspace_id}:5959/"

    with patch(
        "primer.workspace.container.backend.build_runtime_url", _capture_url,
    ), patch(
        "primer.workspace.container.backend.SandboxWorkspace"
    ) as ws_cls:
        ws_cls.materialise = AsyncMock(return_value=MagicMock(id="ws-bridge"))
        await backend.create(_template())

    # The backend did NOT pull mapped_host_port out of the sandbox in
    # bridge mode -- the URL builder is given None (or omitted).
    assert captured["mapped_host_port"] is None
    name = adapter.create_sandbox.await_args.kwargs["name"]
    assert name == f"workspace-{captured['workspace_id']}"
