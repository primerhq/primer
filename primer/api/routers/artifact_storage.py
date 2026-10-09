"""CRUD router for ArtifactStorageProvider (/v1/artifact_storage_providers).

Follows the SemanticSearchProvider pattern: ``make_crud_router`` plus a
PUT/DELETE invalidation hook and a reserved-id guard for the auto-seeded
default provider, and the ``GET /_types`` helper every provider class serves
(form metadata for the console's Register provider menu and form).
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request

from primer.api.deps import get_artifact_storage_provider_storage
from primer.api.registries.artifact_storage_registry import (
    DEFAULT_ARTIFACT_PROVIDER_ID,
)
from primer.api.routers._crud import make_crud_router, preserve_masked_secrets_on_update
from primer.api.routers.providers import _form_field
from primer.artifact.checks import check_artifact_provider_on_update
from primer.common.entity_checks import EntityCheckError
from primer.model.provider import ArtifactStorageProvider


async def _on_update(entity_id: str, request: Request) -> None:
    """Invalidate the cached ArtifactStorage instance after PUT/DELETE."""
    registry = getattr(request.app.state, "artifact_storage_registry", None)
    if registry is not None:
        await registry.invalidate(entity_id)


async def _pre_update(entity: ArtifactStorageProvider, existing: ArtifactStorageProvider, request: Request) -> None:
    """Restore masked secrets, then refuse a write that leaves the reserved default naming a kind the factory cannot build (shared with the system tools)."""
    await preserve_masked_secrets_on_update(entity, existing, request)
    try:
        check_artifact_provider_on_update(entity, existing)
    except EntityCheckError as exc:
        raise HTTPException(
            status_code=422,
            detail={"error": exc.code, "kind": "artifact_storage_provider", "field": exc.field, "message": exc.message},
        ) from exc


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


# ---------- _types: form metadata for the console's provider form ----------
#
# The provider classes with a console form serve ``GET /v1/{plural}/_types`` (docs/dev/architecture/provider-pattern.md): one entry per provider-type value, ``{label, config_fields, row_fields,
# discoverable}``. This class had none, so the console's Register provider menu said "No kinds available." and no row of the class could be registered from the page (board ticket 01a1214c). The field
# lists mirror the config models in primer/model/providers/artifact.py (tests/api/test_artifact_storage_types.py holds them to the models); a row of this class has no fields of its own, no limits
# block and no model list to discover.
#
# THE LABELS TELL THE TRUTH ABOUT WHAT A ROW DOES. ``build_artifact_storage`` (primer/artifact/factory.py) builds only the ``db`` backend, and every consumer reads ONLY the reserved row
# (``ArtifactStorageRegistry.get_default()``, id ``artifact-storage-default``): a ``filesystem`` or ``s3`` row can be stored and is never used, and a ``db`` row registered here is not the default.
# The test builds a row of each kind and requires 'not implemented' in the label of every kind the factory refuses, so the labels change by themselves when a backend ships.
#
# This router MUST be mounted before the CRUD router in _app_routes.py, or GET /{id} swallows "_types".


artifact_storage_helpers_router = APIRouter(tags=["artifact-storage-providers"])


@artifact_storage_helpers_router.get(
    "/artifact_storage_providers/_types",
    summary="Provider-type metadata for the catalog's artifact-storage form.",
)
async def list_artifact_storage_types() -> dict[str, dict[str, Any]]:
    return {
        "db": {
            "label": "Database: only the built-in artifact-storage-default row is used",
            "config_fields": [],
            "row_fields": [],
            "discoverable": False,
        },
        "filesystem": {
            "label": "Filesystem (not implemented yet: stored, never used)",
            "config_fields": [
                _form_field("root", "Root directory", "text", required=True, help_="Directory under which artifact bytes are written.", placeholder="/var/lib/primer/artifacts"),
            ],
            "row_fields": [],
            "discoverable": False,
        },
        "s3": {
            "label": "S3 or S3-compatible store (not implemented yet: stored, never used)",
            "config_fields": [
                _form_field("bucket", "Bucket", "text", required=True, help_="Target bucket name."),
                _form_field("prefix", "Key prefix", "text", help_="Prefix of the stored objects' keys; blank for the bucket root."),
                _form_field("endpoint_url", "Endpoint URL", "url", help_="Override endpoint, for an S3-compatible store; blank for AWS.", placeholder="https://s3.example.com"),
                _form_field("region", "Region", "text", help_="Bucket region."),
                _form_field("access_key", "Access key id", "password"),
                _form_field("secret_key", "Secret access key", "password"),
            ],
            "row_fields": [],
            "discoverable": False,
        },
    }


artifact_storage_router = make_crud_router(
    model_cls=ArtifactStorageProvider,
    storage_dep=get_artifact_storage_provider_storage,
    plural="artifact_storage_providers",
    tag="artifact-storage-providers",
    on_update=_on_update,
    on_delete=_on_update,
    on_pre_update=_pre_update,
    on_pre_delete_id=_reject_reserved_delete,
)


__all__ = ["artifact_storage_helpers_router", "artifact_storage_router"]
