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
:mod:`primer.api.routers.internal_collections`) is a different
operation — it searches the *collection metadata* internal index for
the "find collection by description" use case. The per-collection
``/{id}/search`` route here searches the *document contents*.
"""

from __future__ import annotations

from typing import Any

from fastapi import Body, Depends, File, Path, Query, Request, UploadFile
from fastapi.exceptions import RequestValidationError
from pydantic import BaseModel, Field

from primer.api.deps import (
    get_collection_storage,
    get_document_storage,
    get_provider_registry,
    get_semantic_search_registry,
)
from primer.api.errors import common_responses
from primer.api.registries import ProviderRegistry, SemanticSearchRegistry
from primer.api.routers._cdc_hooks import register_cdc_kind
from primer.api.routers._crud import make_crud_router
from primer.model.chat import TextPart
from primer.model.collection import Collection, Document
from primer.model.except_ import BadRequestError, NotFoundError
from primer.model.provider import SemanticSearchProvider


from primer.model.storage import (
    CursorPageResponse,
    OffsetPage,
    OffsetPageResponse,
)
from primer.storage.q import Q


# Register Document in the CDC kinds registry so the harness service can
# resolve it via known_cdc_kinds().  Document is harness-managed but has no
# internal-collections vector index, so no CDC event hooks are wired here.
register_cdc_kind("document", Document)


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


async def _collection_pre_create(entity: Collection, request: Request) -> None:
    """Composed on_pre_create: SSP reference check."""
    await _validate_ssp_exists(entity, request)


async def _collection_pre_update(
    entity: Collection, existing: Collection, request: Request
) -> None:
    """Composed on_pre_update: SSP immutability check."""
    await _validate_ssp_immutable(entity, existing, request)


# ---- Collection router -----------------------------------------------------

collection_router = make_crud_router(
    model_cls=Collection,
    storage_dep=get_collection_storage,
    plural="collections",
    tag="collections",
    cdc_kind="collection",
    managed_by_field="harness_id",
    on_pre_create=_collection_pre_create,
    on_pre_update=_collection_pre_update,
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

    predicate = Q(Document).where("collection_id", collection_id).build()
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


@collection_router.get(
    "/collections/{collection_id}/indexed_documents",
    summary="List entries indexed in a collection's vector store",
    responses=common_responses(404, 500, 502, 503),
)
async def list_indexed_documents(
    collection_id: str = Path(..., description="Collection id"),
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    collections=Depends(get_collection_storage),
    ssr: SemanticSearchRegistry = Depends(get_semantic_search_registry),
) -> dict:
    """Enumerate everything the vector store has for this collection.

    Internal (``system=True``) collections store their content directly
    in the vector index — no ``Document`` rows back them — so the regular
    ``GET /collections/{id}/documents`` endpoint always returns empty
    for them. This endpoint surfaces the actual indexed entries by
    calling the vector store's ``search_by_meta({})`` primitive (which
    matches every record), then slicing client-side for the requested
    ``offset`` / ``limit`` window.

    Works for user-owned collections too; just returns whatever has been
    ingested into the vector store regardless of whether Document rows
    also exist in storage.

    Pagination today is in-process (the vector-store ABC has no native
    offset/limit). The records list is sorted deterministically by
    ``(document_id, chunk_id)`` so a slice is stable across calls.
    """
    coll = await collections.get(collection_id)
    if coll is None:
        raise NotFoundError(f"Collection {collection_id!r} does not exist")

    store = await ssr.get_store(coll.search_provider_id)
    # SSP registration is lazy: VectorStore.create_collection runs only
    # when the first document is ingested. A freshly-created Collection
    # row is therefore unknown to the vector store's catalogue until
    # then, and search_by_meta raises BadRequestError("...is not
    # registered..."). Treat that as "no indexed entries yet" so the UI
    # surfaces an empty list instead of an error on the very first
    # click after creating the collection.
    try:
        records = await store.search_by_meta(collection_id, meta={})
    except BadRequestError as exc:
        if "is not registered" not in str(exc):
            raise
        records = []
    total = len(records)
    window = records[offset:offset + limit]
    items = [
        {
            "document_id": r.document_id,
            "chunk_id": r.chunk_id,
            "text": r.text,
            "meta": r.meta,
        }
        for r in window
    ]
    return {
        "items": items,
        "total": total,
        "offset": offset,
        "limit": limit,
        "truncated": (offset + limit) < total,
    }


# ---- Document router -------------------------------------------------------


# Extensions / content types whose content IS already markdown or plain
# text. We short-circuit docling for these because (a) docling can't
# reliably detect the format from raw bytes with no filename hint and
# fails on .md; (b) running text through a markdown converter just to
# get markdown back is wasteful.
_TEXT_PASSTHROUGH_EXTENSIONS = (".md", ".markdown", ".txt", ".text")
_TEXT_PASSTHROUGH_CONTENT_TYPES = (
    "text/markdown",
    "text/x-markdown",
    "text/plain",
)


def _is_text_passthrough(
    filename: str | None, content_type: str | None,
) -> bool:
    """True when the upload is already text and needs no docling pass.

    Filename extension wins (operators sometimes mislabel the
    content-type by uploading a `.md` with `application/octet-stream`).
    Content-type is the fallback when there is no extension.
    """
    if filename:
        lower = filename.lower()
        for ext in _TEXT_PASSTHROUGH_EXTENSIONS:
            if lower.endswith(ext):
                return True
    if content_type:
        # Strip any charset / boundary parameters: "text/markdown; charset=utf-8".
        primary = content_type.split(";", 1)[0].strip().lower()
        if primary in _TEXT_PASSTHROUGH_CONTENT_TYPES:
            return True
    return False


@collection_router.post(
    "/documents/_convert_file",
    summary="Convert an uploaded file to markdown via docling",
    responses=common_responses(400, 500),
)
async def convert_uploaded_file(
    file: UploadFile = File(...),
) -> dict:
    """Convert an uploaded file to markdown and return the result.

    For binary formats (PDF, DOCX, PPTX, XLSX, HTML, images with OCR,
    ...) we round-trip through docling. For already-textual formats
    (``.md`` / ``.markdown`` / ``.txt`` / ``text/markdown`` /
    ``text/plain``) we decode the bytes as UTF-8 and return them
    verbatim - docling can't reliably detect a markdown source from
    raw bytes without a filename hint and previously raised
    UnsupportedContentError.

    The endpoint is non-destructive: it does NOT persist a Document
    row. Operators upload, see the converted text in the create form,
    optionally edit, then POST /documents through the normal CRUD path.
    """
    from primer.ingest.loaders.docling import DoclingLoader
    from primer.model.except_ import UnsupportedContentError

    raw = await file.read()
    if not raw:
        raise BadRequestError("uploaded file is empty")
    # 32 MB cap; raise to a bigger value once the worker pool can
    # absorb the conversion cost.
    if len(raw) > 32 * 1024 * 1024:
        raise BadRequestError(
            f"uploaded file is too large ({len(raw)} bytes); cap is "
            f"32 MB. Split the file or paste the extracted text."
        )

    if _is_text_passthrough(file.filename, file.content_type):
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise BadRequestError(
                f"text upload is not valid UTF-8: {exc}"
            ) from exc
        return {
            "filename": file.filename,
            "content_type": file.content_type,
            "bytes_loaded": len(raw),
            "text": text,
        }

    loader = DoclingLoader()
    try:
        loaded = await loader.load(raw)
    except UnsupportedContentError as exc:
        raise BadRequestError(str(exc)) from exc

    return {
        "filename": file.filename,
        "content_type": file.content_type,
        "bytes_loaded": len(raw),
        "text": loaded.text,
    }


async def _reject_system_collection(
    collection_id: str, request: Request, *, verb: str,
) -> None:
    """Raise BadRequestError if ``collection_id`` names a system collection.

    System (internal) collections are owned and maintained entirely by
    their internal subsystem (agents / graphs / tools / collections
    catalogs). Operators must not hand-ingest documents into them; their
    content is reconciled from the source entities via CDC. This guard
    backs the UI which also hides the create button for system rows.
    """
    storage_provider = request.app.state.storage_provider
    collection_storage = storage_provider.get_storage(Collection)
    coll = await collection_storage.get(collection_id)
    # A missing collection is left to referential-integrity handling
    # elsewhere; we only block the system-collection case here.
    if coll is not None and getattr(coll, "system", False):
        raise BadRequestError(
            f"Collection {collection_id!r} is system-managed; documents "
            f"cannot be {verb} into it. Internal collections are "
            f"reconciled automatically from their source entities."
        )


async def _document_pre_create(entity: Document, request: Request) -> None:
    """on_pre_create hook: block ingestion into system collections."""
    await _reject_system_collection(
        entity.collection_id, request, verb="created",
    )


async def _document_pre_update(
    entity: Document, existing: Document, request: Request
) -> None:
    """on_pre_update hook: block edits that target a system collection
    (covers both a system source and a system destination)."""
    await _reject_system_collection(
        entity.collection_id, request, verb="updated",
    )
    if existing.collection_id != entity.collection_id:
        await _reject_system_collection(
            existing.collection_id, request, verb="updated",
        )


document_router = make_crud_router(
    model_cls=Document,
    storage_dep=get_document_storage,
    plural="documents",
    tag="documents",
    managed_by_field="harness_id",
    on_pre_create=_document_pre_create,
    on_pre_update=_document_pre_update,
)


__all__ = [
    "collection_router",
    "document_router",
]
