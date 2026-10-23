"""CRUD router for ArtifactStorageProvider (/v1/artifact_storage_providers).

Follows the SemanticSearchProvider pattern: ``make_crud_router`` plus a
PUT/DELETE invalidation hook and a reserved-id guard for the auto-seeded
default provider, and the ``GET /_types`` helper every provider class serves
(form metadata for the console's Register provider menu and form).
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from primer.api.deps import get_artifact_storage_provider_storage
from primer.api.errors import common_responses
from primer.api.registries.artifact_storage_registry import (
    DEFAULT_ARTIFACT_PROVIDER_ID,
)
from primer.api.routers._crud import make_crud_router
from primer.api.routers._probe_text import draft_error
from primer.api.routers.providers import _form_field
from primer.artifact.checks import check_artifact_provider_write
from primer.artifact.probe import probe_artifact_provider
from primer.common.entity_checks import EntityCheckError
from primer.model.common import preserve_masked_secrets
from primer.model.except_ import NotFoundError
from primer.model.provider import ArtifactStorageProvider


async def _on_update(entity_id: str, request: Request) -> None:
    """Invalidate the cached ArtifactStorage instance after PUT/DELETE."""
    registry = getattr(request.app.state, "artifact_storage_registry", None)
    if registry is not None:
        await registry.invalidate(entity_id)


def _refuse_an_unbuildable_default(entity: ArtifactStorageProvider) -> None:
    """Run the rule the system tools share; a refusal is a 422 shaped like the reserved-delete 403, plus the field."""
    try:
        check_artifact_provider_write(entity)
    except EntityCheckError as exc:
        raise HTTPException(
            status_code=422,
            detail={"error": exc.code, "kind": "artifact_storage_provider", "field": exc.field, "message": exc.message},
        ) from exc


async def _pre_create(entity: ArtifactStorageProvider, request: Request) -> None:
    """Refuse a create of the reserved default (its row went missing) that names a kind the factory cannot build."""
    _refuse_an_unbuildable_default(entity)


async def _pre_update(entity: ArtifactStorageProvider, existing: ArtifactStorageProvider, request: Request) -> None:
    """Restore masked secrets, then refuse a write that leaves the reserved default naming a kind the factory cannot build."""
    preserve_masked_secrets(entity, existing)
    _refuse_an_unbuildable_default(entity)


async def _reject_reserved_delete(entity_id: str, request: Request) -> None:
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
# THE LABELS TELL THE TRUTH ABOUT WHAT A ROW DOES. ``build_artifact_storage`` (primer/artifact/factory.py) builds only the ``db`` backend, and every consumer goes through ``ArtifactStorageRegistry.get_default()``, the routing store
# (primer/artifact/routing.py): a put goes to the ACTIVE provider (``ActiveArtifactStorageConfig``; the database, through the reserved ``artifact-storage-default``, until another is selected) and a get or a delete follows the
# artifact's location record. So a ``filesystem`` or ``s3`` row can be stored but cannot be built, and a ``db`` row registered here is not the default.
# The test builds a row of each kind and requires 'not implemented' in the label of every kind the factory refuses, so the labels change by themselves when a backend ships.
#
# This router MUST be mounted before the CRUD router in _app_routes.py, or GET /{id} swallows "_types".


artifact_storage_helpers_router = APIRouter(tags=["artifact-storage-providers"])


class _ArtifactStorageDraft(BaseModel):
    id: str | None = None
    provider: Any = None
    config: Any = None


def _provider_storage(request: Request):
    return request.app.state.storage_provider.get_storage(ArtifactStorageProvider)


@artifact_storage_helpers_router.post(
    "/artifact_storage_providers/_test",
    responses=common_responses(500),
    summary="Test a draft artifact-storage provider with a live round trip.",
)
async def test_artifact_storage_draft(
    body: _ArtifactStorageDraft,
    request: Request,
) -> dict[str, Any]:
    try:
        draft = ArtifactStorageProvider.model_validate(
            body.model_dump(exclude_none=True),
        )
        if body.id is not None:
            stored = await _provider_storage(request).get(body.id)
            if stored is not None:
                preserve_masked_secrets(draft, stored)
    except Exception as exc:  # noqa: BLE001 - a partial draft is a 200 diagnostic
        return {"ok": False, "error": f"invalid draft: {draft_error(exc)}"}
    return (
        await probe_artifact_provider(
            draft,
            storage_provider=request.app.state.storage_provider,
        )
    ).as_dict()


@artifact_storage_helpers_router.post(
    "/artifact_storage_providers/{provider_id}/_test",
    responses=common_responses(404, 500),
    summary="Test a saved artifact-storage provider with a live round trip.",
)
async def test_saved_artifact_storage_provider(
    provider_id: str,
    request: Request,
) -> dict[str, Any]:
    row = await _provider_storage(request).get(provider_id)
    if row is None:
        raise NotFoundError(
            f"ArtifactStorageProvider {provider_id!r} does not exist",
        )
    return (
        await probe_artifact_provider(
            row,
            storage_provider=request.app.state.storage_provider,
        )
    ).as_dict()


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
                _form_field("addressing_style", "Addressing style", "enum", options=["auto", "path", "virtual"], help_="auto: path-style when an endpoint URL is set (MinIO and most S3-compatible servers), virtual-hosted on AWS."),
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
    on_pre_create=_pre_create,
    on_pre_update=_pre_update,
    on_pre_delete_id=_reject_reserved_delete,
)


__all__ = ["artifact_storage_helpers_router", "artifact_storage_router"]
