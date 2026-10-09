"""Build a live ``ArtifactStorage`` from an ``ArtifactStorageProvider`` row."""

from __future__ import annotations

from primer.int.artifact_storage import ArtifactStorage
from primer.int.storage_provider import StorageProvider
from primer.model.except_ import ConfigError
from primer.model.provider import (
    ArtifactStorageProvider,
    ArtifactStorageProviderType,
)


#: The kinds ``build_artifact_storage`` can construct. The reserved default provider (``artifact-storage-default``) must name one of them: everything that
#: stores or serves chat media resolves it (``primer/artifact/checks.py`` refuses a write that would break that). Add a kind here when its backend ships.
BUILDABLE_KINDS: frozenset[ArtifactStorageProviderType] = frozenset({ArtifactStorageProviderType.DB})


def build_artifact_storage(
    row: ArtifactStorageProvider, *, storage_provider: StorageProvider,
) -> ArtifactStorage:
    """Dispatch a provider row to its concrete backend.

    Only the ``DB`` backend ships in v1; ``FILESYSTEM`` and ``S3`` are accepted
    enum values whose construction raises until implemented.
    """
    if row.provider in BUILDABLE_KINDS:
        from primer.artifact.db import DbArtifactStorage

        return DbArtifactStorage(storage_provider)
    raise ConfigError(
        f"artifact storage backend {row.provider.value!r} is not implemented"
    )


__all__ = ["BUILDABLE_KINDS", "build_artifact_storage"]
