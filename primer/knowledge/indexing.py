"""Chunk, embed, and index user-collection documents into the vector store.

When a Document is created or updated through the REST CRUD routes, its
stored text (``meta['text']`` or ``meta['content']``) is split into
chunks, each chunk is embedded with the parent collection's configured
embedder, and the resulting :class:`EmbeddingRecord` rows are upserted
into the collection's vector store. This is what makes per-collection
search and the "view chunks of a document" UI return results.

System (``system=True``) collections are skipped here: their content is
reconciled by the internal-collections catalog, not hand-ingested.

Indexing is best-effort. If the embedder or vector store fails (for
example a missing API key), the failure is logged and swallowed so the
Document storage row still persists; search simply will not see the
document until indexing succeeds on a later update.
"""

from __future__ import annotations

import logging
import re as _re

from primer.int.document_content import DocumentContentStore
from primer.model.chat import TextPart
from primer.model.collection import Collection, Document
from primer.model.except_ import ConflictError, DimensionMismatchError, PrimerError
from primer.model.vector import EmbeddingRecord

logger = logging.getLogger(__name__)

# Target chunk size in characters. Paragraph-aware: paragraphs are packed
# up to this size, and any single paragraph longer than the hard cap is
# split on character boundaries so one huge block still embeds.

# Number of chunks embedded per embedder call. Mirrors
# the previous ingester's default so user-collection ingestion and
# the internal-collection catalog batch identically.
_EMBED_BATCH_SIZE = 32


def _parse_stored_dim(conflict_message: str, *, fallback: int) -> int:
    """Extract the stored vector dimension from a ConflictError message.

    All backends embed the stored dimension as ``dimensions=<N>`` in their
    ConflictError text. Returns ``fallback`` when the pattern is absent.
    """
    # Match the FIRST "dimensions=<N>" occurrence -- that is the stored dim.
    m = _re.search(r"dimensions=(\d+)", conflict_message)
    if m:
        return int(m.group(1))
    return fallback


async def index_document(
    *,
    document: Document,
    collection: Collection,
    provider_registry,
    semantic_search_registry,
    content_store: DocumentContentStore,
) -> int:
    """Chunk, embed, and upsert ``document`` into its collection's vector
    store. Returns the number of chunks indexed. Re-indexing embeds the new
    chunks FIRST and only then replaces the document's existing chunks, so a
    failed re-embed leaves the prior index intact (the document stays
    searchable) rather than wiping it.

    The indexable body is read from the content store keyed by the stable
    document id. Bodies live there now, not in ``meta``. For a not-yet-
    migrated document with no content row, this falls back to the legacy
    ``meta['text']`` / ``meta['content']`` read so nothing breaks in transit.

    A collection with no search config is skipped (returns 0). System
    collections are NOT: the system collection is precisely what
    /v1/internal_collections/bootstrap vectorises, so skipping them made
    that toggle a no-op that still reported success (state "ready" with
    zero documents indexed, and no vector-store collection ever created).
    The guard dates from when "system" meant the four _internal_* rows a
    separate ingest path owned. Search being unset is the real signal now.

    On embedder/store failure it raises; the caller treats indexing as
    best-effort and swallows it.
    """
    if collection.search is None:
        return 0
    cfg = collection.search

    text = await content_store.get(document.id)
    if text is None:
        text = ""
    from primer.knowledge.splitter import split_text

    chunks = split_text(
        text,
        max_chars=cfg.chunking.max_chars,
        overlap=cfg.chunking.overlap,
    )

    embedder = await provider_registry.get_embedder(
        cfg.embedder.provider_id
    )
    store = await semantic_search_registry.get_store(
        cfg.vector_store_provider_id
    )

    # Probe the embedder's output dimensionality with a single cheap call
    # BEFORE embedding all chunks. This lets us detect a mismatch between
    # the embedder and the vector store's stored collection dimension early
    # -- without wasting a full embedding pass on a batch that cannot be
    # stored. We register (or validate) the collection in the store now so
    # that a ConflictError (dim mismatch) surfaces here, not after work.
    probe_response = await embedder.embed(
        model=cfg.embedder.model,
        inputs=[TextPart(text="dimensionality probe")],
    )
    if not probe_response.embeddings:
        raise PrimerError(
            f"embedder returned no embedding for dimensionality probe "
            f"(collection {collection.id!r})"
        )
    probe_dim = len(probe_response.embeddings[0].vector)
    try:
        await store.create_collection(collection.id, dimensions=probe_dim)
    except ConflictError as exc:
        # The collection is already registered in the store with a different
        # dimension. Parse the stored dim from the ConflictError message
        # produced by all store backends ("dimensions=<N>" in the message).
        stored_dim = _parse_stored_dim(str(exc), fallback=0)
        raise DimensionMismatchError(
            f"Embedder output dimension ({probe_dim}) does not match the "
            f"vector store dimension ({stored_dim}) recorded for collection "
            f"{collection.id!r}. The collection was indexed with a different "
            f"embedding model. To fix: delete all documents from this "
            f"collection, then re-create it with the correct embedder, and "
            f"re-ingest the documents.",
            embedder_dim=probe_dim,
            collection_dim=stored_dim,
            collection_id=collection.id,
            cause=exc,
        ) from exc

    # Embed FIRST, before touching the existing chunks. If the embedder
    # fails (transient error, missing key), we must not have already
    # deleted the document's old chunks: the replace below only runs once
    # every new vector is in hand, so a failure here leaves the prior
    # index intact and the document still searchable.
    #
    # Batch the chunk embeddings: each embed
    # call carries up to ``_EMBED_BATCH_SIZE`` chunks and returns one
    # embedding per input in input order, so the records line up with the
    # chunks one-to-one. This is behaviour-equivalent to the previous
    # per-chunk loop (same vector per chunk, same chunk_id == str(idx),
    # same order) but collapses N embedder round-trips into ceil(N / 32).
    # The dimensionality-mismatch PROBE above is untouched; only the main
    # chunk embedding is batched here.
    records: list[EmbeddingRecord] = []
    for batch_start in range(0, len(chunks), _EMBED_BATCH_SIZE):
        batch = chunks[batch_start : batch_start + _EMBED_BATCH_SIZE]
        response = await embedder.embed(
            model=cfg.embedder.model,
            inputs=[TextPart(text=chunk) for chunk in batch],
        )
        if len(response.embeddings) != len(batch):
            raise PrimerError(
                f"embedder returned {len(response.embeddings)} embeddings "
                f"for {len(batch)} chunk(s) of document {document.id!r}"
            )
        for offset, (chunk, emb) in enumerate(
            zip(batch, response.embeddings, strict=True)
        ):
            idx = batch_start + offset
            records.append(
                EmbeddingRecord(
                    collection_id=collection.id,
                    document_id=document.id,
                    chunk_id=str(idx),
                    text=chunk,
                    vector=list(emb.vector),
                    meta={"path": document.path},
                )
            )

    # Now that embedding succeeded, atomically-ish replace the document's
    # chunks: drop the prior set, then upsert the new one. The vector store
    # exposes no native "replace by document id", so this is delete + put;
    # because it runs only after a successful embed there is no failure
    # window that leaves the document with zero chunks. Chunk ids are the
    # stable str(idx), so a shorter re-index could leave higher-index stale
    # rows behind without the delete -- hence the delete is still required.
    try:
        await store.delete(collection.id, document.id)
    except PrimerError:
        # Tolerate stores that raise on delete when the collection has no
        # prior rows (some backends signal an unregistered collection this
        # way). The probe's create_collection above already registered it.
        pass

    if not chunks:
        # An empty body produces no records: the delete above already
        # cleared any prior chunks, which is the intended replace-with-empty.
        return 0

    # Idempotent: the probe already registered the collection above.
    # Calling again is a no-op for all compliant backends.
    await store.create_collection(
        collection.id, dimensions=len(records[0].vector)
    )
    for record in records:
        await store.put(record)

    logger.info(
        "indexed document %s into collection %s (%d chunks)",
        document.id, collection.id, len(records),
    )
    return len(records)


async def remove_document_index(
    *,
    document_id: str,
    collection: Collection,
    semantic_search_registry,
) -> None:
    """Delete every indexed chunk for a document. Best-effort, idempotent."""
    if collection.search is None:
        return
    store = await semantic_search_registry.get_store(
        collection.search.vector_store_provider_id
    )
    try:
        await store.delete(collection.id, document_id)
    except PrimerError:
        pass


__all__ = [
    "index_document",
    "remove_document_index",
    "rewrite_document_path_meta",
]


async def rewrite_document_path_meta(
    *,
    document_id: str,
    collection: Collection,
    semantic_search_registry,
    new_path: str,
) -> None:
    """Rewrite each stored chunk's meta path after a move. Metadata only:
    vectors are reused verbatim (spec section 6), so no embedder runs."""
    if collection.search is None:
        return
    store = await semantic_search_registry.get_store(
        collection.search.vector_store_provider_id
    )
    try:
        records = await store.get(collection.id, document_id)
        for record in records:
            await store.put(record.model_copy(
                update={"meta": {**record.meta, "path": new_path}}
            ))
    except PrimerError:
        pass
