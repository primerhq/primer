"""Tests for WorkspaceBackendFactory dispatch.

Validates that the factory builds the right backend for each provider
type. Real runtime adapters (Docker / Podman / containerd) are not
constructed here -- the factory hands the config to
:class:`ContainerWorkspaceBackend`, but actual sandbox creation needs
a live daemon and is covered by gated contract tests.
"""

from __future__ import annotations

import pytest

from primer.model.except_ import ConfigError
from primer.model.workspace import (
    ContainerConnectionSocket,
    ContainerReachabilityHostPort,
    ContainerWorkspaceConfig,
    K8sConnectionInCluster,
    K8sReachabilityInCluster,
    KubernetesWorkspaceConfig,
    LocalWorkspaceConfig,
    WorkspaceProvider,
    WorkspaceProviderType,
)
from primer.workspace.factory import WorkspaceBackendFactory
from primer.workspace.local import LocalWorkspaceBackend


def _container_cfg(runtime: str = "docker") -> ContainerWorkspaceConfig:
    return ContainerWorkspaceConfig(
        runtime=runtime,  # type: ignore[arg-type]
        connection=ContainerConnectionSocket(socket_path="/var/run/docker.sock"),
        reachability=ContainerReachabilityHostPort(bind_host="127.0.0.1"),
    )


def _k8s_cfg() -> KubernetesWorkspaceConfig:
    return KubernetesWorkspaceConfig(
        connection=K8sConnectionInCluster(),
        namespace="primer",
        reachability=K8sReachabilityInCluster(),
    )


def test_factory_builds_local() -> None:
    cfg = WorkspaceProvider(
        id="l1",
        provider=WorkspaceProviderType.LOCAL,
        config=LocalWorkspaceConfig(root_path="/tmp/wsroot"),
    )
    backend = WorkspaceBackendFactory.create(cfg)
    assert isinstance(backend, LocalWorkspaceBackend)


def test_factory_builds_container_docker() -> None:
    cfg = WorkspaceProvider(
        id="c1",
        provider=WorkspaceProviderType.CONTAINER,
        config=_container_cfg("docker"),
    )
    backend = WorkspaceBackendFactory.create(cfg)
    from primer.workspace.container.backend import ContainerWorkspaceBackend

    assert isinstance(backend, ContainerWorkspaceBackend)


def test_factory_builds_container_podman_raises_stub() -> None:
    """Podman adapter is stubbed in Phase B; construction raises ConfigError."""
    cfg = WorkspaceProvider(
        id="c1",
        provider=WorkspaceProviderType.CONTAINER,
        config=_container_cfg("podman"),
    )
    with pytest.raises(ConfigError, match="Podman"):
        WorkspaceBackendFactory.create(cfg)


def test_factory_builds_kubernetes() -> None:
    cfg = WorkspaceProvider(
        id="k1",
        provider=WorkspaceProviderType.KUBERNETES,
        config=_k8s_cfg(),
    )
    backend = WorkspaceBackendFactory.create(cfg)
    from primer.workspace.k8s.backend import KubernetesWorkspaceBackend

    assert isinstance(backend, KubernetesWorkspaceBackend)


def test_factory_kind_mismatch_rejected_at_parse_time() -> None:
    """Provider/config kind mismatch is rejected by the model validator."""
    with pytest.raises(ValueError):
        WorkspaceProvider(
            id="bad",
            provider=WorkspaceProviderType.CONTAINER,
            config=LocalWorkspaceConfig(root_path="/tmp/x"),
        )
