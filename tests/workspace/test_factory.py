"""Tests for WorkspaceBackendFactory dispatch.

Validates that the factory builds the right backend for each provider
type. Real runtime adapters (Docker / Podman / containerd) are not
constructed here -- the factory hands the config to
:class:`ContainerWorkspaceBackend`, but actual sandbox creation needs
a live daemon and is covered by gated contract tests.
"""

from __future__ import annotations

import pytest

from matrix.model.except_ import ConfigError
from matrix.model.workspace import (
    ContainerWorkspaceConfig,
    KubernetesWorkspaceConfig,
    LocalWorkspaceConfig,
    WorkspaceProvider,
    WorkspaceProviderType,
    _DockerRuntimeConfig,
    _PodmanRuntimeConfig,
)
from matrix.workspace.factory import WorkspaceBackendFactory
from matrix.workspace.local import LocalWorkspaceBackend


def test_factory_builds_local() -> None:
    cfg = WorkspaceProvider(
        id="l1",
        provider=WorkspaceProviderType.LOCAL,
        config=LocalWorkspaceConfig(path="/tmp/wsroot"),
    )
    backend = WorkspaceBackendFactory.create(cfg)
    assert isinstance(backend, LocalWorkspaceBackend)


def test_factory_builds_container_docker() -> None:
    cfg = WorkspaceProvider(
        id="c1",
        provider=WorkspaceProviderType.CONTAINER,
        config=ContainerWorkspaceConfig(runtime=_DockerRuntimeConfig()),
    )
    backend = WorkspaceBackendFactory.create(cfg)
    from matrix.workspace.container.backend import ContainerWorkspaceBackend

    assert isinstance(backend, ContainerWorkspaceBackend)


def test_factory_builds_container_podman_raises_stub() -> None:
    """Podman adapter is stubbed in Phase B; construction raises ConfigError."""
    cfg = WorkspaceProvider(
        id="c1",
        provider=WorkspaceProviderType.CONTAINER,
        config=ContainerWorkspaceConfig(runtime=_PodmanRuntimeConfig()),
    )
    with pytest.raises(ConfigError, match="Podman"):
        WorkspaceBackendFactory.create(cfg)


def test_factory_builds_kubernetes() -> None:
    cfg = WorkspaceProvider(
        id="k1",
        provider=WorkspaceProviderType.KUBERNETES,
        config=KubernetesWorkspaceConfig(),
    )
    backend = WorkspaceBackendFactory.create(cfg)
    from matrix.workspace.k8s.backend import KubernetesWorkspaceBackend

    assert isinstance(backend, KubernetesWorkspaceBackend)


def test_factory_kind_mismatch_rejected_at_parse_time() -> None:
    """Provider/config kind mismatch is rejected by the model validator."""
    with pytest.raises(ValueError):
        WorkspaceProvider(
            id="bad",
            provider=WorkspaceProviderType.CONTAINER,
            config=LocalWorkspaceConfig(path="/tmp/x"),
        )
