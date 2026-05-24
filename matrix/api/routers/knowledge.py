"""Phase-3 knowledge entity routers: Collection + Document.

* Collection — CRUD + Find. ``GET /v1/collections/{id}/documents``
  lists documents belonging to the collection (server-side filter on
  ``collection_id``). ``POST /v1/collections/{id}/search`` runs
  semantic search across the collection's indexed documents using the
  collection's own embedder + the SSP-registry-resolved vector store.
* Document — CRUD + Find. Live ``ingest`` (multipart upload + docling
  chunking) is deferred to a follow-up sub-project; the system
  toolset's ``put_document`` provides an in-process upsert path.

NOTE: ``POST /v1/collections/search`` (no id, in
:mod:`matrix.api.routers.internal_collections`) is a different
operation — it searches the *collection metadata* internal index for
the "find collection by description" use case. The per-collection
``/{id}/search`` route here searches the *document contents*.
"""

from __future__ import annotations

from typing import Any

from fastapi import Body, Depends, Path, Query, Request
from fastapi.exceptions import RequestValidationError
from pydantic import BaseModel, Field

from matrix.api.deps import (
    get_collection_storage,
    get_document_storage,
    get_provider_registry,
    get_semantic_search_registry,
)
from matrix.api.errors import common_responses
from matrix.api.registries import ProviderRegistry, SemanticSearchRegistry
from matrix.api.routers._cdc_hooks import make_cdc_hooks
from matrix.api.routers._crud import make_crud_router
from matrix.model.chat import TextPart
from matrix.model.collection import Collection, Document
from matrix.model.except_ import NotFoundError
from matrix.model.provider import SemanticSearchProvider


from matrix.model.storage import (
    CursorPageResponse,
    FieldRef,
    Op,
    OffsetPage,
    OffsetPageResponse,
    Predicate,
    Value,
)


_collection_create, _collection_update, _collection_delete = make_cdc_hooks(
    "collection", Collection,
)


class _CollectionSearchBody(BaseModel):
    """Body for ``POST /v1/collections/{id}/search``."""

    query: str = Field(
        ..., min_length=1, description="Free-text query string.",
    )
    top_k: int = Field(
        default=10, ge=1, le=100,
        description="Maximum number of hits to return.",
    )


# ---- Collection validation hooks -------------------------------------------


async def _validate_ssp_exists(entity: Collection, request: Request) -> None:
    """on_pre_create hook: verify search_provider_id points at an existing SSP."""
    storage_provider = request.app.state.storage_provider
    ssp_storage = storage_provider.get_storage(SemanticSearchProvider)
    existing = await ssp_storage.get(entity.search_provider_id)
    if existing is None:
        raise NotFoundError(
            f"Collection {entity.id!r}: search_provider_id "
            f"{entity.search_provider_id!r} does not refer to a "
            "known SemanticSearchProvider."
        )


async def _validate_ssp_immutable(
    entity: Collection, existing: Collection, request: Request
) -> None:
    """on_pre_update hook: reject changes to search_provider_id after create."""
    if existing.search_provider_id != entity.search_provider_id:
        raise RequestValidationError(
            errors=[
                {
                    "type": "value_error",
                    "loc": ("body", "search_provider_id"),
                    "msg": "field is immutable after create",
                    "input": entity.search_provider_id,
                }
            ]
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
    on_pre_create=_validate_ssp_exists,
    on_pre_update=_validate_ssp_immutable,
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


@collection_router.post(
    "/collections/{collection_id}/search",
    summary="Semantic search within a collection's documents",
    responses=common_responses(404, 422, 502, 503),
)
async def search_collection(
    collection_id: str = Path(..., description="Collection id"),
    body: _CollectionSearchBody = Body(...),
    collections=Depends(get_collection_storage),
    registry: ProviderRegistry = Depends(get_provider_registry),
    ssr: SemanticSearchRegistry = Depends(get_semantic_search_registry),
) -> dict:
    """Vectorise ``body.query`` with the collection's embedder and run a
    similarity search against the collection's vector store (resolved
    via the collection's ``search_provider_id``), scoped to this
    collection. Returns ``{"hits": [{document_id, chunk_id, score,
    text, meta}, ...]}``.

    The collection must exist and have indexed documents; an empty
    collection returns an empty hits list. The embedder used is the
    one declared on ``Collection.embedder`` (provider id + model name)
    — the same one the ingest pipeline used when storing chunks, so
    query and index vectors live in the same embedding space.
    """
    coll = await collections.get(collection_id)
    if coll is None:
        raise NotFoundError(f"Collection {collection_id!r} does not exist")

    # Vectorise the query with the collection's own embedder so query
    # and index vectors agree on dimensionality + distance metric.
    embedder = await registry.get_embedder(coll.embedder.provider_id)
    response = await embedder.embed(
        model=coll.embedder.model,
        inputs=[TextPart(text=body.query)],
    )
    vector = list(response.embeddings[0].vector)

    # Resolve the vector store via the collection's search_provider_id.
    store = await ssr.get_store(coll.search_provider_id)
    hits = await store.search(collection_id, vector, body.top_k)
    return {
        "hits": [
            {
                "document_id": h.record.document_id,
                "chunk_id": h.record.chunk_id,
                "score": h.score,
                "text": h.record.text,
                "meta": h.record.meta,
            }
            for h in hits
        ],
    }


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
