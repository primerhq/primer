"""Phase-3 knowledge entity routers: VectorStoreConfig, Collection, Document.

* VectorStoreConfig — single-active config for the application's
  vector store. CRUD on the model only; mutating the row invalidates
  the cached :class:`VectorStoreRegistry` provider so the next call
  rebuilds it from scratch. The id is conventionally
  ``_active_vector_store`` (see
  :mod:`matrix.api.registries.vector_store_registry`).
* Collection — CRUD + Find. ``GET /v1/collections/{id}/documents``
  lists documents belonging to the collection (server-side filter on
  ``collection_id``). Live ``search`` against the vector store is
  deferred to a follow-up sub-project.
* Document — CRUD + Find. Live ``ingest`` (multipart upload + docling
  chunking) is deferred to a follow-up sub-project.
"""

from __future__ import annotations

from fastapi import Depends, Path, Query, Request

from matrix.api.deps import (
    get_collection_storage,
    get_document_storage,
    get_vector_store_config_storage,
)
from matrix.api.errors import common_responses
from matrix.api.routers._crud import make_crud_router
from matrix.model.collection import Collection, Document
from matrix.model.except_ import NotFoundError
from matrix.model.storage import (
    CursorPageResponse,
    FieldRef,
    Op,
    OffsetPage,
    OffsetPageResponse,
    Predicate,
    Value,
)
from matrix.model.vector import VectorStoreConfig


# ---- VectorStoreConfig router ----------------------------------------------


async def _invalidate_vector_store(_id: str, request: Request) -> None:
    registry = request.app.state.vector_store_registry
    await registry.invalidate()


vector_store_config_router = make_crud_router(
    model_cls=VectorStoreConfig,
    storage_dep=get_vector_store_config_storage,
    plural="vector_store_configs",
    tag="vector-store-configs",
    on_create=_invalidate_vector_store,
    on_update=_invalidate_vector_store,
    on_delete=_invalidate_vector_store,
)


# ---- Collection router -----------------------------------------------------

collection_router = make_crud_router(
    model_cls=Collection,
    storage_dep=get_collection_storage,
    plural="collections",
    tag="collections",
)


@collection_router.get(
    "/collections/{collection_id}/documents",
    summary="List documents belonging to a collection",
    responses=common_responses(404, 500),
)
async def list_collection_documents(
    collection_id: str = Path(..., description="Collection id"),
    limit: int = Query(default=20, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    collections=Depends(get_collection_storage),
    documents=Depends(get_document_storage),
) -> OffsetPageResponse | CursorPageResponse:
    """Server-side find on ``Document.collection_id == collection_id``.

    Uses an offset page; cursor pagination over a filtered set is
    deferred to the SearchService follow-up.
    """
    if await collections.get(collection_id) is None:
        raise NotFoundError(f"Collection {collection_id!r} does not exist")

    predicate = Predicate(
        left=FieldRef(name="collection_id"),
        op=Op.EQ,
        right=Value(value=collection_id),
    )
    page = OffsetPage(offset=offset, length=limit)
    return await documents.find(predicate, page)


# ---- Document router -------------------------------------------------------

document_router = make_crud_router(
    model_cls=Document,
    storage_dep=get_document_storage,
    plural="documents",
    tag="documents",
)


__all__ = [
    "collection_router",
    "document_router",
    "vector_store_config_router",
]
