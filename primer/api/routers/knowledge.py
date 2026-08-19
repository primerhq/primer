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

import logging
from typing import Any, Literal

from fastapi import (
    Body,
    Depends,
    File,
    HTTPException,
    Path,
    Query,
    Request,
    UploadFile,
)
from fastapi.exceptions import RequestValidationError
from pydantic import BaseModel, Field

from primer.api.deps import (
    get_collection_storage,
    get_document_service,
    get_document_tree_service,
    get_document_storage,
    get_provider_registry,
    get_semantic_search_registry,
)
from primer.api.errors import common_responses
from primer.api.registries import ProviderRegistry, SemanticSearchRegistry
from primer.api.routers._cdc_hooks import register_cdc_kind
from primer.api.routers._crud import make_crud_router
from primer.model.chat import TextPart
from primer.model.collection import (
    ChunkingConfig,
    Collection,
    CollectionEmbedder,
    CollectionSearchConfig,
    Document,
)
from primer.model.search import CollectionCrossEncoder
from primer.model.except_ import (
    BadRequestError,
    ConfigError,
    ConflictError,
    DimensionMismatchError,
    NotFoundError,
)
from primer.knowledge.grep import grep_collection
from primer.knowledge.importer import import_zip
from primer.knowledge.lifecycle import (
    disable_search,
    enable_search,
    search_status,
)
from primer.search.run import run_collection_search


logger = logging.getLogger(__name__)


# Register Document in the CDC kinds registry so the harness service can
# resolve it via known_cdc_kinds().  Document is harness-managed but has no
# internal-collections vector index, so no CDC event hooks are wired here.
register_cdc_kind("document", Document)


class _DocumentUpsertBody(BaseModel):
    """Body for ``PUT /v1/collections/{id}/documents?path=<p>``."""

    content: str = Field(..., description="The document body to store + index.")
    title: str | None = Field(
        default=None,
        description="Optional display title; defaults to the path leaf when unset.",
    )
    meta: dict[str, Any] | None = Field(
        default=None,
        description="Free-form metadata bag stored on the Document entity.",
    )


class _DocumentMoveBody(BaseModel):
    """Body for ``POST /v1/collections/{id}/documents/move``.

    Uses ``from`` / ``to`` on the wire (matching a filesystem move); ``from``
    is a reserved Python keyword so it is aliased to the ``src`` field.
    """

    model_config = {"populate_by_name": True}

    src: str = Field(..., alias="from", description="Source path to move.")
    dst: str = Field(..., alias="to", description="Destination path.")


class _CollectionSearchBody(BaseModel):
    """Body for ``POST /v1/collections/{id}/search``."""

    query: str = Field(
        ..., min_length=1, description="Free-text query string.",
    )
    top_k: int = Field(
        default=10, ge=1, le=100,
        description="Maximum number of hits to return.",
    )


# ---- Collection router -----------------------------------------------------

collection_router = make_crud_router(
    model_cls=Collection,
    storage_dep=get_collection_storage,
    plural="collections",
    tag="collections",
    cdc_kind="collection",
    managed_by_field="harness_id",
)


_BODY_CAP = 1024 * 1024  # 1 MiB, enforced at the API edge (spec section 3)


class _DocCreateBody(BaseModel):
    parent: str = Field(default="", description="Parent path; '' = root.")
    slug: str = Field(..., min_length=1)
    title: str | None = None
    body: str = Field(...)


class _DocPatchBody(BaseModel):
    body: str | None = None
    title: str | None = None


class _DocMoveBody(BaseModel):
    path: str = Field(..., min_length=1)
    new_parent: str = Field(default="")
    new_slug: str | None = None


async def _require_writable(collections, collection_id: str) -> None:
    coll = await collections.get(collection_id)
    if coll is None:
        raise NotFoundError(f"Collection {collection_id!r} does not exist")
    if coll.system:
        raise HTTPException(
            status_code=403,
            detail=(
                f"Collection {collection_id!r} is system-owned and "
                "read-only; it is regenerated from platform state."
            ),
        )


def _check_body_cap(body: str) -> None:
    if len(body.encode("utf-8")) > _BODY_CAP:
        raise RequestValidationError(errors=[{
            "type": "value_error", "loc": ("body", "body"),
            "msg": "document body exceeds the 1 MiB cap", "input": "<omitted>",
        }])


def _doc_json(document) -> dict:
    return document.model_dump(mode="json")


@collection_router.get(
    "/collections/{collection_id}/docs",
    summary="Read one document by slug path, or list the tree under a parent",
    responses=common_responses(404, 500),
)
async def get_or_list_docs(
    collection_id: str = Path(..., description="Collection id"),
    path: str | None = Query(
        default=None,
        description="Read the single document at this slug path.",
    ),
    parent: str | None = Query(
        default=None,
        description="List the tree under this parent path ('' = root).",
    ),
    depth: int = Query(default=1, ge=1, le=10),
    collections=Depends(get_collection_storage),
    service=Depends(get_document_tree_service),
) -> dict:
    """Read a node (body + children) or walk the tree under a parent."""
    coll = await collections.get(collection_id)
    if coll is None:
        raise NotFoundError(f"Collection {collection_id!r} does not exist")
    if path is not None:
        res = await service.read(collection_id=collection_id, path=path)
        return {
            "document": _doc_json(res.document),
            "body": res.body,
            "children": [c.model_dump() for c in res.children],
        }
    nodes = await service.tree(
        collection_id=collection_id, parent=parent or "", depth=depth,
    )
    return {"nodes": [n.model_dump() for n in nodes]}


@collection_router.post(
    "/collections/{collection_id}/docs",
    status_code=201,
    summary="Create a document node under a parent",
    responses=common_responses(404, 409, 422, 500),
)
async def create_doc(
    collection_id: str = Path(..., description="Collection id"),
    body: _DocCreateBody = Body(...),
    collections=Depends(get_collection_storage),
    service=Depends(get_document_tree_service),
) -> dict:
    await _require_writable(collections, collection_id)
    _check_body_cap(body.body)
    doc = await service.create(
        collection_id=collection_id, parent=body.parent, slug=body.slug,
        body=body.body, title=body.title,
    )
    return {"document": _doc_json(doc)}


@collection_router.patch(
    "/collections/{collection_id}/docs",
    summary="Update a document's body and/or title",
    responses=common_responses(404, 422, 500),
)
async def patch_doc(
    collection_id: str = Path(..., description="Collection id"),
    path: str = Query(..., description="Slug path of the document"),
    body: _DocPatchBody = Body(...),
    collections=Depends(get_collection_storage),
    service=Depends(get_document_tree_service),
) -> dict:
    await _require_writable(collections, collection_id)
    if body.body is not None:
        _check_body_cap(body.body)
    doc = await service.update(
        collection_id=collection_id, path=path, body=body.body, title=body.title,
    )
    return {"document": _doc_json(doc)}


@collection_router.post(
    "/collections/{collection_id}/docs/move",
    summary="Move a document (and its subtree) to a new parent and/or slug",
    responses=common_responses(400, 404, 409, 500),
)
async def move_doc(
    collection_id: str = Path(..., description="Collection id"),
    body: _DocMoveBody = Body(...),
    collections=Depends(get_collection_storage),
    service=Depends(get_document_tree_service),
) -> dict:
    await _require_writable(collections, collection_id)
    doc = await service.move(
        collection_id=collection_id, path=body.path,
        new_parent=body.new_parent, new_slug=body.new_slug,
    )
    return {"document": _doc_json(doc)}


@collection_router.delete(
    "/collections/{collection_id}/docs",
    status_code=204,
    summary="Delete a document, optionally with its subtree",
    responses=common_responses(404, 409, 500),
)
async def delete_doc(
    collection_id: str = Path(..., description="Collection id"),
    path: str = Query(..., description="Slug path of the document"),
    recursive: bool = Query(default=False),
    collections=Depends(get_collection_storage),
    service=Depends(get_document_tree_service),
) -> None:
    await _require_writable(collections, collection_id)
    await service.delete(
        collection_id=collection_id, path=path, recursive=recursive,
    )


@collection_router.get(
    "/collections/{collection_id}/grep",
    summary="Regex line search across the collection's document bodies",
    responses=common_responses(400, 404, 500),
)
async def grep_docs(
    request: Request,
    collection_id: str = Path(..., description="Collection id"),
    q: str = Query(..., min_length=1, description="Regex pattern"),
    path_prefix: str | None = Query(default=None),
    max_results: int = Query(default=50, ge=1, le=500),
    collections=Depends(get_collection_storage),
) -> dict:
    coll = await collections.get(collection_id)
    if coll is None:
        raise NotFoundError(f"Collection {collection_id!r} does not exist")
    result = await grep_collection(
        request.app.state.storage_provider.get_content_store(),
        collection_id=collection_id, pattern=q,
        path_prefix=path_prefix, max_results=max_results,
    )
    return result.model_dump(mode="json")


@collection_router.get(
    "/collections/{collection_id}/documents",
    summary="Read one document by path, or list documents under a prefix",
    responses=common_responses(404, 500),
)
async def get_or_list_collection_documents(
    collection_id: str = Path(..., description="Collection id"),
    path: str | None = Query(
        default=None,
        description=(
            "When set, return the single document at this path (body + "
            "metadata), or 404. The ?path= query form avoids slash-in-path "
            "segment routing issues, matching the workspace files/read "
            "convention."
        ),
    ),
    prefix: str | None = Query(
        default=None,
        description=(
            "Optional path prefix to scope the listing. Ignored when "
            "``path`` is set."
        ),
    ),
    collections=Depends(get_collection_storage),
    service=Depends(get_document_service),
) -> dict:
    """Path-addressed read + list, reconciled onto one GET.

    * ``?path=<p>`` set -> return the single document at that path as
      ``{"document": {...}, "content": "..."}`` (404 if missing).
    * no ``path`` -> list entries under the optional ``?prefix=`` as
      ``{"documents": [{path, document_id, size}, ...]}``, NO bodies. The
      listing is sourced from the content store, which is authoritative for
      ``(collection_id, path)`` resolution.
    """
    if await collections.get(collection_id) is None:
        raise NotFoundError(f"Collection {collection_id!r} does not exist")

    if path is not None:
        # DocumentService.read raises NotFoundError -> 404 via the handler.
        result = await service.read(collection_id=collection_id, path=path)
        return {
            "document": result.document.model_dump(mode="json"),
            "content": result.content,
        }

    entries = await service.list(collection_id=collection_id, prefix=prefix)
    return {"documents": [e.model_dump(mode="json") for e in entries]}


@collection_router.put(
    "/collections/{collection_id}/documents",
    summary="Create or replace a document at a path",
    responses=common_responses(404, 422, 500),
)
async def put_collection_document(
    collection_id: str = Path(..., description="Collection id"),
    path: str = Query(..., description="Path to create/replace within the collection"),
    body: _DocumentUpsertBody = Body(...),
    collections=Depends(get_collection_storage),
    service=Depends(get_document_service),
) -> dict:
    """Upsert the document at ``(collection_id, path)``.

    Writes the entity + body atomically, then (search on in P1) re-indexes
    the body via the service's indexer after the write commits. Returns the
    stored document metadata.
    """
    if await collections.get(collection_id) is None:
        raise NotFoundError(f"Collection {collection_id!r} does not exist")

    doc = await service.upsert(
        collection_id=collection_id,
        path=path,
        content=body.content,
        title=body.title,
        meta=body.meta,
    )
    return {"document": doc.model_dump(mode="json")}


@collection_router.delete(
    "/collections/{collection_id}/documents",
    summary="Delete a document by path",
    status_code=204,
    responses=common_responses(404, 500),
)
async def delete_collection_document(
    collection_id: str = Path(..., description="Collection id"),
    path: str = Query(..., description="Path to delete within the collection"),
    collections=Depends(get_collection_storage),
    service=Depends(get_document_service),
) -> None:
    """Delete the entity + body at ``(collection_id, path)`` atomically.

    Raises 404 (via NotFoundError) when no document lives at that path.
    """
    if await collections.get(collection_id) is None:
        raise NotFoundError(f"Collection {collection_id!r} does not exist")
    await service.delete(collection_id=collection_id, path=path)


@collection_router.post(
    "/collections/{collection_id}/documents/move",
    summary="Move a document from one path to another",
    status_code=204,
    responses=common_responses(404, 409, 500),
)
async def move_collection_document(
    collection_id: str = Path(..., description="Collection id"),
    body: _DocumentMoveBody = Body(...),
    collections=Depends(get_collection_storage),
    service=Depends(get_document_service),
) -> None:
    """Move ``from`` -> ``to`` within the collection.

    404 (NotFoundError) when ``from`` does not exist; 409 (ConflictError)
    when ``to`` is already occupied.
    """
    if await collections.get(collection_id) is None:
        raise NotFoundError(f"Collection {collection_id!r} does not exist")
    await service.move(collection_id=collection_id, src=body.src, dst=body.dst)


class _SearchEnableBody(BaseModel):
    """CollectionSearchConfig as the API accepts it: state and error are
    lifecycle-owned, never client-set."""

    embedder: CollectionEmbedder
    vector_store_provider_id: str = Field(..., min_length=1)
    cross_encoder: CollectionCrossEncoder | None = None
    chunking: ChunkingConfig = Field(default_factory=ChunkingConfig)


@collection_router.put(
    "/collections/{collection_id}/search",
    summary="Enable semantic search and backfill the collection",
    responses=common_responses(404, 409, 422, 500),
)
async def enable_collection_search(
    collection_id: str = Path(..., description="Collection id"),
    body: _SearchEnableBody = Body(...),
    collections=Depends(get_collection_storage),
    registry: ProviderRegistry = Depends(get_provider_registry),
    ssr=Depends(get_semantic_search_registry),
    request: Request = None,  # noqa: RUF013 - FastAPI fills this
) -> dict:
    """Turn semantic search on and index what is already there.

    The backfill runs inline: collections are text-only and bounded, so a
    caller learns the outcome from this response rather than polling. A
    failure leaves the partial index intact and the state in ``error``;
    re-issuing the same PUT retries.
    """
    await _require_writable(collections, collection_id)
    cfg = CollectionSearchConfig(**body.model_dump())
    updated = await enable_search(
        request.app.state.storage_provider, registry, ssr,
        collection_id=collection_id, cfg=cfg,
    )
    return updated.model_dump(mode="json")


@collection_router.delete(
    "/collections/{collection_id}/search",
    status_code=204,
    summary="Disable semantic search and drop the collection's vectors",
    responses=common_responses(404, 500),
)
async def disable_collection_search(
    collection_id: str = Path(..., description="Collection id"),
    collections=Depends(get_collection_storage),
    ssr=Depends(get_semantic_search_registry),
    request: Request = None,  # noqa: RUF013 - FastAPI fills this
) -> None:
    """Always succeeds: a missing provider or namespace still disables."""
    await _require_writable(collections, collection_id)
    await disable_search(
        request.app.state.storage_provider, ssr, collection_id=collection_id,
    )


@collection_router.get(
    "/collections/{collection_id}/search",
    summary="Report search state and indexing progress",
    responses=common_responses(404, 500),
)
async def get_collection_search_status(
    collection_id: str = Path(..., description="Collection id"),
    ssr=Depends(get_semantic_search_registry),
    request: Request = None,  # noqa: RUF013 - FastAPI fills this
) -> dict:
    status = await search_status(
        request.app.state.storage_provider, ssr, collection_id=collection_id,
    )
    return status.model_dump(mode="json")


@collection_router.post(
    "/collections/{collection_id}/import",
    summary="Import a zip archive's directory structure into the tree",
    responses=common_responses(400, 404, 409, 500),
)
async def import_collection_zip(
    collection_id: str = Path(..., description="Collection id"),
    file: UploadFile = File(...),
    parent: str = Query(default="", description="Parent path to import under"),
    conflict: Literal["fail", "skip", "overwrite"] = Query(default="fail"),
    collections=Depends(get_collection_storage),
    service=Depends(get_document_tree_service),
) -> dict:
    """Map an archive's directories onto the document tree.

    Directory segments and filenames are slugified, extensions dropped;
    binary or non-UTF-8 entries are reported rather than failing the whole
    import. ``conflict`` selects what happens at an existing path.
    """
    await _require_writable(collections, collection_id)
    raw = await file.read()
    if not raw:
        raise BadRequestError("uploaded archive is empty")
    # 32 MB cap, matching the single-file convert route.
    if len(raw) > 32 * 1024 * 1024:
        raise BadRequestError(
            f"uploaded archive is too large ({len(raw)} bytes); cap is 32 MB."
        )
    report = await import_zip(
        service, collection_id=collection_id, data=raw,
        parent=parent, conflict=conflict,
    )
    return report.model_dump(mode="json")


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
    similarity search against the collection's vector store (both
    resolved from ``Collection.search``), scoped to this collection.
    Returns ``{"hits": [{document_id, chunk_id, score, text, path,
    meta}, ...]}``.

    The collection must have semantic search enabled: without a search
    block this answers 409. An enabled collection with nothing indexed
    yet returns an empty hits list. The embedder is the one declared on
    ``Collection.search.embedder``, the same one the ingest pipeline
    used when storing chunks, so query and index vectors live in the
    same embedding space.
    """
    coll = await collections.get(collection_id)
    if coll is None:
        raise NotFoundError(f"Collection {collection_id!r} does not exist")

    if coll.search is None:
        raise ConflictError(
            f"semantic search is not enabled on collection {collection_id!r}; "
            "enable it with PUT /v1/collections/{id}/search. grep and the "
            "document tree remain available."
        )
    if coll.search.state == "error":
        raise ConfigError(
            f"semantic search on {collection_id!r} is in error state: "
            f"{coll.search.error}"
        )

    # Vectorise the query with the collection's own embedder so query
    # and index vectors agree on dimensionality + distance metric.
    embedder = await registry.get_embedder(coll.search.embedder.provider_id)
    response = await embedder.embed(
        model=coll.search.embedder.model,
        inputs=[TextPart(text=body.query)],
    )
    vector = list(response.embeddings[0].vector)

    store = await ssr.get_store(coll.search.vector_store_provider_id)
    # SSP registration is lazy: the vector store's collection is created
    # only when the first chunk is indexed. A collection that has Document
    # rows but no indexed vectors yet (live embedding on create is a
    # follow-up) is therefore unknown to the store's catalogue, and
    # search raises BadRequestError("...is not registered..."). Treat
    # that as "nothing indexed yet" and return an empty hits list rather
    # than surfacing a 400, matching list_indexed_documents and the
    # docstring's empty-collection contract.
    #
    # run_collection_search honours the collection's cross-encoder when
    # configured and runs a plain vector search otherwise, reusing the
    # query vector we already embedded so that path does not double-embed.
    try:
        hits = await run_collection_search(
            collection=coll,
            embedder=embedder,
            store=store,
            query=body.query,
            top_k=body.top_k,
            cross_encoder_resolver=registry,
            query_vector=vector,
        )
    except BadRequestError as exc:
        if "is not registered" not in str(exc):
            raise
        hits = []
    return {
        "hits": [
            {
                "document_id": h.record.document_id,
                "chunk_id": h.record.chunk_id,
                "score": h.score,
                "text": h.record.text,
                "path": h.record.meta.get("path"),
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
    document_id: str | None = Query(
        default=None,
        description=(
            "When set, return only the chunks belonging to this document "
            "id. Used by the 'view chunks of a document' UI."
        ),
    ),
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

    When ``document_id`` is supplied, the result is filtered to that
    single document's chunks before the offset/limit window is applied,
    so the UI can show "all chunks of this document".

    Pagination today is in-process (the vector-store ABC has no native
    offset/limit). The records list is sorted deterministically by
    ``(document_id, chunk_id)`` so a slice is stable across calls.
    """
    coll = await collections.get(collection_id)
    if coll is None:
        raise NotFoundError(f"Collection {collection_id!r} does not exist")

    if coll.search is None:
        raise ConflictError(
            f"semantic search is not enabled on collection {collection_id!r}; "
            "enable it with PUT /v1/collections/{id}/search. grep and the "
            "document tree remain available."
        )
    store = await ssr.get_store(coll.search.vector_store_provider_id)
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
    if document_id is not None:
        records = [r for r in records if r.document_id == document_id]
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


def build_document_unindexer(request: Request):
    """Best-effort per-document vector cleanup for tree deletes."""
    from primer.knowledge.indexing import remove_document_index

    storage_provider = request.app.state.storage_provider

    async def _unindexer(*, document_id: str, collection_id: str) -> None:
        collection = await storage_provider.get_storage(Collection).get(collection_id)
        if collection is None:
            return
        try:
            ssr = get_semantic_search_registry(request)
            await remove_document_index(
                document_id=document_id, collection=collection,
                semantic_search_registry=ssr,
            )
        except Exception:  # noqa: BLE001 - best-effort cleanup
            logger.exception(
                "document %s: unindexing failed; chunks may linger", document_id,
            )

    return _unindexer


def build_document_path_rewriter(request: Request):
    """Best-effort chunk path-metadata rewrite after a tree move."""
    from primer.knowledge.indexing import rewrite_document_path_meta

    storage_provider = request.app.state.storage_provider

    async def _rewriter(
        *, document_id: str, collection_id: str, new_path: str
    ) -> None:
        collection = await storage_provider.get_storage(Collection).get(collection_id)
        if collection is None:
            return
        try:
            ssr = get_semantic_search_registry(request)
            await rewrite_document_path_meta(
                document_id=document_id, collection=collection,
                semantic_search_registry=ssr, new_path=new_path,
            )
        except Exception:  # noqa: BLE001 - best-effort metadata fix
            logger.exception(
                "document %s: path meta rewrite failed; hits may show the "
                "pre-move path", document_id,
            )

    return _rewriter


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


def build_document_indexer(request: Request):
    """Return the best-effort indexer the path-addressed routes wire into
    :class:`DocumentService`.

    The returned async callable ``(document=..., content=...)`` is invoked by
    the service AFTER its atomic entity + content write commits, so the body
    is durable in the content store before any embedding work begins. It
    mirrors the CRUD ``_index_document_hook`` best-effort contract: a missing
    collection is a no-op, a dimension mismatch surfaces as 422, and any
    other embedder/store failure is logged and swallowed so the write still
    succeeds. There is no double-index risk: the path-addressed routes do not
    go through ``make_crud_router``, so the Document CDC hook never fires for
    these writes.
    """
    from primer.knowledge.indexing import index_document

    storage_provider = request.app.state.storage_provider

    async def _indexer(*, document: Document, content: str) -> None:
        collection = await storage_provider.get_storage(Collection).get(
            document.collection_id
        )
        if collection is None:
            return
        try:
            provider_registry = get_provider_registry(request)
            ssr = get_semantic_search_registry(request)
            await index_document(
                document=document,
                collection=collection,
                provider_registry=provider_registry,
                semantic_search_registry=ssr,
                content_store=storage_provider.get_content_store(),
            )
        except DimensionMismatchError:
            # Operator-configuration error: surface as 422, do not swallow.
            raise
        except Exception:  # noqa: BLE001 - best-effort indexing
            logger.exception(
                "document %s: indexing failed; row persisted but not searchable",
                document.id,
            )

    return _indexer


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


def build_document_indexer(request: Request):
    """Return the best-effort indexer the path-addressed routes wire into
    :class:`DocumentService`.

    The returned async callable ``(document=..., content=...)`` is invoked by
    the service AFTER its atomic entity + content write commits, so the body
    is durable in the content store before any embedding work begins. It
    mirrors the CRUD ``_index_document_hook`` best-effort contract: a missing
    collection is a no-op, a dimension mismatch surfaces as 422, and any
    other embedder/store failure is logged and swallowed so the write still
    succeeds. There is no double-index risk: the path-addressed routes do not
    go through ``make_crud_router``, so the Document CDC hook never fires for
    these writes.
    """
    from primer.knowledge.indexing import index_document

    storage_provider = request.app.state.storage_provider

    async def _indexer(*, document: Document, content: str) -> None:
        collection = await storage_provider.get_storage(Collection).get(
            document.collection_id
        )
        if collection is None:
            return
        try:
            provider_registry = get_provider_registry(request)
            ssr = get_semantic_search_registry(request)
            await index_document(
                document=document,
                collection=collection,
                provider_registry=provider_registry,
                semantic_search_registry=ssr,
                content_store=storage_provider.get_content_store(),
            )
        except DimensionMismatchError:
            # Operator-configuration error: surface as 422, do not swallow.
            raise
        except Exception:  # noqa: BLE001 - best-effort indexing
            logger.exception(
                "document %s: indexing failed; row persisted but not searchable",
                document.id,
            )

    return _indexer


async def _index_document_hook(document_id: str, request: Request) -> None:
    """on_create / on_update hook: chunk, embed, and index the document.

    Best-effort: an embedder/store failure is logged but does not fail
    the CRUD write, so the Document row still persists when the embedding
    backend is misconfigured or down. System collections are skipped by
    the indexer itself.
    """
    from primer.knowledge.indexing import index_document

    storage_provider = request.app.state.storage_provider
    doc = await storage_provider.get_storage(Document).get(document_id)
    if doc is None:
        return
    collection = await storage_provider.get_storage(Collection).get(
        doc.collection_id
    )
    if collection is None:
        return
    try:
        from primer.api.deps import (
            get_provider_registry,
            get_semantic_search_registry,
        )

        provider_registry = get_provider_registry(request)
        ssr = get_semantic_search_registry(request)
        await index_document(
            document=doc,
            collection=collection,
            provider_registry=provider_registry,
            semantic_search_registry=ssr,
            content_store=storage_provider.get_content_store(),
        )
    except DimensionMismatchError:
        # Dimension mismatches are operator-configuration errors that must
        # surface to the caller as 422, not be swallowed. Re-raise so the
        # FastAPI error handler can render the RFC 7807 problem response.
        raise
    except Exception:  # noqa: BLE001 - best-effort indexing
        logger.exception(
            "document %s: indexing failed; row persisted but not searchable",
            document_id,
        )


async def _unindex_document_hook(
    stored: Document, request: Request
) -> None:
    """on_pre_delete hook: drop the document's indexed chunks before the
    row is removed. Best-effort."""
    from primer.knowledge.indexing import remove_document_index

    storage_provider = request.app.state.storage_provider
    collection = await storage_provider.get_storage(Collection).get(
        stored.collection_id
    )
    if collection is None:
        return
    try:
        from primer.api.deps import get_semantic_search_registry

        ssr = get_semantic_search_registry(request)
        await remove_document_index(
            document_id=stored.id,
            collection=collection,
            semantic_search_registry=ssr,
        )
    except Exception:  # noqa: BLE001 - best-effort cleanup
        logger.exception(
            "document %s: unindexing failed; chunks may linger",
            stored.id,
        )


document_router = make_crud_router(
    model_cls=Document,
    storage_dep=get_document_storage,
    plural="documents",
    tag="documents",
    managed_by_field="harness_id",
    on_pre_create=_document_pre_create,
    on_pre_update=_document_pre_update,
    on_create=_index_document_hook,
    on_update=_index_document_hook,
    on_pre_delete=_unindex_document_hook,
)


__all__ = [
    "collection_router",
    "document_router",
]
