"""REST router for the Harness resource.

Endpoints:
  POST   /v1/harnesses                  Create row (DRAFT).
  GET    /v1/harnesses                  List with ?slug, ?status filters.
  GET    /v1/harnesses/{id}             Get one. 404 on miss.
  PUT    /v1/harnesses/{id}             Update name/description/ref/subpath/git_token.
  DELETE /v1/harnesses/{id}             Enqueue UNINSTALL. 202.
  PUT    /v1/harnesses/{id}/overrides   Validate + store overrides. 422 on invalid.
  POST   /v1/harnesses/{id}/fetch       Enqueue FETCH. 202.
  POST   /v1/harnesses/{id}/install     Enqueue INSTALL. 202.
  POST   /v1/harnesses/{id}/sync        Enqueue SYNC. 202.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Annotated, Any
from uuid import uuid4

from fastapi import APIRouter, Depends, Path, Query, Request, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from matrix.api.deps import get_event_bus, get_storage_provider
from matrix.api.errors import common_responses
from matrix.api.pagination import parse_page
from matrix.harness.hashes import hash_overrides
from matrix.model.except_ import ConflictError, NotFoundError
from matrix.model.harness import Harness, HarnessOperation, HarnessStatus, HarnessRendering
from matrix.model.storage import (
    FieldRef,
    Op,
    OffsetPage,
    PageRequest,
    Predicate,
    Value,
)


harness_router = APIRouter(prefix="/v1/harnesses", tags=["harnesses"])


# ---------------------------------------------------------------------------
# Body models
# ---------------------------------------------------------------------------


class HarnessCreateBody(BaseModel):
    name: str = Field(..., min_length=1, max_length=200)
    slug: str = Field(..., min_length=2, max_length=64)
    git_url: str = Field(..., min_length=1)
    ref: str | None = Field(default=None, min_length=1)
    subpath: str | None = None
    git_token: str | None = None
    description: str | None = Field(default=None, max_length=2000)


class HarnessUpdateBody(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=2000)
    ref: str | None = Field(default=None, min_length=1)
    subpath: str | None = None
    git_token: str | None = None


# ---------------------------------------------------------------------------
# Storage helper
# ---------------------------------------------------------------------------


def _get_harness_storage(sp):
    return sp.get_storage(Harness)


# ---------------------------------------------------------------------------
# POST /v1/harnesses  — create DRAFT
# ---------------------------------------------------------------------------


@harness_router.post(
    "",
    response_model=Harness,
    status_code=201,
    summary="Create a new harness (DRAFT)",
    responses=common_responses(409, 422, 500),
)
async def create_harness(
    body: HarnessCreateBody,
    sp=Depends(get_storage_provider),
) -> Harness:
    storage = _get_harness_storage(sp)

    # Enforce slug uniqueness
    slug_pred = Predicate(
        left=FieldRef(name="slug"),
        op=Op.EQ,
        right=Value(value=body.slug),
    )
    existing_page = await storage.find(slug_pred, OffsetPage(offset=0, length=1))
    items = list(getattr(existing_page, "items", []))
    if items:
        raise ConflictError(
            f"A harness with slug {body.slug!r} already exists"
        )

    harness_id = f"hns_{uuid4().hex[:12]}"
    from pydantic import SecretStr

    harness = Harness(
        id=harness_id,
        slug=body.slug,
        name=body.name,
        description=body.description,
        git_url=body.git_url,
        git_token=SecretStr(body.git_token) if body.git_token else None,
        ref=body.ref or "main",
        subpath=body.subpath,
        status=HarnessStatus.DRAFT,
        created_at=datetime.now(timezone.utc),
    )
    return await storage.create(harness)


# ---------------------------------------------------------------------------
# GET /v1/harnesses  — list
# ---------------------------------------------------------------------------


@harness_router.get(
    "",
    summary="List harnesses",
    responses=common_responses(400, 422, 500),
)
async def list_harnesses(
    slug: Annotated[str | None, Query(description="Filter by slug.")] = None,
    status_filter: Annotated[
        HarnessStatus | None,
        Query(alias="status", description="Filter by status."),
    ] = None,
    page: PageRequest = Depends(parse_page),
    sp=Depends(get_storage_provider),
):
    storage = _get_harness_storage(sp)

    predicates: list[Predicate] = []
    if slug is not None:
        predicates.append(
            Predicate(left=FieldRef(name="slug"), op=Op.EQ, right=Value(value=slug))
        )
    if status_filter is not None:
        predicates.append(
            Predicate(
                left=FieldRef(name="status"),
                op=Op.EQ,
                right=Value(value=status_filter.value),
            )
        )

    if not predicates:
        return await storage.list(page)

    pred = predicates[0]
    for p in predicates[1:]:
        pred = Predicate(left=pred, op=Op.AND, right=p)
    return await storage.find(pred, page)


# ---------------------------------------------------------------------------
# GET /v1/harnesses/{id}  — get one
# ---------------------------------------------------------------------------


@harness_router.get(
    "/{harness_id}",
    response_model=Harness,
    summary="Get a harness by id",
    responses=common_responses(404, 500),
)
async def get_harness(
    harness_id: str = Path(...),
    sp=Depends(get_storage_provider),
) -> Harness:
    storage = _get_harness_storage(sp)
    harness = await storage.get(harness_id)
    if harness is None:
        raise NotFoundError(f"Harness {harness_id!r} does not exist")
    return harness


# ---------------------------------------------------------------------------
# PUT /v1/harnesses/{id}  — partial update
# ---------------------------------------------------------------------------


@harness_router.put(
    "/{harness_id}",
    response_model=Harness,
    summary="Update harness fields",
    responses=common_responses(404, 422, 500),
)
async def update_harness(
    body: HarnessUpdateBody,
    harness_id: str = Path(...),
    sp=Depends(get_storage_provider),
) -> Harness:
    storage = _get_harness_storage(sp)
    harness = await storage.get(harness_id)
    if harness is None:
        raise NotFoundError(f"Harness {harness_id!r} does not exist")

    from pydantic import SecretStr

    overrides_dirty = harness.overrides_dirty

    if body.name is not None:
        harness.name = body.name
    if body.description is not None:
        harness.description = body.description
    if body.ref is not None and body.ref != harness.ref:
        harness.ref = body.ref
        overrides_dirty = True
    if body.subpath is not None and body.subpath != harness.subpath:
        harness.subpath = body.subpath
        overrides_dirty = True
    if body.git_token is not None:
        harness.git_token = SecretStr(body.git_token)

    harness.overrides_dirty = overrides_dirty
    return await storage.update(harness)


# ---------------------------------------------------------------------------
# DELETE /v1/harnesses/{id}  — enqueue UNINSTALL, 202
# ---------------------------------------------------------------------------


@harness_router.delete(
    "/{harness_id}",
    summary="Enqueue UNINSTALL for a harness (202)",
    responses=common_responses(404, 409, 500),
)
async def delete_harness(
    harness_id: str = Path(...),
    sp=Depends(get_storage_provider),
    event_bus=Depends(get_event_bus),
):
    storage = _get_harness_storage(sp)
    harness = await storage.get(harness_id)
    if harness is None:
        raise NotFoundError(f"Harness {harness_id!r} does not exist")

    if harness.pending_operation is not None:
        raise ConflictError(
            f"Harness {harness_id!r} already has a pending operation: "
            f"{harness.pending_operation.value!r}"
        )

    harness.pending_operation = HarnessOperation.UNINSTALL
    updated = await storage.update(harness)
    await event_bus.publish("harness-claimable", {"harness_id": harness_id})
    return JSONResponse(
        status_code=202,
        content=_harness_to_json(updated),
    )


# ---------------------------------------------------------------------------
# PUT /v1/harnesses/{id}/overrides  — validate + store overrides
# ---------------------------------------------------------------------------


@harness_router.put(
    "/{harness_id}/overrides",
    response_model=Harness,
    summary="Set harness overrides (validates against cached schema)",
    responses=common_responses(404, 422, 500),
)
async def put_harness_overrides(
    request: Request,
    harness_id: str = Path(...),
    sp=Depends(get_storage_provider),
) -> Harness:
    storage = _get_harness_storage(sp)
    harness = await storage.get(harness_id)
    if harness is None:
        raise NotFoundError(f"Harness {harness_id!r} does not exist")

    overrides_body: dict[str, Any] = await request.json()

    if harness.overrides_schema is None:
        return JSONResponse(  # type: ignore[return-value]
            status_code=422,
            content={"code": "overrides_schema_missing", "detail": "No overrides schema cached for this harness"},
        )

    # Validate against the schema
    try:
        import jsonschema
        jsonschema.validate(instance=overrides_body, schema=harness.overrides_schema)
    except Exception as exc:
        errors = []
        if hasattr(exc, "message"):
            errors.append(str(exc.message))
        else:
            errors.append(str(exc))
        return JSONResponse(  # type: ignore[return-value]
            status_code=422,
            content={"code": "overrides_invalid", "errors": errors},
        )

    harness.overrides = overrides_body
    harness.overrides_hash = hash_overrides(overrides_body)

    # Recompute overrides_dirty by comparing to the HarnessRendering snapshot
    rendering_storage = sp.get_storage(HarnessRendering)
    rendering = await rendering_storage.get(harness_id)
    if rendering is not None:
        harness.overrides_dirty = harness.overrides_hash != rendering.overrides_hash
    else:
        harness.overrides_dirty = False

    return await storage.update(harness)


# ---------------------------------------------------------------------------
# POST /v1/harnesses/{id}/fetch  — enqueue FETCH
# ---------------------------------------------------------------------------


@harness_router.post(
    "/{harness_id}/fetch",
    summary="Enqueue FETCH for a harness (202)",
    responses=common_responses(404, 409, 500),
)
async def fetch_harness(
    harness_id: str = Path(...),
    sp=Depends(get_storage_provider),
    event_bus=Depends(get_event_bus),
):
    storage = _get_harness_storage(sp)
    harness = await storage.get(harness_id)
    if harness is None:
        raise NotFoundError(f"Harness {harness_id!r} does not exist")

    if harness.pending_operation is not None:
        raise ConflictError(
            f"Harness {harness_id!r} already has a pending operation: "
            f"{harness.pending_operation.value!r}"
        )

    harness.pending_operation = HarnessOperation.FETCH
    updated = await storage.update(harness)
    await event_bus.publish("harness-claimable", {"harness_id": harness_id})
    return JSONResponse(
        status_code=202,
        content=_harness_to_json(updated),
    )


# ---------------------------------------------------------------------------
# POST /v1/harnesses/{id}/install  — enqueue INSTALL
# ---------------------------------------------------------------------------


@harness_router.post(
    "/{harness_id}/install",
    summary="Enqueue INSTALL for a harness (202)",
    responses=common_responses(404, 409, 422, 500),
)
async def install_harness(
    harness_id: str = Path(...),
    sp=Depends(get_storage_provider),
    event_bus=Depends(get_event_bus),
):
    storage = _get_harness_storage(sp)
    harness = await storage.get(harness_id)
    if harness is None:
        raise NotFoundError(f"Harness {harness_id!r} does not exist")

    if harness.pending_operation is not None:
        raise ConflictError(
            f"Harness {harness_id!r} already has a pending operation: "
            f"{harness.pending_operation.value!r}"
        )

    _INSTALL_ALLOWED = {HarnessStatus.DRAFT, HarnessStatus.READY, HarnessStatus.OUTDATED}
    if harness.status not in _INSTALL_ALLOWED:
        raise ConflictError(
            f"Harness {harness_id!r} status is {harness.status.value!r}; "
            f"install requires one of {[s.value for s in _INSTALL_ALLOWED]}"
        )

    if harness.overrides_schema is None:
        return JSONResponse(
            status_code=422,
            content={
                "code": "overrides_schema_missing",
                "detail": "No overrides schema cached; run fetch first",
            },
        )

    # Always validate the current overrides against the schema — even if
    # overrides is an empty dict, the schema may declare required fields
    # so empty input is invalid input, not a no-op skip.
    try:
        import jsonschema
        jsonschema.validate(
            instance=harness.overrides, schema=harness.overrides_schema,
        )
    except Exception as exc:
        errors = []
        if hasattr(exc, "message"):
            errors.append(str(exc.message))
        else:
            errors.append(str(exc))
        return JSONResponse(
            status_code=422,
            content={"code": "overrides_invalid", "errors": errors},
        )

    harness.pending_operation = HarnessOperation.INSTALL
    updated = await storage.update(harness)
    await event_bus.publish("harness-claimable", {"harness_id": harness_id})
    return JSONResponse(
        status_code=202,
        content=_harness_to_json(updated),
    )


# ---------------------------------------------------------------------------
# POST /v1/harnesses/{id}/sync  — enqueue SYNC
# ---------------------------------------------------------------------------


@harness_router.post(
    "/{harness_id}/sync",
    summary="Enqueue SYNC for a harness (202)",
    responses=common_responses(404, 409, 422, 500),
)
async def sync_harness(
    harness_id: str = Path(...),
    sp=Depends(get_storage_provider),
    event_bus=Depends(get_event_bus),
):
    storage = _get_harness_storage(sp)
    harness = await storage.get(harness_id)
    if harness is None:
        raise NotFoundError(f"Harness {harness_id!r} does not exist")

    if harness.pending_operation is not None:
        raise ConflictError(
            f"Harness {harness_id!r} already has a pending operation: "
            f"{harness.pending_operation.value!r}"
        )

    _SYNC_ALLOWED = {HarnessStatus.INSTALLED, HarnessStatus.OUTDATED}
    if harness.status not in _SYNC_ALLOWED:
        raise ConflictError(
            f"Harness {harness_id!r} status is {harness.status.value!r}; "
            f"sync requires one of {[s.value for s in _SYNC_ALLOWED]}"
        )

    if harness.available_bundle_hash is None:
        return JSONResponse(
            status_code=422,
            content={
                "code": "fetch_required",
                "detail": "No bundle fetched yet; run fetch first",
            },
        )

    harness.pending_operation = HarnessOperation.SYNC
    updated = await storage.update(harness)
    await event_bus.publish("harness-claimable", {"harness_id": harness_id})
    return JSONResponse(
        status_code=202,
        content=_harness_to_json(updated),
    )


# ---------------------------------------------------------------------------
# Serialisation helper
# ---------------------------------------------------------------------------


def _harness_to_json(harness: Harness) -> dict:
    """Serialize a Harness to a JSON-safe dict.

    Uses Pydantic's model_dump so SecretStr fields are auto-redacted.
    """
    return harness.model_dump(mode="json")


__all__ = ["harness_router"]
