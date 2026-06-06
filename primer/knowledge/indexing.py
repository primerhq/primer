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


__all__ = [
    "chunk_text",
    "index_document",
    "remove_document_index",
]
