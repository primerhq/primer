"""Abstract base class for vector stores.

Sibling of :class:`matrix.int.LLM`, :class:`matrix.int.Embedder`,
:class:`matrix.int.ToolsetProvider`, and :class:`matrix.int.Storage`.
A :class:`VectorStore` instance is bound to one backend (in-memory,
FAISS, pgvector, Qdrant, Pinecone, Weaviate, etc.) and accepts
:class:`matrix.model.vector.EmbeddingRecord` rows keyed by the
composite ``(collection_id, document_id, chunk_id)``.

The interface is intentionally narrow -- four methods cover the full
"index a chunked document, search by vector, fetch its embeddings,
retire the document" lifecycle:

* :meth:`VectorStore.put` -- insert-or-replace one embedding row.
* :meth:`VectorStore.search` -- return the top ``k`` records most
  similar to the query vector. Search semantics are
  backend-specific (cosine similarity, dot product, hybrid lexical+
  semantic, etc.); callers should treat the ranking as a black box
  and use the optional similarity score only for relative comparison
  within a single result list.
* :meth:`VectorStore.get` -- return every chunk for a given document.
* :meth:`VectorStore.delete` -- remove every chunk for a given
  document.

Note the asymmetry: ``put`` operates on a single row, while ``get`` /
``delete`` operate on a whole document at once. That matches how
chunked-document pipelines actually work -- chunks are produced one at
a time but retired together with their parent document.
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from matrix.model.vector import EmbeddingRecord, SearchResult, Vector


class VectorStore(ABC):
    """Provider-agnostic interface to a vector index.

    Subclasses bind to a single backend. One vector store may host
    embeddings from many collections; the backend partitions internally
    by ``collection_id`` if needed.
    """

    @abstractmethod
    async def put(self, record: EmbeddingRecord) -> None:
        """Insert or replace one embedding row.

        Upsert semantics: if a record with the same
        ``(collection_id, document_id, chunk_id)`` triple already
        exists, it is replaced wholesale (vector, text, and meta).
        Concurrent ``put`` of the same key is the backend's
        responsibility to serialise; the caller sees last-write-wins
        without explicit locking.

        Returns ``None`` -- backends that auto-populate fields (an
        internal row id, a stored timestamp) do not surface them
        through this interface; callers re-read via :meth:`get` if
        needed.
        """

    @abstractmethod
    async def search(
        self,
        vector: Vector,
        k: int,
    ) -> list[SearchResult]:
        """Return the top ``k`` records most similar to ``vector``.

        Parameters
        ----------
        vector
            The query vector. Length should match the dimensionality
            of vectors previously inserted; backends MAY raise
            :class:`matrix.model.except_.BadRequestError` on mismatch.
        k
            Maximum number of results to return. Backends MAY return
            fewer (e.g. when the index holds fewer than ``k`` rows or
            when post-filtering trims hits).

        Returns
        -------
        list[SearchResult]
            Hits ordered by relevance, most similar first. The
            :attr:`SearchResult.score` field is populated when the
            backend exposes a similarity metric (always normalised so
            higher = more similar; see
            :class:`matrix.model.vector.SearchResult` for the
            convention). An empty list is returned when nothing
            matches.

        Notes
        -----
        Actual ranking semantics -- pure cosine similarity, dot
        product, hybrid lexical+semantic, learned re-ranker -- are
        the backend's choice. Callers should treat the order as
        opaque and not rely on score values being comparable across
        stores.
        """

    @abstractmethod
    async def get(
        self,
        collection_id: str,
        document_id: str,
    ) -> list[EmbeddingRecord]:
        """Return every chunk's embedding for one document.

        Results are ordered by ``chunk_id`` ascending so callers can
        rely on deterministic order across calls and across backends.
        Returns an empty list if no chunks exist for the
        ``(collection_id, document_id)`` pair -- this is not an
        error.
        """

    @abstractmethod
    async def delete(
        self,
        collection_id: str,
        document_id: str,
    ) -> None:
        """Remove every chunk's embedding for one document.

        Idempotent: deleting a document that has no chunks is a
        successful no-op. Use this to retire a document from the
        index before re-indexing or after the document is removed
        from its collection.
        """
