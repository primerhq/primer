"""Read and replace the singleton active artifact-storage setting."""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from primer.artifact.probe import ArtifactProbeResult, probe_artifact_provider
from primer.common.entity_checks import EntityCheckError
from primer.int.storage_provider import StorageProvider
from primer.model.except_ import ConflictError, NotFoundError
from primer.model.provider import ArtifactStorageProvider
from primer.model.providers.artifact import ACTIVE_ARTIFACT_STORAGE_CONFIG_ID, ActiveArtifactStorageConfig
from primer.storage import raw_generation
from primer.storage.cas import patch_if_checked

Probe = Callable[..., Awaitable[ArtifactProbeResult]]


async def read_active_artifact_store(storage_provider: StorageProvider) -> ActiveArtifactStorageConfig:
    """Return the stored selection, or the database default when absent."""
    storage = storage_provider.get_storage(ActiveArtifactStorageConfig)
    row = await storage.get(ACTIVE_ARTIFACT_STORAGE_CONFIG_ID)
    return row or ActiveArtifactStorageConfig(id=ACTIVE_ARTIFACT_STORAGE_CONFIG_ID)


def _selection_changed() -> EntityCheckError:
    return EntityCheckError(
        "conflict",
        "the active artifact store was changed by someone else while this one was being checked; read it again and retry",
        code="active_artifact_store_changed",
        field="provider_id",
    )


async def set_active_artifact_store(
    storage_provider: StorageProvider,
    provider_id: str,
    *,
    probe: Probe | None = None,
) -> ActiveArtifactStorageConfig:
    """Probe and select an existing provider for all new artifact writes.

    The probe can take seconds (an S3 round trip), and the delete of a provider is refused only while it is the active one: a delete that runs
    during the probe passes its own check. So the provider row is read again after the probe, and the selection is written with a
    compare-and-set against the selection read BEFORE the probe (the G5 audit, G5-06). Either race lost is a 409 (``kind="conflict"``) and
    leaves the selection as it was: ``artifact_store_deleted`` when the provider is gone, ``active_artifact_store_changed`` when another
    selection was written meanwhile. What is left is a window two statements wide: a delete whose active-store check ran before this
    selection was written and whose row delete lands after the re-read.
    """
    providers = storage_provider.get_storage(ArtifactStorageProvider)
    provider = await providers.get(provider_id)
    if provider is None:
        raise EntityCheckError(
            "validation",
            f"artifact store {provider_id!r} does not exist",
            code="unknown_provider_id",
            field="provider_id",
        )
    storage = storage_provider.get_storage(ActiveArtifactStorageConfig)
    before = await storage.get(ACTIVE_ARTIFACT_STORAGE_CONFIG_ID)
    result = await (probe or probe_artifact_provider)(provider, storage_provider=storage_provider)
    if not result.ok:
        raise EntityCheckError(
            "validation",
            f"artifact store {provider_id!r} failed its check at {result.step}: {result.error}",
            code="artifact_store_unusable",
            field="provider_id",
            extra={"step": result.step},
        )
    if await providers.get(provider_id) is None:
        raise EntityCheckError(
            "conflict",
            f"artifact store {provider_id!r} was deleted while it was being checked; the active store is unchanged",
            code="artifact_store_deleted",
            field="provider_id",
        )

    if before is None:
        try:
            return await storage.create(ActiveArtifactStorageConfig(id=ACTIVE_ARTIFACT_STORAGE_CONFIG_ID, provider_id=provider_id))
        except ConflictError:
            raise _selection_changed() from None
    try:
        written = await patch_if_checked(
            storage,
            ACTIVE_ARTIFACT_STORAGE_CONFIG_ID,
            {"provider_id": provider_id},
            where={"provider_id": [raw_generation(before, "provider_id")]},
        )
    except NotFoundError:
        raise _selection_changed() from None
    if written is None:
        raise _selection_changed()
    return written


__all__ = ["read_active_artifact_store", "set_active_artifact_store"]
