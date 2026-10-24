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

#: Reserved id of the auto-seeded default provider (the DB backend), so chat media works with zero operator configuration. It is defined in
#: ``primer.model.providers.artifact`` (a leaf module that imports nothing from ``primer.api``) because ``primer/artifact/checks.py`` reads it,
#: ``primer.toolset.system`` imports that check, and ``primer.api`` imports ``primer.toolset.system``, so reading it from the registry was an import
#: cycle. This module and ``primer.api.registries.artifact_storage_registry`` re-export the same object.
from primer.model.providers.artifact import DEFAULT_ARTIFACT_PROVIDER_ID


def _build_db(row: ArtifactStorageProvider, storage_provider: StorageProvider) -> ArtifactStorage:
    del row  # the DB backend keeps the bytes in the deployment's own storage; its config has nothing to read
    from primer.artifact.db import DbArtifactStorage

    return DbArtifactStorage(storage_provider)


def _build_filesystem(row: ArtifactStorageProvider, storage_provider: StorageProvider) -> ArtifactStorage:
    del storage_provider  # the files live under the row's root, not in the deployment's storage
    from primer.artifact.filesystem import FilesystemArtifactStorage

    # Building does no IO: ``initialize`` checks the root (and its marker) in the process that uses the store.
    return FilesystemArtifactStorage(row.config.root)


#: The builder of each kind ``build_artifact_storage`` can construct. ``s3`` is an accepted enum value with no builder yet, so a row naming it
#: raises ``ConfigError``. A backend ships by adding its builder here.
_BUILDERS: dict[ArtifactStorageProviderType, Callable[[ArtifactStorageProvider, StorageProvider], ArtifactStorage]] = {
    ArtifactStorageProviderType.DB: _build_db,
    ArtifactStorageProviderType.FILESYSTEM: _build_filesystem,
}

#: The kinds ``build_artifact_storage`` can construct: the keys of ``_BUILDERS``, so a kind is in this set exactly when it has a builder.
#: Being buildable is not enough for the reserved default provider: it must stay ``db`` (``primer/artifact/checks.py``), because the routing
#: store (``primer/artifact/routing.py``) reads every artifact without a location record from a direct database store, never through the row.
BUILDABLE_KINDS: frozenset[ArtifactStorageProviderType] = frozenset(_BUILDERS)


def build_artifact_storage(
    row: ArtifactStorageProvider, *, storage_provider: StorageProvider,
) -> ArtifactStorage:
    """Dispatch a provider row to the builder of its kind.

    The ``DB`` and ``FILESYSTEM`` backends build; ``S3`` is an accepted enum
    value with no builder, whose construction raises until implemented.
    """
    builder = _BUILDERS.get(row.provider)
    if builder is None:
        raise ConfigError(
            f"artifact storage backend {row.provider.value!r} is not implemented"
        )
    return builder(row, storage_provider)


__all__ = ["BUILDABLE_KINDS", "DEFAULT_ARTIFACT_PROVIDER_ID", "build_artifact_storage"]
