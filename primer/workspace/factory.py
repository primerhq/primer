"""Factory for instantiating :class:`WorkspaceBackend`s from config.

Mirrors the :class:`primer.storage.factory.StorageProviderFactory` /
:class:`primer.vector.factory.VectorStoreProviderFactory` pattern.

Phase A wires only the Local provider. Container / Kubernetes raise
:class:`ConfigError` until phases B and C land.

Usage::

    config = WorkspaceProvider(
        id="local-1",
        provider=WorkspaceProviderType.LOCAL,
        config=LocalWorkspaceConfig(root_path="/var/lib/primer/workspaces"),
    )
    backend = WorkspaceBackendFactory.create(config)
    await backend.initialize()
"""

from __future__ import annotations

from pathlib import Path

from primer.int.workspace import WorkspaceBackend
from primer.model.except_ import ConfigError
from primer.model.workspace import (
    LocalWorkspaceConfig,
    WorkspaceProvider,
    WorkspaceProviderType,
)
from primer.workspace.local import LocalWorkspaceBackend


class WorkspaceBackendFactory:
    """Construct a :class:`WorkspaceBackend` from a config entry."""

    @staticmethod
    def create(
        config: WorkspaceProvider,
        *,
        subprocess_timeout_seconds: float = 120.0,
    ) -> WorkspaceBackend:
        """Dispatch on ``config.provider`` and build the matching backend.

        ``subprocess_timeout_seconds`` is forwarded to
        :class:`~primer.workspace.local.backend.LocalWorkspaceBackend` so
        every git / init_command subprocess in the local backend is bounded
        by the configured deadline.

        Raises :class:`ConfigError` when the provider enum is recognised
        but no backend has been wired yet for it (Container/Kubernetes
        in Phase A).
        """
        if config.provider == WorkspaceProviderType.LOCAL:
            if not isinstance(config.config, LocalWorkspaceConfig):
                raise ConfigError(
                    "provider='local' requires a LocalWorkspaceConfig"
                )
            return LocalWorkspaceBackend(
                root=Path(config.config.root_path),
                subprocess_timeout_seconds=subprocess_timeout_seconds,
            )
        if config.provider == WorkspaceProviderType.CONTAINER:
            from primer.model.workspace import ContainerWorkspaceConfig
            from primer.workspace.container.backend import (
                ContainerWorkspaceBackend,
            )
            if not isinstance(config.config, ContainerWorkspaceConfig):
                raise ConfigError(
                    "provider='container' requires a ContainerWorkspaceConfig"
                )
            return ContainerWorkspaceBackend(config.config)
        if config.provider == WorkspaceProviderType.KUBERNETES:
            from primer.model.workspace import KubernetesWorkspaceConfig
            from primer.workspace.k8s.backend import (
                KubernetesWorkspaceBackend,
            )
            if not isinstance(config.config, KubernetesWorkspaceConfig):
                raise ConfigError(
                    "provider='kubernetes' requires a KubernetesWorkspaceConfig"
                )
            return KubernetesWorkspaceBackend(config.config)
        raise ConfigError(
            f"no backend wired for WorkspaceProviderType {config.provider!r}"
        )


__all__ = [
    "WorkspaceBackendFactory",
]
