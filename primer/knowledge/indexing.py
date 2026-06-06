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

from primer.model.chat import TextPart
from primer.model.collection import Collection, Document
from primer.model.except_ import PrimerError
from primer.model.vector import EmbeddingRecord

logger = logging.getLogger(__name__)

# Target chunk size in characters. Paragraph-aware: paragraphs are packed
# up to this size, and any single paragraph longer than the hard cap is
# split on character boundaries so one huge block still embeds.
_CHUNK_TARGET_CHARS = 1500
_CHUNK_HARD_CAP = 3000


def _document_text(doc: Document) -> str:
    """Extract the indexable body text from a Document.

    The REST create form stores prose under ``meta['text']``; the
    system toolset's ``put_document`` uses ``meta['content']``. The
    name is metadata, not body, so it is not indexed: a document with
    no text body produces no chunks.
    """
    meta = doc.meta or {}
    for key in ("text", "content"):
        val = meta.get(key)
        if isinstance(val, str) and val.strip():
            return val
    return ""


def chunk_text(text: str) -> list[str]:
    """Split text into embedding-sized chunks, paragraph-aware.

    Paragraphs (split on blank lines) are packed greedily up to
    ``_CHUNK_TARGET_CHARS``. A paragraph longer than ``_CHUNK_HARD_CAP``
    on its own is hard-split so no single chunk is unbounded. Returns an
    empty list for empty input.
    """
    text = (text or "").strip()
    if not text:
        return []
    paragraphs = [p.strip() for p in text.split("\n\n") if p.strip()]
    if not paragraphs:
        paragraphs = [text]

    chunks: list[str] = []
    current = ""
    for para in paragraphs:
        # Hard-split an over-long paragraph on character boundaries.
        if len(para) > _CHUNK_HARD_CAP:
            if current:
                chunks.append(current)
                current = ""
            for i in range(0, len(para), _CHUNK_TARGET_CHARS):
                chunks.append(para[i:i + _CHUNK_TARGET_CHARS])
            continue
        if not current:
            current = para
        elif len(current) + 2 + len(para) <= _CHUNK_TARGET_CHARS:
            current = f"{current}\n\n{para}"
        else:
            chunks.append(current)
            current = para
    if current:
        chunks.append(current)
    return chunks


async def index_document(
    *,
    document: Document,
    collection: Collection,
    provider_registry,
    semantic_search_registry,
) -> int:
    """Chunk, embed, and upsert ``document`` into its collection's vector
    store. Returns the number of chunks indexed. Re-indexing first drops
    the document's existing chunks so stale chunks do not linger.

    System collections are skipped (returns 0). Raises nothing on
    embedder/store failure; the caller treats this as best-effort.
    """
    if collection.system:
        return 0

    text = _document_text(document)
    chunks = chunk_text(text)

    embedder = await provider_registry.get_embedder(
        collection.embedder.provider_id
    )
    store = await semantic_search_registry.get_store(
        collection.search_provider_id
    )

    # Always clear prior chunks for this document so an update replaces
    # rather than accumulates. Idempotent no-op when nothing is indexed.
    try:
        await store.delete(collection.id, document.id)
    except PrimerError:
        # The collection may not be registered yet (first ingest); the
        # create_collection below handles registration.
        pass

    if not chunks:
        return 0

    # Embed all chunks; one embed call per chunk keeps the code simple
    # and matches the catalog's per-record approach. The first chunk's
    # vector length determines the collection dimensionality.
    records: list[EmbeddingRecord] = []
    for idx, chunk in enumerate(chunks):
        response = await embedder.embed(
            model=collection.embedder.model,
            inputs=[TextPart(text=chunk)],
        )
        if not response.embeddings:
            raise PrimerError(
                f"embedder returned no embedding for document "
                f"{document.id!r} chunk {idx}"
            )
        vector = list(response.embeddings[0].vector)
        records.append(
            EmbeddingRecord(
                collection_id=collection.id,
                document_id=document.id,
                chunk_id=str(idx),
                text=chunk,
                vector=vector,
                meta={"document_name": document.name},
            )
        )

    # Register the collection in the store (idempotent) using the actual
    # embedding dimensionality, then upsert every chunk.
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
    if collection.system:
        return
    store = await semantic_search_registry.get_store(
        collection.search_provider_id
    )
    try:
        await store.delete(collection.id, document_id)
    except PrimerError:
        pass


async def backfill_missing_document_vectors(
    *,
    storage_provider,
    provider_registry,
    semantic_search_registry,
) -> int:
    """Index every user document that has no vector chunks yet.

    The embed-on-ingest hook only fires when a Document is created or
    updated. Documents that were stored before that hook existed (or whose
    embedding failed at ingest time, since indexing is best-effort) keep a
    storage row but never land in the vector store, so per-collection search
    and the "view chunks" UI return nothing for them. This startup pass
    closes that gap and is the system's self-healing path for any document
    whose embedding was missed.

    The check is cheap and idempotent: for each non-system collection we ask
    the vector store once for the set of document ids that already have
    chunks (``search_by_meta(meta={})``), then index only the documents
    missing from that set. A collection that has never been registered in
    the store raises, which we treat as "no documents indexed yet". On a
    healthy boot where everything is already indexed, no embeds run.

    Returns the number of documents (re)indexed. Best-effort throughout:
    a failure on one collection or document is logged and skipped so a bad
    embedder never blocks startup.
    """
    from primer.model.collection import Collection, Document
    from primer.model.storage import OffsetPage

    doc_storage = storage_provider.get_storage(Document)
    coll_storage = storage_provider.get_storage(Collection)

    # Group documents by collection so the "already indexed" lookup runs
    # once per collection rather than once per document.
    docs_by_collection: dict[str, list[Document]] = {}
    offset = 0
    page_size = 200
    while True:
        page = await doc_storage.list(OffsetPage(offset=offset, length=page_size))
        for doc in page.items:
            docs_by_collection.setdefault(doc.collection_id, []).append(doc)
        if len(page.items) < page_size:
            break
        offset += page_size

    indexed = 0
    for collection_id, docs in docs_by_collection.items():
        try:
            collection = await coll_storage.get(collection_id)
        except PrimerError:
            collection = None
        if collection is None or collection.system:
            continue

        # Which documents already have chunks? One query per collection.
        # An unregistered collection (never embedded) raises; treat as empty.
        try:
            store = await semantic_search_registry.get_store(
                collection.search_provider_id
            )
            existing = await store.search_by_meta(collection.id, meta={})
            indexed_doc_ids = {r.document_id for r in existing}
        except PrimerError:
            indexed_doc_ids = set()
        except Exception:
            logger.exception(
                "backfill: failed to read existing chunks for collection %s",
                collection_id,
            )
            continue

        for doc in docs:
            if doc.id in indexed_doc_ids:
                continue
            try:
                n = await index_document(
                    document=doc,
                    collection=collection,
                    provider_registry=provider_registry,
                    semantic_search_registry=semantic_search_registry,
                )
                if n:
                    indexed += 1
            except Exception:
                logger.exception(
                    "backfill: failed to index document %s in collection %s",
                    doc.id, collection_id,
                )

    if indexed:
        logger.info("backfill: indexed %d previously unindexed document(s)", indexed)
    return indexed


__all__ = [
    "backfill_missing_document_vectors",
    "chunk_text",
    "index_document",
    "remove_document_index",
]
