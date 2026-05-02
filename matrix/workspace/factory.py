"""Factory for instantiating :class:`WorkspaceBackend`s from config.

Mirrors the :class:`matrix.storage.factory.StorageProviderFactory` /
:class:`matrix.vector.factory.VectorStoreProviderFactory` pattern.

Usage::

    config = WorkspaceProvider(
        id="local-1",
        provider=WorkspaceProviderType.LOCAL,
        config=LocalWorkspaceConfig(path="/var/lib/matrix/workspaces"),
    )
    backend = WorkspaceBackendFactory.create(config)
    await backend.initialize()
"""

from __future__ import annotations

from pathlib import Path

from matrix.int.workspace import WorkspaceBackend
from matrix.model.except_ import ConfigError
from matrix.model.workspace import (
    LocalWorkspaceConfig,
    WorkspaceProvider,
    WorkspaceProviderType,
)
from matrix.workspace.local import LocalWorkspaceBackend


class WorkspaceBackendFactory:
    """Construct a :class:`WorkspaceBackend` from a config entry."""

    @staticmethod
    def create(config: WorkspaceProvider) -> WorkspaceBackend:
        """Dispatch on ``config.provider`` and build the matching backend.

        Raises :class:`ConfigError` when the provider enum is recognised
        at the type level but no backend has been wired into this
        factory yet (only ``LOCAL`` ships today).
        """
        if config.provider == WorkspaceProviderType.LOCAL:
            if not isinstance(config.config, LocalWorkspaceConfig):
                # Defensive: WorkspaceProvider's own model_validator
                # already rejects mismatches, but keep the dispatcher
                # honest in case the union grows.
                raise ConfigError(
                    "provider='local' requires a LocalWorkspaceConfig"
                )
            return LocalWorkspaceBackend(root=Path(config.config.path))
        raise ConfigError(
            f"no backend wired for WorkspaceProviderType {config.provider!r}"
        )


__all__ = [
    "WorkspaceBackendFactory",
]
