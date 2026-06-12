"""CRUD router for ArtifactStorageProvider (/v1/artifact_storage_providers).

Follows the SemanticSearchProvider pattern: ``make_crud_router`` plus a
PUT/DELETE invalidation hook and a reserved-id guard for the auto-seeded
default provider.
"""

from __future__ import annotations

from fastapi import Request

from primer.api.deps import get_artifact_storage_provider_storage
from primer.api.registries.artifact_storage_registry import (
    DEFAULT_ARTIFACT_PROVIDER_ID,
)
from primer.api.routers._crud import make_crud_router
from primer.model.provider import ArtifactStorageProvider


async def _on_update(entity_id: str, request: Request) -> None:
    """Invalidate the cached ArtifactStorage instance after PUT/DELETE."""
    registry = getattr(request.app.state, "artifact_storage_registry", None)
    if registry is not None:
        await registry.invalidate(entity_id)


async def _reject_reserved_delete(entity_id: str, request: Request) -> None:
    from fastapi import HTTPException

    if entity_id == DEFAULT_ARTIFACT_PROVIDER_ID:
        raise HTTPException(
            status_code=403,
            detail={
                "error": "reserved_id_protected",
                "kind": "artifact_storage_provider",
                "message": (
                    f"id {entity_id!r} is the reserved default artifact "
                    "provider and cannot be deleted"
                ),
            },
        )


artifact_storage_router = make_crud_router(
    model_cls=ArtifactStorageProvider,
    storage_dep=get_artifact_storage_provider_storage,
    plural="artifact_storage_providers",
    tag="artifact-storage-providers",
    on_update=_on_update,
    on_delete=_on_update,
    on_pre_delete_id=_reject_reserved_delete,
)


__all__ = ["artifact_storage_router"]
