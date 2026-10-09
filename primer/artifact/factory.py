"""Build a live ``ArtifactStorage`` from an ``ArtifactStorageProvider`` row."""

from __future__ import annotations

from collections.abc import Callable

from primer.int.artifact_storage import ArtifactStorage
from primer.int.storage_provider import StorageProvider
from primer.model.except_ import ConfigError
from primer.model.provider import (
    ArtifactStorageProvider,
    ArtifactStorageProviderType,
)


#: Reserved id of the auto-seeded default provider (the DB backend), so chat media works with zero operator configuration. It is defined
#: here because this module imports nothing from ``primer.api``: ``primer/artifact/checks.py`` reads it, ``primer.toolset.system`` imports
#: that check, and ``primer.api`` imports ``primer.toolset.system``, so reading it from the registry was an import cycle.
#: ``primer.api.registries.artifact_storage_registry`` re-exports it.
DEFAULT_ARTIFACT_PROVIDER_ID = "artifact-storage-default"


def _build_db(row: ArtifactStorageProvider, storage_provider: StorageProvider) -> ArtifactStorage:
    del row  # the DB backend keeps the bytes in the deployment's own storage; its config has nothing to read
    from primer.artifact.db import DbArtifactStorage

    return DbArtifactStorage(storage_provider)


#: The builder of each kind ``build_artifact_storage`` can construct. ``filesystem`` and ``s3`` are accepted enum values with no builder
#: yet, so a row naming one raises ``ConfigError``. A backend ships by adding its builder here.
_BUILDERS: dict[ArtifactStorageProviderType, Callable[[ArtifactStorageProvider, StorageProvider], ArtifactStorage]] = {
    ArtifactStorageProviderType.DB: _build_db,
}

#: The kinds ``build_artifact_storage`` can construct: the keys of ``_BUILDERS``, so a kind is in this set exactly when it has a builder.
#: The reserved default provider must name one of them: everything that stores or serves chat media resolves it
#: (``primer/artifact/checks.py`` refuses a write that would break that).
BUILDABLE_KINDS: frozenset[ArtifactStorageProviderType] = frozenset(_BUILDERS)


def build_artifact_storage(
    row: ArtifactStorageProvider, *, storage_provider: StorageProvider,
) -> ArtifactStorage:
    """Dispatch a provider row to the builder of its kind.

    Only the ``DB`` backend ships in v1; ``FILESYSTEM`` and ``S3`` are accepted
    enum values with no builder, whose construction raises until implemented.
    """
    builder = _BUILDERS.get(row.provider)
    if builder is None:
        raise ConfigError(
            f"artifact storage backend {row.provider.value!r} is not implemented"
        )
    return builder(row, storage_provider)


__all__ = ["BUILDABLE_KINDS", "DEFAULT_ARTIFACT_PROVIDER_ID", "build_artifact_storage"]
