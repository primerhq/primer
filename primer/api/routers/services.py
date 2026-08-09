"""Service CRUD router.

Standard CRUD + Find from :mod:`primer.api.routers._crud`, plus two
guards from the Services spec (section 4.2 and section 8):

* Renaming a PUBLISHED service is a 409: the slug is the public
  ``/svc/{name}/`` URL, so once ``active_version_id`` is set the name is
  load-bearing. Unpublished rows rename freely.
* Deleting a service cascades to its versions and their bundle
  artifacts. Versions are immutable children with no independent
  lifecycle, so blocking (the ReferenceCheck approach) would just force
  operators to hand-delete rows they cannot edit anyway.

The publish / versions / activate routes are appended to this router by
task 5 of the implementation plan (they need the bundle pipeline).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from fastapi import Depends, HTTPException, Request
from pydantic import BaseModel

from primer.api.deps import (
    get_artifact_storage_registry,
    get_service_storage,
)
from primer.api.routers._crud import make_crud_router
from primer.model.providers.toolset import Toolset
from primer.model.service import Service, ServiceVersion
from primer.model.storage import FieldRef, OffsetPage, Op, Predicate, Value
from primer.service.bundle import BundleError, validate_bundle
from primer.service.publish import activate_version, publish_version
from primer.service.serve import invalidate_service_cache

if TYPE_CHECKING:
    from primer.int.storage import Storage


async def _reject_rename_when_published(
    entity: Service, existing: Service, request: Request
) -> None:
    """409 when the incoming body renames a published service."""
    if existing.active_version_id is not None and entity.name != existing.name:
        raise HTTPException(
            status_code=409,
            detail={
                "error": "published_service_rename",
                "field": "name",
                "message": (
                    f"service {existing.name!r} is published at "
                    f"/svc/{existing.name}/; the name is its public URL "
                    "and cannot change while a version is active"
                ),
            },
        )


async def _cascade_versions_and_artifacts(
    existing: Service, request: Request
) -> None:
    """Delete every version (and its artifacts) before the service row."""
    sp = request.app.state.storage_provider
    versions = sp.get_storage(ServiceVersion)
    try:
        artifacts = await get_artifact_storage_registry(request).get_default()
    except HTTPException:
        # No artifact backend configured: still cascade the rows. The
        # bytes live behind the same storage provider in the default
        # deployment, so this is a test-harness edge, not a leak path.
        artifacts = None
    predicate = Predicate(
        left=FieldRef(name="service_id"), op=Op.EQ, right=Value(value=existing.id)
    )
    while True:
        page = await versions.find(predicate, OffsetPage(offset=0, length=100))
        if not page.items:
            break
        for version in page.items:
            if artifacts is not None:
                for artifact_id in version.files.values():
                    await artifacts.delete(artifact_id)
            await versions.delete(version.id)


service_router = make_crud_router(
    model_cls=Service,
    storage_dep=get_service_storage,
    plural="services",
    tag="services",
    managed_by_field="harness_id",
    search_fields=["id", "name", "description"],
    on_pre_update=_reject_rename_when_published,
    on_pre_delete=_cascade_versions_and_artifacts,
)


# ---------------------------------------------------------------------------
# Publish / versions / activate (plan task 5; spec sections 5 and 8)
# ---------------------------------------------------------------------------


class _ActivateBody(BaseModel):
    version_id: str


async def _get_service_or_404(
    services: "Storage[Service]", service_id: str
) -> Service:
    service = await services.get(service_id)
    if service is None:
        raise HTTPException(
            status_code=404,
            detail={
                "error": "not_found",
                "message": f"Service {service_id!r} does not exist",
            },
        )
    return service


@service_router.post(
    "/services/{service_id}/versions",
    response_model=ServiceVersion,
    status_code=201,
    summary="Publish a bundle as a new service version",
)
async def publish_service_version(
    service_id: str,
    request: Request,
    activate: bool = True,
    services: "Storage[Service]" = Depends(get_service_storage),
) -> ServiceVersion:
    service = await _get_service_or_404(services, service_id)
    if service.harness_id is not None:
        raise HTTPException(
            status_code=409,
            detail={
                "error": "managed_by_harness",
                "message": (
                    f"service {service.name!r} is managed by harness "
                    f"{service.harness_id!r}; publish through the harness"
                ),
            },
        )
    tar_gz = await request.body()
    if not tar_gz:
        raise HTTPException(
            status_code=422,
            detail={
                "error": "invalid_bundle",
                "message": "the request body must be a gzipped tar bundle",
            },
        )
    try:
        bundle = validate_bundle(tar_gz)
    except BundleError as exc:
        raise HTTPException(
            status_code=422,
            detail={
                "error": "invalid_bundle",
                "field": exc.field,
                "lineno": exc.lineno,
                "message": str(exc),
            },
        ) from exc

    # Spec section 5 step 4: every granted toolset must resolve. Tool
    # names are deliberately NOT resolved here; a stale name 404s at
    # call time (phase 3).
    sp = request.app.state.storage_provider
    toolsets = sp.get_storage(Toolset)
    for grant in bundle.manifest.tools:
        if await toolsets.get(grant.toolset_id) is None:
            raise HTTPException(
                status_code=422,
                detail={
                    "error": "unknown_toolset",
                    "field": "tools",
                    "message": (
                        f"manifest grants toolset {grant.toolset_id!r}, "
                        "which does not exist"
                    ),
                },
            )

    artifacts = await get_artifact_storage_registry(request).get_default()
    created = await publish_version(
        sp, artifacts, service=service, bundle=bundle, activate=activate
    )
    invalidate_service_cache(request, service.name)
    return created


@service_router.get(
    "/services/{service_id}/versions",
    summary="List a service's published versions, newest first",
)
async def list_service_versions(
    service_id: str,
    request: Request,
    services: "Storage[Service]" = Depends(get_service_storage),
) -> dict:
    await _get_service_or_404(services, service_id)
    sp = request.app.state.storage_provider
    versions = sp.get_storage(ServiceVersion)
    predicate = Predicate(
        left=FieldRef(name="service_id"), op=Op.EQ, right=Value(value=service_id)
    )
    items: list[ServiceVersion] = []
    offset = 0
    while True:
        page = await versions.find(predicate, OffsetPage(offset=offset, length=100))
        items.extend(page.items)
        if len(page.items) < 100:
            break
        offset += 100
    items.sort(key=lambda v: v.version, reverse=True)
    return {"items": [v.model_dump(by_alias=True) for v in items]}


@service_router.post(
    "/services/{service_id}/_activate",
    response_model=Service,
    summary="Activate (or roll back to) a published version",
)
async def activate_service_version(
    service_id: str,
    body: _ActivateBody,
    request: Request,
    services: "Storage[Service]" = Depends(get_service_storage),
) -> Service:
    service = await _get_service_or_404(services, service_id)
    sp = request.app.state.storage_provider
    try:
        updated = await activate_version(
            sp, service=service, version_id=body.version_id
        )
        invalidate_service_cache(request, service.name)
        return updated
    except ValueError as exc:
        raise HTTPException(
            status_code=422,
            detail={
                "error": "version_mismatch",
                "field": "version_id",
                "message": str(exc),
            },
        ) from exc


__all__ = ["service_router"]
