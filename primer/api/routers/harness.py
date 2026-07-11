"""REST router for the Harness resource.

Endpoints:
  POST   /v1/harnesses                       Create row (DRAFT).
  GET    /v1/harnesses                       List with ?slug, ?status, ?direction filters.
  GET    /v1/harnesses/{id}                  Get one. 404 on miss.
  PUT    /v1/harnesses/{id}                  Update name/description/ref/subpath/git_token.
  DELETE /v1/harnesses/{id}                  Enqueue UNINSTALL. 202.
  PUT    /v1/harnesses/{id}/overrides        Validate + store overrides. 422 on invalid.
  PUT    /v1/harnesses/{id}/tracked_entities Outbound only — replace tracked_entities atomically.
  POST   /v1/harnesses/{id}/fetch            Enqueue FETCH. 202. Inbound only.
  POST   /v1/harnesses/{id}/install          Enqueue INSTALL. 202. Inbound only.
  POST   /v1/harnesses/{id}/sync             Enqueue SYNC. 202. Inbound only.
  POST   /v1/harnesses/{id}/build            Enqueue BUILD. 202. Outbound only.
  POST   /v1/harnesses/{id}/push             Enqueue PUSH. 202. Outbound only.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Annotated, Any
from uuid import uuid4

from fastapi import APIRouter, Depends, Path, Query, Request, Response, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from primer.api.deps import get_claim_engine, get_event_bus, get_storage_provider
from primer.api.errors import common_responses
from primer.api.pagination import parse_page
from primer.harness.hashes import hash_overrides
from primer.harness.outbound import (
    OutboundBuildError,
    build_bundle_targz,
    build_outbound,
)
from primer.model.except_ import ConflictError, NotFoundError
from primer.model.harness import (
    Harness,
    HarnessDirection,
    HarnessOperation,
    HarnessRendering,
    HarnessStatus,
    TrackedEntity,
)
from primer.model.storage import (
    OffsetPage,
    PageRequest,
)
from primer.storage.q import Q


harness_router = APIRouter(prefix="/v1/harnesses", tags=["harnesses"])


# ---------------------------------------------------------------------------
# Body models
# ---------------------------------------------------------------------------


class HarnessCreateBody(BaseModel):
    name: str = Field(..., min_length=1, max_length=200)
    slug: str = Field(..., min_length=2, max_length=64)
    git_url: str | None = Field(default=None, min_length=1)
    ref: str | None = Field(default=None, min_length=1)
    subpath: str | None = None
    git_token: str | None = None
    description: str | None = Field(default=None, max_length=2000)
    direction: HarnessDirection = HarnessDirection.INBOUND
    tracked_entities: list[TrackedEntity] = Field(default_factory=list)


class HarnessUpdateBody(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=2000)
    ref: str | None = Field(default=None, min_length=1)
    subpath: str | None = None
    git_url: str | None = Field(default=None, min_length=1)
    git_token: str | None = None


class TrackedEntitiesBody(BaseModel):
    tracked_entities: list[TrackedEntity] = Field(default_factory=list)


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
    status_code=201,
    summary="Create a new harness (DRAFT)",
    responses=common_responses(409, 422, 500),
)
async def create_harness(
    body: HarnessCreateBody,
    sp=Depends(get_storage_provider),
):
    storage = _get_harness_storage(sp)

    # Direction-aware validation
    if (
        body.direction == HarnessDirection.INBOUND
        and body.tracked_entities
    ):
        return JSONResponse(
            status_code=422,
            content={
                "code": "tracked_entities_on_inbound",
                "detail": (
                    "tracked_entities may only be supplied when "
                    "direction='outbound'"
                ),
            },
        )

    if body.direction == HarnessDirection.OUTBOUND:
        seen: set[str] = set()
        for te in body.tracked_entities:
            if te.template_name in seen:
                return JSONResponse(
                    status_code=422,
                    content={
                        "code": "outbound_template_name_collision",
                        "detail": (
                            f"duplicate template_name {te.template_name!r} "
                            f"in tracked_entities"
                        ),
                    },
                )
            seen.add(te.template_name)

    # Inbound harnesses install FROM a git repo, so a remote is mandatory.
    # Outbound harnesses render from the live DB and can be consumed via the
    # bundle tarball, so git is optional — a push target only if the user wants
    # one.
    if body.direction == HarnessDirection.INBOUND and not body.git_url:
        return JSONResponse(
            status_code=422,
            content={
                "code": "git_url_required_inbound",
                "detail": (
                    "inbound harnesses install from a git repo, so git_url "
                    "is required"
                ),
            },
        )

    # Enforce slug uniqueness
    slug_pred = Q(Harness).where("slug", body.slug).build()
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
        direction=body.direction,
        tracked_entities=list(body.tracked_entities),
        created_at=datetime.now(timezone.utc),
    )
    created = await storage.create(harness)
    return JSONResponse(
        status_code=201,
        content=_harness_to_json(created),
    )


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
    direction: Annotated[
        HarnessDirection | None,
        Query(description="Filter by direction."),
    ] = None,
    page: PageRequest = Depends(parse_page),
    sp=Depends(get_storage_provider),
):
    storage = _get_harness_storage(sp)

    if slug is None and status_filter is None and direction is None:
        return await storage.list(page)

    q = Q(Harness)
    if slug is not None:
        q = q.where("slug", slug)
    if status_filter is not None:
        q = q.where("status", status_filter.value)
    if direction is not None:
        q = q.where("direction", direction.value)
    return await storage.find(q.build(), page)


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
    if "git_url" in body.model_fields_set:
        # Explicitly provided (possibly null). Outbound harnesses may clear
        # their remote (git is optional); inbound must always keep one to
        # install from.
        if (
            body.git_url is None
            and harness.direction == HarnessDirection.INBOUND
        ):
            return JSONResponse(
                status_code=422,
                content={
                    "code": "git_url_required_inbound",
                    "detail": (
                        "inbound harnesses require a git_url; it cannot be "
                        "cleared"
                    ),
                },
            )
        harness.git_url = body.git_url
        if body.git_url is None:
            # Clearing the remote forgets the push history: those stamps point
            # at a repo we're no longer publishing to, so a later build reads
            # DRAFT (never-pushed) rather than OUTDATED (drifted-from-remote).
            # (Inbound already returned 422 above, so this is outbound-only.)
            harness.last_pushed_commit = None
            harness.last_pushed_bundle_hash = None
            harness.last_pushed_at = None

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
    cascade: bool | None = Query(
        None,
        description=(
            "Also delete the harness's tracked/managed entities (agents, "
            "graphs, collections, documents, toolsets). Omitted: defaults by "
            "direction — inbound (installed) harnesses cascade so uninstall "
            "removes the installed objects; outbound harnesses do NOT, so "
            "deleting one keeps the objects it merely tracks. Pass true/false "
            "to override."
        ),
    ),
    sp=Depends(get_storage_provider),
    event_bus=Depends(get_event_bus),
    engine=Depends(get_claim_engine),
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
    # Explicit toggle wins; when omitted, default by direction — inbound
    # (installed) harnesses cascade, outbound harnesses keep their tracked
    # objects.
    harness.uninstall_cascade = (
        cascade if cascade is not None
        else harness.direction == HarnessDirection.INBOUND
    )
    updated = await storage.update(harness)
    await event_bus.publish("harness-claimable", {"harness_id": harness_id})
    # Also notify the ClaimEngine (forward-compat; no-op when not wired).
    if engine is not None:
        from primer.int.claim import ClaimKind
        await engine.upsert(ClaimKind.HARNESS, harness_id, priority=10)
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
    engine=Depends(get_claim_engine),
):
    storage = _get_harness_storage(sp)
    harness = await storage.get(harness_id)
    if harness is None:
        raise NotFoundError(f"Harness {harness_id!r} does not exist")

    if harness.direction == HarnessDirection.OUTBOUND:
        return JSONResponse(
            status_code=409,
            content={
                "code": "direction_mismatch",
                "detail": (
                    f"Harness {harness_id!r} is outbound; "
                    "fetch is an inbound operation"
                ),
            },
        )

    if harness.pending_operation is not None:
        raise ConflictError(
            f"Harness {harness_id!r} already has a pending operation: "
            f"{harness.pending_operation.value!r}"
        )

    harness.pending_operation = HarnessOperation.FETCH
    updated = await storage.update(harness)
    await event_bus.publish("harness-claimable", {"harness_id": harness_id})
    # Also notify the ClaimEngine (forward-compat; no-op when not wired).
    if engine is not None:
        from primer.int.claim import ClaimKind
        await engine.upsert(ClaimKind.HARNESS, harness_id, priority=10)
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
    engine=Depends(get_claim_engine),
):
    storage = _get_harness_storage(sp)
    harness = await storage.get(harness_id)
    if harness is None:
        raise NotFoundError(f"Harness {harness_id!r} does not exist")

    if harness.direction == HarnessDirection.OUTBOUND:
        return JSONResponse(
            status_code=409,
            content={
                "code": "direction_mismatch",
                "detail": (
                    f"Harness {harness_id!r} is outbound; "
                    "install is an inbound operation"
                ),
            },
        )

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
    # Also notify the ClaimEngine (forward-compat; no-op when not wired).
    if engine is not None:
        from primer.int.claim import ClaimKind
        await engine.upsert(ClaimKind.HARNESS, harness_id, priority=10)
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
    engine=Depends(get_claim_engine),
):
    storage = _get_harness_storage(sp)
    harness = await storage.get(harness_id)
    if harness is None:
        raise NotFoundError(f"Harness {harness_id!r} does not exist")

    if harness.direction == HarnessDirection.OUTBOUND:
        return JSONResponse(
            status_code=409,
            content={
                "code": "direction_mismatch",
                "detail": (
                    f"Harness {harness_id!r} is outbound; "
                    "sync is an inbound operation"
                ),
            },
        )

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
    # Also notify the ClaimEngine (forward-compat; no-op when not wired).
    if engine is not None:
        from primer.int.claim import ClaimKind
        await engine.upsert(ClaimKind.HARNESS, harness_id, priority=10)
    return JSONResponse(
        status_code=202,
        content=_harness_to_json(updated),
    )


# ---------------------------------------------------------------------------
# PUT /v1/harnesses/{id}/tracked_entities  — outbound only
# ---------------------------------------------------------------------------


@harness_router.put(
    "/{harness_id}/tracked_entities",
    summary="Replace tracked_entities on an outbound harness",
    responses=common_responses(404, 409, 422, 500),
)
async def put_tracked_entities(
    body: TrackedEntitiesBody,
    harness_id: str = Path(...),
    sp=Depends(get_storage_provider),
):
    storage = _get_harness_storage(sp)
    harness = await storage.get(harness_id)
    if harness is None:
        raise NotFoundError(f"Harness {harness_id!r} does not exist")

    if harness.direction != HarnessDirection.OUTBOUND:
        return JSONResponse(
            status_code=409,
            content={
                "code": "direction_mismatch",
                "detail": (
                    f"Harness {harness_id!r} is inbound; "
                    "tracked_entities can only be edited on outbound harnesses"
                ),
            },
        )

    seen: set[str] = set()
    for te in body.tracked_entities:
        if te.template_name in seen:
            return JSONResponse(
                status_code=422,
                content={
                    "code": "outbound_template_name_collision",
                    "detail": (
                        f"duplicate template_name {te.template_name!r} "
                        f"in tracked_entities"
                    ),
                },
            )
        seen.add(te.template_name)

    # Tracked set changed → re-BUILD required. Clear bundle_hash and
    # bring status back to DRAFT so the worker re-renders before the
    # next push.
    harness.tracked_entities = list(body.tracked_entities)
    harness.bundle_hash = None
    harness.status = HarnessStatus.DRAFT
    updated = await storage.update(harness)
    return JSONResponse(
        status_code=200,
        content=_harness_to_json(updated),
    )


# ---------------------------------------------------------------------------
# POST /v1/harnesses/{id}/build  — enqueue BUILD (outbound only)
# ---------------------------------------------------------------------------


@harness_router.post(
    "/{harness_id}/build",
    summary="Enqueue BUILD for an outbound harness (202)",
    responses=common_responses(404, 409, 422, 500),
)
async def build_harness(
    harness_id: str = Path(...),
    sp=Depends(get_storage_provider),
    event_bus=Depends(get_event_bus),
    engine=Depends(get_claim_engine),
):
    storage = _get_harness_storage(sp)
    harness = await storage.get(harness_id)
    if harness is None:
        raise NotFoundError(f"Harness {harness_id!r} does not exist")

    if harness.direction != HarnessDirection.OUTBOUND:
        return JSONResponse(
            status_code=409,
            content={
                "code": "direction_mismatch",
                "detail": (
                    f"Harness {harness_id!r} is inbound; "
                    "build is an outbound operation"
                ),
            },
        )

    if harness.pending_operation is not None:
        return JSONResponse(
            status_code=409,
            content={
                "code": "operation_in_flight",
                "detail": (
                    f"Harness {harness_id!r} already has a pending operation: "
                    f"{harness.pending_operation.value!r}"
                ),
            },
        )

    if not harness.tracked_entities:
        return JSONResponse(
            status_code=422,
            content={
                "code": "outbound_no_entities",
                "detail": "No tracked entities; nothing to build",
            },
        )

    harness.pending_operation = HarnessOperation.BUILD
    updated = await storage.update(harness)
    await event_bus.publish("harness-claimable", {"harness_id": harness_id})
    if engine is not None:
        from primer.int.claim import ClaimKind
        await engine.upsert(ClaimKind.HARNESS, harness_id, priority=10)
    return JSONResponse(
        status_code=202,
        content=_harness_to_json(updated),
    )


# ---------------------------------------------------------------------------
# GET /v1/harnesses/{id}/bundle.tar.gz  — download the bundle (outbound only)
# ---------------------------------------------------------------------------


@harness_router.get(
    "/{harness_id}/bundle.tar.gz",
    summary="Download an outbound harness bundle as a gzipped tarball",
    responses=common_responses(404, 409, 422, 500),
)
async def download_harness_bundle(
    harness_id: str = Path(...),
    sp=Depends(get_storage_provider),
):
    storage = _get_harness_storage(sp)
    harness = await storage.get(harness_id)
    if harness is None:
        raise NotFoundError(f"Harness {harness_id!r} does not exist")

    if harness.direction != HarnessDirection.OUTBOUND:
        return JSONResponse(
            status_code=409,
            content={
                "code": "direction_mismatch",
                "detail": (
                    f"Harness {harness_id!r} is inbound; "
                    "bundle download is an outbound operation"
                ),
            },
        )

    # Build fresh from the live DB (the source of truth for an outbound
    # harness) and stream it — no queue, no worker, no ephemeral pod fs.
    try:
        result = await build_outbound(harness, storage_provider=sp)
    except OutboundBuildError as exc:
        return JSONResponse(
            status_code=422,
            content={"code": exc.code, "detail": exc.message},
        )

    tar_gz = build_bundle_targz(result)
    filename = f"{harness.slug}-{result.bundle_hash[:12]}.tar.gz"
    return Response(
        content=tar_gz,
        media_type="application/gzip",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


# ---------------------------------------------------------------------------
# POST /v1/harnesses/{id}/push  — enqueue PUSH (outbound only)
# ---------------------------------------------------------------------------


@harness_router.post(
    "/{harness_id}/push",
    summary="Enqueue PUSH for an outbound harness (202)",
    responses=common_responses(404, 409, 422, 500),
)
async def push_harness(
    harness_id: str = Path(...),
    sp=Depends(get_storage_provider),
    event_bus=Depends(get_event_bus),
    engine=Depends(get_claim_engine),
):
    storage = _get_harness_storage(sp)
    harness = await storage.get(harness_id)
    if harness is None:
        raise NotFoundError(f"Harness {harness_id!r} does not exist")

    if harness.direction != HarnessDirection.OUTBOUND:
        return JSONResponse(
            status_code=409,
            content={
                "code": "direction_mismatch",
                "detail": (
                    f"Harness {harness_id!r} is inbound; "
                    "push is an outbound operation"
                ),
            },
        )

    if harness.pending_operation is not None:
        return JSONResponse(
            status_code=409,
            content={
                "code": "operation_in_flight",
                "detail": (
                    f"Harness {harness_id!r} already has a pending operation: "
                    f"{harness.pending_operation.value!r}"
                ),
            },
        )

    if not harness.tracked_entities:
        return JSONResponse(
            status_code=422,
            content={
                "code": "outbound_no_entities",
                "detail": "No tracked entities; nothing to push",
            },
        )

    if not harness.git_url:
        return JSONResponse(
            status_code=422,
            content={
                "code": "git_remote_not_configured",
                "detail": (
                    "This harness has no git_url configured; set one before "
                    "pushing, or download the bundle via /bundle.tar.gz"
                ),
            },
        )

    harness.pending_operation = HarnessOperation.PUSH
    updated = await storage.update(harness)
    await event_bus.publish("harness-claimable", {"harness_id": harness_id})
    if engine is not None:
        from primer.int.claim import ClaimKind
        await engine.upsert(ClaimKind.HARNESS, harness_id, priority=10)
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
