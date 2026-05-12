"""Phase-3 knowledge entity routers: Collection + Document.

* Collection — CRUD + Find. ``GET /v1/collections/{id}/documents``
  lists documents belonging to the collection (server-side filter on
  ``collection_id``). Live ``search`` against the vector store ships
  via :mod:`matrix.api.routers.search` (per-Describeable type) once
  the internal collections subsystem is activated.
* Document — CRUD + Find. Live ``ingest`` (multipart upload + docling
  chunking) is deferred to a follow-up sub-project; the system
  toolset's ``put_document`` provides an in-process upsert path.

VectorStoreConfig CRUD has moved out of storage entirely — vector
store configuration is now an AppConfig field
(:attr:`matrix.api.config.AppConfig.vector_store`) read once at
process boot.
"""

from __future__ import annotations

from fastapi import Depends, Path, Query

from matrix.api.deps import (
    get_collection_storage,
    get_document_storage,
)
from matrix.api.errors import common_responses
from matrix.api.routers._cdc_hooks import make_cdc_hooks
from matrix.api.routers._crud import make_crud_router
from matrix.model.collection import Collection, Document


_collection_create, _collection_update, _collection_delete = make_cdc_hooks(
    "collection", Collection,
)
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


# ---- Collection router -----------------------------------------------------

collection_router = make_crud_router(
    model_cls=Collection,
    storage_dep=get_collection_storage,
    plural="collections",
    tag="collections",
    on_create=_collection_create,
    on_update=_collection_update,
    on_delete=_collection_delete,
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
    """Server-side find on ``Document.collection_id == collection_id``."""
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
]
