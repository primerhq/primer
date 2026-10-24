"""Pre-write checks for ``ArtifactStorageProvider`` rows, shared by the REST route and the system tools (ticket 01a1226f, the D3/D5 shared-validator pattern).

The reserved default (``artifact-storage-default``) is always the database store: the routing store reads every artifact without a location record from a direct
``DbArtifactStorage``. New files may go elsewhere through the separate active-store setting, but changing this row's kind would make those older database
artifacts unreadable. The shared write check therefore keeps the row on ``db`` even as more backends become buildable.

This module must not import ``primer.api``: ``primer.toolset.system`` imports it at module level and ``primer.api`` imports ``primer.toolset.system``. The
default id therefore comes from the leaf ``primer.model.providers.artifact`` (``primer.artifact.factory`` re-exports it).
"""

from __future__ import annotations

import asyncio

from primer.artifact.filesystem import prepare_root
from primer.artifact.routing import count_artifacts_held
from primer.common.entity_checks import EntityCheckError
from primer.model.except_ import ConfigError
from primer.int.storage_provider import StorageProvider
from primer.model.provider import ArtifactStorageProvider, ArtifactStorageProviderType
from primer.model.providers.artifact import (
    ACTIVE_ARTIFACT_STORAGE_CONFIG_ID,
    DEFAULT_ARTIFACT_PROVIDER_ID,
    ActiveArtifactStorageConfig,
)

#: Appended to the create and update descriptors of the system ``artifact_storage_provider`` tools: the agent reads the rule before it writes.
ARTIFACT_DEFAULT_WRITE_NOTE = (
    "The reserved ``artifact-storage-default`` row is the database store that every artifact without a location record is read from, so its "
    "``provider`` must stay ``db``: a create or an update of that row that names another kind returns ``type=validation-error`` naming "
    "``provider``, and nothing is stored. To store new files elsewhere, register another provider and select it with "
    "``set_active_artifact_store``."
)


def check_artifact_provider_write(entity: ArtifactStorageProvider) -> None:
    """Refuse a create or update that makes the reserved database fallback any other kind."""
    if entity.id != DEFAULT_ARTIFACT_PROVIDER_ID or entity.provider is ArtifactStorageProviderType.DB:
        return
    raise EntityCheckError(
        "validation",
        f"{DEFAULT_ARTIFACT_PROVIDER_ID!r} is the database store that every artifact without a location record is read from, so its provider must stay "
        f"'db', not {entity.provider.value!r}; register another provider and select it with PUT /v1/artifact_storage_active_config or "
        "system::set_active_artifact_store",
        code="artifact_default_must_be_db",
        field="provider",
    )


async def check_artifact_provider_delete(storage_provider: StorageProvider, provider_id: str) -> None:
    """Refuse deletion of the provider currently selected for new writes."""
    active = await storage_provider.get_storage(ActiveArtifactStorageConfig).get(ACTIVE_ARTIFACT_STORAGE_CONFIG_ID)
    if active is not None and active.provider_id == provider_id:
        raise EntityCheckError(
            "conflict",
            "currently the active artifact store; select another store first",
            code="cascade_blocked",
            extra={"referenced_by": ACTIVE_ARTIFACT_STORAGE_CONFIG_ID},
        )
    count = await count_artifacts_held(storage_provider, provider_id)
    if count:
        raise EntityCheckError(
            "conflict",
            f"artifact provider {provider_id!r} still holds {count} artifact(s), and they are read from it; it can be deleted once they are gone",
            code="artifact_provider_holds_artifacts",
            extra={"count": count},
        )


def _moved_field(entity: ArtifactStorageProvider, existing: ArtifactStorageProvider) -> str | None:
    if entity.provider is not existing.provider:
        return "provider"
    if entity.provider is ArtifactStorageProviderType.FILESYSTEM and entity.config.root != existing.config.root:
        return "config.root"
    if entity.provider is ArtifactStorageProviderType.S3:
        if entity.config.bucket != existing.config.bucket:
            return "config.bucket"
        if entity.config.prefix != existing.config.prefix:
            return "config.prefix"
    return None


async def check_artifact_provider_update(
    storage_provider: StorageProvider,
    entity: ArtifactStorageProvider,
    existing: ArtifactStorageProvider,
) -> None:
    """Refuse a location-key change while stored artifacts still name the row."""
    field = _moved_field(entity, existing)
    if field is None:
        return
    count = await count_artifacts_held(storage_provider, entity.id)
    if count:
        raise EntityCheckError(
            "conflict",
            f"artifact provider {entity.id!r} holds {count} artifact(s) at its current location; changing {field} would leave them unreadable. "
            "Register a new provider and select it as the active store instead",
            code="artifact_provider_holds_artifacts",
            field=field,
            extra={"count": count},
        )


async def prepare_artifact_provider(entity: ArtifactStorageProvider) -> None:
    """Prepare the location a provider row names before the row is written: a filesystem root is created when missing, checked for a write and
    given its marker (``primer.artifact.filesystem.prepare_root``, run in a thread). Other kinds have nothing to prepare.

    The REST route and the system tools run it LAST in their create and update checks, so a write that another check refuses prepares nothing.
    """
    if entity.provider is not ArtifactStorageProviderType.FILESYSTEM:
        return
    try:
        await asyncio.to_thread(prepare_root, entity.config.root)
    except ConfigError as exc:
        raise EntityCheckError("validation", str(exc), code="artifact_root_unusable", field="config.root") from exc


__all__ = [
    "ARTIFACT_DEFAULT_WRITE_NOTE",
    "check_artifact_provider_delete",
    "check_artifact_provider_update",
    "check_artifact_provider_write",
    "prepare_artifact_provider",
]
