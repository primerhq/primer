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

from collections.abc import Awaitable, Callable, Sequence
from typing import Any, TypeVar

from fastapi import APIRouter, Body, Depends, HTTPException, Path, Query, Request, status
from fastapi.exceptions import RequestValidationError
from pydantic import ValidationError as PydanticValidationError

from primer.api.errors import common_responses
from primer.api.routers import _managed as _managed_mod
from primer.api.routers._references import ReferenceCheck, build_reference_block_hook
from primer.api.pagination import FindRequest, parse_order_by, parse_page
from primer.model.common import Identifiable
from primer.model.except_ import ConflictError, NotFoundError
from primer.model.storage import (
    CursorPageResponse,
    FieldRef,
    OffsetPageResponse,
    Op,
    OrderBy,
    PageRequest,
    Predicate,
    Value,
)


_ModelT = TypeVar("_ModelT", bound=Identifiable)


# Page response is a runtime union of the two concrete page shapes;
# FastAPI/Pydantic serializes it via the discriminator on ``kind``.
_PageResp = OffsetPageResponse[Any] | CursorPageResponse[Any]


def _escape_like(value: str) -> str:
    """Escape SQL ``LIKE`` / ``ILIKE`` metacharacters in a user query.

    Escapes the escape char first, then ``%`` and ``_``, so the value matches
    LITERALLY under the ``ESCAPE '\\'`` clause the ILIKE renderers emit. A user
    typing ``50%`` then searches for a literal ``50%`` rather than "50 followed
    by any sequence". Mirrors the backend content-store ``_escape_like``
    helpers (``primer.storage.sqlite`` / ``primer.storage.postgres``).
    """
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _build_search_predicate(search_fields: Sequence[str], q: str) -> Predicate:
    """OR a case-insensitive substring (ILIKE) match across ``search_fields``.

    Each field yields ``<field> ILIKE '%<escaped-q>%'``; the terms are folded
    right-associatively into an OR tree. ``q`` is escaped so its ``%`` / ``_``
    are treated literally (see :func:`_escape_like`).
    """
    pattern = f"%{_escape_like(q)}%"
    terms = [
        Predicate(
            left=FieldRef(name=field),
            op=Op.ILIKE,
            right=Value(value=pattern),
        )
        for field in search_fields
    ]
    combined = terms[-1]
    for term in reversed(terms[:-1]):
        combined = Predicate(left=term, op=Op.OR, right=combined)
    return combined


def _compose(
    first: _OnMutateHook,
    second: _OnMutateHook,
) -> _OnMutateHook:
    """Return a hook that calls *first* then *second* (if both non-None)."""
    if first is None:
        return second
    if second is None:
        return first

    async def _composed(entity_id: str, request: Any) -> None:
        await first(entity_id, request)  # type: ignore[misc]
        await second(entity_id, request)  # type: ignore[misc]

    return _composed


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

# Pre-delete hook signature: ``(existing, request) -> None``.
# Called BEFORE storage.delete with the stored row that is about to be
# removed. Raise HTTPException to abort the delete (e.g. harness-managed
# entity guard).
_OnPreDeleteHook = Callable[[Any, Request], Awaitable[None]] | None

# Pre-delete-id hook signature: ``(entity_id, request) -> None``.
# Called BEFORE the storage lookup on DELETE, so the hook fires even
# when the row does not exist in storage. Use for reserved-id guards
# that must return 403 regardless of storage state.
_OnPreDeleteIdHook = Callable[[str, Request], Awaitable[None]] | None


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
    on_pre_delete: _OnPreDeleteHook = None,
    on_pre_delete_id: _OnPreDeleteIdHook = None,
    extra_get_responses: dict[int, dict[str, Any]] | None = None,
    scope_field: str | None = None,
    parent_path_segment: str | None = None,
    managed_by_field: str | None = None,
    references: Sequence[ReferenceCheck] = (),
    cdc_kind: str | None = None,
    search_fields: list[str] | None = None,
) -> APIRouter:
    """Build a CRUD + Find APIRouter for ``model_cls``.

    Parameters
    ----------
    model_cls
        The persisted Pydantic model. Used as the request/response body type.
    storage_dep
        FastAPI dependency that returns ``Storage[model_cls]``. Typically
        one of the per-model helpers in :mod:`primer.api.deps`.
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
    on_pre_delete
        Async callable ``async def(existing, request: Request) -> None``
        invoked BEFORE ``storage.delete()`` with the stored row that is
        about to be removed. Raise :class:`HTTPException` to abort the
        delete (e.g. harness-managed entity guard).
    on_pre_delete_id
        Async callable ``async def(entity_id: str, request: Request) -> None``
        invoked BEFORE the storage lookup on DELETE. Fires even when the
        row does not exist in storage. Use for reserved-id guards that must
        return 403 regardless of storage state (e.g. bootstrap protections).
    extra_get_responses
        Extra response codes documented for the GET-by-id route (in
        addition to the standard 404).
    scope_field
        When set alongside ``parent_path_segment``, the router mounts
        under ``/v1/{parent_path_segment}/{parent_id}/{plural}`` and
        all CRUD endpoints enforce that the row's ``scope_field`` matches
        the ``parent_id`` path segment.  Must be set together with
        ``parent_path_segment``; raises :class:`ValueError` if only one
        of the two params is provided.
    parent_path_segment
        URL segment for the parent resource, e.g. ``"workspaces"``.
        See ``scope_field`` above.
    managed_by_field
        When set, automatically wires three guards keyed on *field_name*:

        * **CREATE** – rejects the request with 422 if the body sets
          ``managed_by_field`` to a non-null value.
        * **UPDATE** – rejects with 409 if the *existing* row has a
          non-null ``managed_by_field`` (the row is owned by an external
          system).
        * **DELETE** – same 409 guard as UPDATE.

        User-supplied ``on_pre_create`` / ``on_pre_update`` /
        ``on_pre_delete`` hooks are composed *after* these auto-wired
        guards.
    references
        Optional list of :class:`~primer.api.routers._references.ReferenceCheck`
        declarations.  When non-empty, the factory calls
        :func:`~primer.api.routers._references.build_reference_block_hook`
        to build a pre-delete hook that enforces all checks in order.  The
        auto-generated reference hook runs **before** any user-supplied
        ``on_pre_delete`` hook.
    cdc_kind
        When set, the factory:

        * Calls ``register_cdc_kind(cdc_kind, model_cls)`` immediately
          (at factory call time / module import) so the kind appears in
          :func:`~primer.api.routers._cdc_hooks.known_cdc_kinds`.
        * Auto-wires the three CDC hooks from
          :func:`~primer.api.routers._cdc_hooks.make_cdc_hooks` into
          ``on_create`` / ``on_update`` / ``on_delete``.  The CDC hooks
          run *before* any user-supplied post-mutate hooks.
    search_fields
        When set, the unscoped ``GET /<plural>`` list route accepts an
        optional ``?q=`` query param and, when it is non-empty, filters
        rows by a case-insensitive substring (ILIKE) match ORed across
        these fields via ``storage.find``.  The response keeps the same
        ``{items, total}`` offset-paged shape as the unfiltered list.
        ``None`` (the default) leaves the list route unchanged.
    """

    # Validate scope params: both must be set together or not at all.
    _scoped = scope_field is not None or parent_path_segment is not None
    if _scoped:
        if scope_field is None:
            raise ValueError(
                "scope_field must be provided when parent_path_segment is set"
            )
        if parent_path_segment is None:
            raise ValueError(
                "parent_path_segment must be provided when scope_field is set"
            )

    # Auto-wire managed_by_field guards. User-supplied hooks are appended
    # after so they compose on top of the built-in protections.
    if managed_by_field is not None:
        _auto_pre_create = _managed_mod.reject_if_body_sets_field(managed_by_field)
        _auto_pre_update = _managed_mod.on_pre_update_reject_if_managed_factory(
            managed_by_field
        )
        _auto_pre_delete = _managed_mod.reject_if_managed_factory(
            managed_by_field, for_action="delete"
        )

        _user_pre_create = on_pre_create
        _user_pre_update = on_pre_update
        _user_pre_delete = on_pre_delete

        async def _chained_pre_create(entity, request: Request) -> None:
            await _auto_pre_create(entity, request)
            if _user_pre_create is not None:
                await _user_pre_create(entity, request)

        async def _chained_pre_update(entity, existing, request: Request) -> None:
            await _auto_pre_update(entity, existing, request)
            if _user_pre_update is not None:
                await _user_pre_update(entity, existing, request)

        async def _chained_pre_delete(existing, request: Request) -> None:
            await _auto_pre_delete(existing, request)
            if _user_pre_delete is not None:
                await _user_pre_delete(existing, request)

        on_pre_create = _chained_pre_create
        on_pre_update = _chained_pre_update
        on_pre_delete = _chained_pre_delete

    # Auto-wire reference-block hook. The generated hook runs BEFORE the
    # user-supplied on_pre_delete so reference checks always fire first.
    if references:
        _ref_hook = build_reference_block_hook(references)
        _user_pre_delete_ref = on_pre_delete

        async def _pre_delete_with_refs(existing: Any, request: Request) -> None:
            await _ref_hook(existing, request)
            if _user_pre_delete_ref is not None:
                await _user_pre_delete_ref(existing, request)

        on_pre_delete = _pre_delete_with_refs

    # Auto-wire CDC hooks. Register the kind in the global registry at
    # factory call time (i.e. when the router module is imported), then
    # compose the three CDC hooks ahead of any user-supplied post-mutate
    # hooks so CDC events always fire.
    if cdc_kind is not None:
        from primer.api.routers._cdc_hooks import (  # noqa: PLC0415
            make_cdc_hooks,
            register_cdc_kind,
        )

        register_cdc_kind(cdc_kind, model_cls)
        cdc_create_hook, cdc_update_hook, cdc_delete_hook = make_cdc_hooks(
            cdc_kind, model_cls  # type: ignore[arg-type]
        )
        on_create = _compose(cdc_create_hook, on_create)
        on_update = _compose(cdc_update_hook, on_update)
        on_delete = _compose(cdc_delete_hook, on_delete)

    router = APIRouter(tags=[tag])

    if scope_field is not None and parent_path_segment is not None:
        # ------------------------------------------------------------------
        # SCOPED routes: /v1/{parent_path_segment}/{parent_id}/{plural}/...
        # ------------------------------------------------------------------

        # ---- POST /{parent_path_segment}/{parent_id}/{plural}  (create) --
        @router.post(
            f"/{parent_path_segment}/{{parent_id}}/{plural}",
            response_model=model_cls,
            status_code=status.HTTP_201_CREATED,
            summary=f"Create {model_cls.__name__}",
            responses=common_responses(409, 422, 500),
        )
        async def _scoped_create(
            request: Request,
            parent_id: str = Path(..., description="Parent resource id."),
            entity: model_cls = Body(..., description=f"{model_cls.__name__} body"),  # type: ignore[valid-type,assignment]
            storage=Depends(storage_dep),
        ) -> _ModelT:
            # Enforce scope_field matches parent_id from path.
            body_scope = getattr(entity, scope_field)
            if body_scope != parent_id:
                raise HTTPException(
                    status_code=422,
                    detail=(
                        f"{scope_field} in body ({body_scope!r}) does not match "
                        f"parent_id in path ({parent_id!r})"
                    ),
                )
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

        _scoped_create.__annotations__["entity"] = model_cls

        # ---- GET /{parent_path_segment}/{parent_id}/{plural}/{id}  (read) -
        get_responses = common_responses(404, 500)
        if extra_get_responses:
            get_responses = {**get_responses, **extra_get_responses}

        @router.get(
            f"/{parent_path_segment}/{{parent_id}}/{plural}/{{entity_id}}",
            response_model=model_cls,
            summary=f"Get {model_cls.__name__} by id",
            responses=get_responses,
        )
        async def _scoped_get(
            parent_id: str = Path(..., description="Parent resource id."),
            entity_id: str = Path(..., description="Entity id."),
            storage=Depends(storage_dep),
        ) -> _ModelT:
            row = await storage.get(entity_id)
            if row is None or getattr(row, scope_field) != parent_id:
                raise NotFoundError(
                    f"{model_cls.__name__} {entity_id!r} does not exist"
                )
            return row

        # ---- PUT /{parent_path_segment}/{parent_id}/{plural}/{id}  --------
        @router.put(
            f"/{parent_path_segment}/{{parent_id}}/{plural}/{{entity_id}}",
            response_model=model_cls,
            summary=f"Replace {model_cls.__name__}",
            responses=common_responses(404, 409, 422, 500),
        )
        async def _scoped_update(
            request: Request,
            parent_id: str = Path(..., description="Parent resource id."),
            body: dict[str, Any] = Body(..., description=f"{model_cls.__name__} body"),
            entity_id: str = Path(..., description="Entity id."),
            storage=Depends(storage_dep),
        ) -> _ModelT:
            if not body.get("id"):
                body = {**body, "id": entity_id}
            try:
                entity = model_cls.model_validate(body)
            except PydanticValidationError as exc:
                raise RequestValidationError(exc.errors()) from exc
            if entity.id != entity_id:
                raise ConflictError(
                    f"path id {entity_id!r} does not match body id {entity.id!r}"
                )
            existing = await storage.get(entity_id)
            if existing is None or getattr(existing, scope_field) != parent_id:
                raise NotFoundError(
                    f"{model_cls.__name__} {entity_id!r} does not exist"
                )
            if on_pre_update is not None:
                await on_pre_update(entity, existing, request)
            updated = await storage.update(entity)
            if on_update is not None:
                await on_update(updated.id, request)
            return updated

        # ---- DELETE /{parent_path_segment}/{parent_id}/{plural}/{id}  -----
        @router.delete(
            f"/{parent_path_segment}/{{parent_id}}/{plural}/{{entity_id}}",
            status_code=status.HTTP_204_NO_CONTENT,
            summary=f"Delete {model_cls.__name__}",
            responses=common_responses(404, 500),
        )
        async def _scoped_delete(
            request: Request,
            parent_id: str = Path(..., description="Parent resource id."),
            entity_id: str = Path(..., description="Entity id."),
            storage=Depends(storage_dep),
        ) -> None:
            if on_pre_delete_id is not None:
                await on_pre_delete_id(entity_id, request)
            existing = await storage.get(entity_id)
            if existing is None or getattr(existing, scope_field) != parent_id:
                raise NotFoundError(
                    f"{model_cls.__name__} {entity_id!r} does not exist"
                )
            if on_pre_delete is not None:
                await on_pre_delete(existing, request)
            if on_delete is not None:
                await on_delete(entity_id, request)
            await storage.delete(entity_id)

        # ---- GET /{parent_path_segment}/{parent_id}/{plural}  (list) ------
        _scope_predicate = Predicate(
            left=FieldRef(name=scope_field),
            op=Op.EQ,
            right=Value(value="{parent_id}"),  # placeholder; overridden in closure
        )

        @router.get(
            f"/{parent_path_segment}/{{parent_id}}/{plural}",
            summary=f"List {model_cls.__name__}",
            responses=common_responses(400, 422, 500),
        )
        async def _scoped_list(
            parent_id: str = Path(..., description="Parent resource id."),
            page: PageRequest = Depends(parse_page),
            order_by: list[OrderBy] | None = Depends(parse_order_by),
            storage=Depends(storage_dep),
        ) -> _PageResp:
            predicate = Predicate(
                left=FieldRef(name=scope_field),
                op=Op.EQ,
                right=Value(value=parent_id),
            )
            return await storage.find(predicate, page, order_by=order_by)

        # ---- POST /{parent_path_segment}/{parent_id}/{plural}/find  -------
        @router.post(
            f"/{parent_path_segment}/{{parent_id}}/{plural}/find",
            summary=f"Find {model_cls.__name__} with predicate",
            responses=common_responses(400, 422, 500),
        )
        async def _scoped_find(
            parent_id: str = Path(..., description="Parent resource id."),
            body: FindRequest = Body(...),
            storage=Depends(storage_dep),
        ) -> _PageResp:
            scope_pred = Predicate(
                left=FieldRef(name=scope_field),
                op=Op.EQ,
                right=Value(value=parent_id),
            )
            combined = (
                Predicate(left=scope_pred, op=Op.AND, right=body.predicate)
                if body.predicate is not None
                else scope_pred
            )
            return await storage.find(combined, body.page, order_by=body.order_by)

    else:
        # ------------------------------------------------------------------
        # UNSCOPED routes (original behaviour)
        # ------------------------------------------------------------------

        # ---- POST /<plural>  (create) ------------------------------------
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

        # ---- GET /<plural>/{id}  (read) ----------------------------------
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

        # ---- PUT /<plural>/{id}  (replace) -------------------------------
        @router.put(
            f"/{plural}/{{entity_id}}",
            response_model=model_cls,
            summary=f"Replace {model_cls.__name__}",
            responses=common_responses(404, 409, 422, 500),
        )
        async def _update(
            request: Request,
            body: dict[str, Any] = Body(..., description=f"{model_cls.__name__} body"),
            entity_id: str = Path(..., description="Entity id."),
            storage=Depends(storage_dep),
        ) -> _ModelT:
            if not body.get("id"):
                body = {**body, "id": entity_id}
            try:
                entity = model_cls.model_validate(body)
            except PydanticValidationError as exc:
                raise RequestValidationError(exc.errors()) from exc
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

        # ---- DELETE /<plural>/{id} ---------------------------------------
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
            if on_pre_delete_id is not None:
                await on_pre_delete_id(entity_id, request)
            existing = await storage.get(entity_id)
            if existing is None:
                raise NotFoundError(
                    f"{model_cls.__name__} {entity_id!r} does not exist"
                )
            if on_pre_delete is not None:
                await on_pre_delete(existing, request)
            if on_delete is not None:
                await on_delete(entity_id, request)
            await storage.delete(entity_id)

        # ---- GET /<plural>  (list) ---------------------------------------
        @router.get(
            f"/{plural}",
            summary=f"List {model_cls.__name__}",
            responses=common_responses(400, 422, 500),
        )
        async def _list(
            page: PageRequest = Depends(parse_page),
            order_by: list[OrderBy] | None = Depends(parse_order_by),
            q: str | None = Query(
                default=None,
                description=(
                    "Case-insensitive substring match over the entity's "
                    "searchable fields. Ignored when the entity declares no "
                    "searchable fields."
                ),
            ),
            storage=Depends(storage_dep),
        ) -> _PageResp:
            # q present AND this entity is searchable -> ILIKE substring find,
            # preserving the same page/order_by handling and the identical
            # OffsetPageResponse {items, total} shape as the plain list.
            if q and q.strip() and search_fields:
                predicate = _build_search_predicate(search_fields, q)
                return await storage.find(predicate, page, order_by=order_by)
            return await storage.list(page, order_by=order_by)

        # ---- POST /<plural>/find  (find with predicate) ------------------
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
