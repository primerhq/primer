"""Read and replace the singleton active artifact-storage setting."""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from primer.artifact.probe import ArtifactProbeResult, probe_artifact_provider
from primer.common.entity_checks import EntityCheckError
from primer.int.storage_provider import StorageProvider
from primer.model.provider import ArtifactStorageProvider
from primer.model.providers.artifact import ACTIVE_ARTIFACT_STORAGE_CONFIG_ID, ActiveArtifactStorageConfig

Probe = Callable[..., Awaitable[ArtifactProbeResult]]


async def read_active_artifact_store(storage_provider: StorageProvider) -> ActiveArtifactStorageConfig:
    """Return the stored selection, or the database default when absent."""
    storage = storage_provider.get_storage(ActiveArtifactStorageConfig)
    row = await storage.get(ACTIVE_ARTIFACT_STORAGE_CONFIG_ID)
    return row or ActiveArtifactStorageConfig(id=ACTIVE_ARTIFACT_STORAGE_CONFIG_ID)


async def set_active_artifact_store(
    storage_provider: StorageProvider,
    provider_id: str,
    *,
    probe: Probe | None = None,
) -> ActiveArtifactStorageConfig:
    """Probe and select an existing provider for all new artifact writes."""
    provider = await storage_provider.get_storage(ArtifactStorageProvider).get(provider_id)
    if provider is None:
        raise EntityCheckError(
            "validation",
            f"artifact store {provider_id!r} does not exist",
            code="unknown_provider_id",
            field="provider_id",
        )
    result = await (probe or probe_artifact_provider)(provider, storage_provider=storage_provider)
    if not result.ok:
        raise EntityCheckError(
            "validation",
            f"artifact store {provider_id!r} failed its check at {result.step}: {result.error}",
            code="artifact_store_unusable",
            field="provider_id",
            extra={"step": result.step},
        )

    storage = storage_provider.get_storage(ActiveArtifactStorageConfig)
    row = ActiveArtifactStorageConfig(id=ACTIVE_ARTIFACT_STORAGE_CONFIG_ID, provider_id=provider_id)
    if await storage.get(ACTIVE_ARTIFACT_STORAGE_CONFIG_ID) is None:
        return await storage.create(row)
    return await storage.update(row)


__all__ = ["read_active_artifact_store", "set_active_artifact_store"]
