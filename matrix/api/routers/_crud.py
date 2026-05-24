"""Generic CRUD + Find router factory.

Each persisted entity (LLMProvider, EmbeddingProvider, Toolset, Tool,
Agent, Graph, Collection, Document, VectorStoreConfig) shares the
same five HTTP shapes:

* ``POST   /v1/<plural>``           -- create, returns 201
* ``GET    /v1/<plural>/{id}``      -- get-by-id, 404 on miss
* ``PUT    /v1/<plural>/{id}``      -- replace, 404 on miss; body id MUST match path id
* ``DELETE /v1/<plural>/{id}``      -- delete, 404 on miss
* ``GET    /v1/<plural>``           -- list with pagination + ordering
* ``POST   /v1/<plural>/find``      -- find with predicate (POST body)

:func:`make_crud_router` returns an APIRouter pre-wired to the storage
helper of the caller's choosing, with optional ``on_mutate`` callback
invoked after every successful create / update / delete (used by
provider routers to invalidate cached adapters).
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any, TypeVar

from fastapi import APIRouter, Body, Depends, Path, Request, status

from matrix.api.errors import common_responses
from matrix.api.pagination import FindRequest, parse_order_by, parse_page
from matrix.model.common import Identifiable
from matrix.model.except_ import ConflictError, NotFoundError
from matrix.model.storage import (
    CursorPageResponse,
    OffsetPageResponse,
    OrderBy,
    PageRequest,
)


_ModelT = TypeVar("_ModelT", bound=Identifiable)


# Page response is a runtime union of the two concrete page shapes;
# FastAPI/Pydantic serializes it via the discriminator on ``kind``.
_PageResp = OffsetPageResponse[Any] | CursorPageResponse[Any]


# Callback signature: ``(entity_id, request) -> None``. The Request is
# threaded through so callbacks can reach ``request.app.state`` for
# the per-request ProviderRegistry / SemanticSearchRegistry.
_OnMutateHook = Callable[[str, Request], Awaitable[None]] | None

# Pre-write hook signature: ``(entity, request) -> None``.
# Called BEFORE the storage write so the hook can raise HTTPException
# to abort the operation (e.g. reference-integrity checks).
_OnPreWriteHook = Callable[[Any, Request], Awaitable[None]] | None

# Pre-update hook signature: ``(entity, existing, request) -> None``.
# Called BEFORE storage.update with both the new entity body and the
# prior stored row, enabling immutability checks.
_OnPreUpdateHook = (
    Callable[[Any, Any, Request], Awaitable[None]] | None
)


def make_crud_router(
    *,
    model_cls: type[_ModelT],
    storage_dep: Callable[..., Any],
    plural: str,
    tag: str,
    on_create: _OnMutateHook = None,
    on_update: _OnMutateHook = None,
    on_delete: _OnMutateHook = None,
    on_pre_create: _OnPreWriteHook = None,
    on_pre_update: _OnPreUpdateHook = None,
    extra_get_responses: dict[int, dict[str, Any]] | None = None,
) -> APIRouter:
    """Build a CRUD + Find APIRouter for ``model_cls``.

    Parameters
    ----------
    model_cls
        The persisted Pydantic model. Used as the request/response body type.
    storage_dep
        FastAPI dependency that returns ``Storage[model_cls]``. Typically
        one of the per-model helpers in :mod:`matrix.api.deps`.
    plural
        URL path segment, e.g. ``llm_providers``.
    tag
        OpenAPI tag for the router (groups endpoints in /docs).
    on_create / on_update / on_delete
        Async callables ``async def(entity_id: str, request: Request) -> None``
        invoked after each successful mutation. Used by provider routers to
        invalidate cached adapters in :class:`ProviderRegistry`.
    on_pre_create
        Async callable ``async def(entity, request: Request) -> None``
        invoked BEFORE ``storage.create()``. Raise :class:`HTTPException`
        to abort the create (e.g. reference-integrity checks).
    on_pre_update
        Async callable
        ``async def(entity, existing, request: Request) -> None``
        invoked BEFORE ``storage.update()`` with both the new entity body
        and the prior stored row. Raise :class:`HTTPException` to abort
        the update (e.g. immutability checks).
    extra_get_responses
        Extra response codes documented for the GET-by-id route (in
        addition to the standard 404).
    """

    router = APIRouter(tags=[tag])

    # ---- POST /<plural>  (create) ---------------------------------------
    @router.post(
        f"/{plural}",
        response_model=model_cls,
        status_code=status.HTTP_201_CREATED,
        summary=f"Create {model_cls.__name__}",
        responses=common_responses(409, 422, 500),
    )
    async def _create(
        request: Request,
        entity: model_cls = Body(..., description=f"{model_cls.__name__} body"),  # type: ignore[valid-type,assignment]
        storage=Depends(storage_dep),
    ) -> _ModelT:
        existing = await storage.get(entity.id)
        if existing is not None:
            raise ConflictError(
                f"{model_cls.__name__} with id {entity.id!r} already exists"
            )
        if on_pre_create is not None:
            await on_pre_create(entity, request)
        created = await storage.create(entity)
        if on_create is not None:
            await on_create(created.id, request)
        return created

    # `from __future__ import annotations` turned ``entity``'s annotation
    # into the string "model_cls"; FastAPI cannot resolve that forward
    # ref in a closure scope. Bind the real class explicitly so the body
    # parser sees the concrete Pydantic model.
    _create.__annotations__["entity"] = model_cls

    # ---- GET /<plural>/{id}  (read) -------------------------------------
    get_responses = common_responses(404, 500)
    if extra_get_responses:
        get_responses = {**get_responses, **extra_get_responses}

    @router.get(
        f"/{plural}/{{entity_id}}",
        response_model=model_cls,
        summary=f"Get {model_cls.__name__} by id",
        responses=get_responses,
    )
    async def _get(
        entity_id: str = Path(..., description="Entity id."),
        storage=Depends(storage_dep),
    ) -> _ModelT:
        row = await storage.get(entity_id)
        if row is None:
            raise NotFoundError(
                f"{model_cls.__name__} {entity_id!r} does not exist"
            )
        return row

    # ---- PUT /<plural>/{id}  (replace) ----------------------------------
    @router.put(
        f"/{plural}/{{entity_id}}",
        response_model=model_cls,
        summary=f"Replace {model_cls.__name__}",
        responses=common_responses(404, 409, 422, 500),
    )
    async def _update(
        request: Request,
        entity: model_cls = Body(..., description=f"{model_cls.__name__} body"),  # type: ignore[valid-type,assignment]
        entity_id: str = Path(..., description="Entity id."),
        storage=Depends(storage_dep),
    ) -> _ModelT:
        if entity.id != entity_id:
            raise ConflictError(
                f"path id {entity_id!r} does not match body id {entity.id!r}"
            )
        existing = await storage.get(entity_id)
        if existing is None:
            raise NotFoundError(
                f"{model_cls.__name__} {entity_id!r} does not exist"
            )
        if on_pre_update is not None:
            await on_pre_update(entity, existing, request)
        updated = await storage.update(entity)
        if on_update is not None:
            await on_update(updated.id, request)
        return updated

    _update.__annotations__["entity"] = model_cls

    # ---- DELETE /<plural>/{id} ------------------------------------------
    @router.delete(
        f"/{plural}/{{entity_id}}",
        status_code=status.HTTP_204_NO_CONTENT,
        summary=f"Delete {model_cls.__name__}",
        responses=common_responses(404, 500),
    )
    async def _delete(
        request: Request,
        entity_id: str = Path(..., description="Entity id."),
        storage=Depends(storage_dep),
    ) -> None:
        existing = await storage.get(entity_id)
        if existing is None:
            raise NotFoundError(
                f"{model_cls.__name__} {entity_id!r} does not exist"
            )
        if on_delete is not None:
            await on_delete(entity_id, request)
        await storage.delete(entity_id)

    # ---- GET /<plural>  (list) ------------------------------------------
    @router.get(
        f"/{plural}",
        summary=f"List {model_cls.__name__}",
        responses=common_responses(400, 422, 500),
    )
    async def _list(
        page: PageRequest = Depends(parse_page),
        order_by: list[OrderBy] | None = Depends(parse_order_by),
        storage=Depends(storage_dep),
    ) -> _PageResp:
        return await storage.list(page, order_by=order_by)

    # ---- POST /<plural>/find  (find with predicate) ---------------------
    @router.post(
        f"/{plural}/find",
        summary=f"Find {model_cls.__name__} with predicate",
        responses=common_responses(400, 422, 500),
    )
    async def _find(
        body: FindRequest,
        storage=Depends(storage_dep),
    ) -> _PageResp:
        return await storage.find(
            body.predicate, body.page, order_by=body.order_by
        )

    return router


__all__ = ["make_crud_router"]
