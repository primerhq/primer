"""Build a live ``ArtifactStorage`` from an ``ArtifactStorageProvider`` row."""

from __future__ import annotations

from primer.int.artifact_storage import ArtifactStorage
from primer.int.storage_provider import StorageProvider
from primer.model.except_ import ConfigError
from primer.model.provider import (
    ArtifactStorageProvider,
    ArtifactStorageProviderType,
)


def build_artifact_storage(
    row: ArtifactStorageProvider, *, storage_provider: StorageProvider,
) -> ArtifactStorage:
    """Dispatch a provider row to its concrete backend.

    Only the ``DB`` backend ships in v1; ``FILESYSTEM`` and ``S3`` are accepted
    enum values whose construction raises until implemented.
    """
    if row.provider is ArtifactStorageProviderType.DB:
        from primer.artifact.db import DbArtifactStorage

        return DbArtifactStorage(storage_provider)
    raise ConfigError(
        f"artifact storage backend {row.provider.value!r} is not implemented"
    )


__all__ = ["build_artifact_storage"]
