"""Pre-write checks for ``ArtifactStorageProvider`` rows, shared by the REST route and the system tools (ticket 01a1226f, the D3/D5 shared-validator pattern).

The reserved default (``artifact-storage-default``) is always the database store: the routing store reads every artifact without a location record from a direct
``DbArtifactStorage``. New files may go elsewhere through the separate active-store setting, but changing this row's kind would make those older database
artifacts unreadable. The shared write check therefore keeps the row on ``db`` even as more backends become buildable.

This module must not import ``primer.api``: ``primer.toolset.system`` imports it at module level and ``primer.api`` imports ``primer.toolset.system``. The
default id therefore comes from the leaf ``primer.model.providers.artifact`` (``primer.artifact.factory`` re-exports it).
"""

from __future__ import annotations

from primer.common.entity_checks import EntityCheckError
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


__all__ = ["ARTIFACT_DEFAULT_WRITE_NOTE", "check_artifact_provider_delete", "check_artifact_provider_write"]
