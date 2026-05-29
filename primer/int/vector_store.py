"""Abstract base class for vector stores.

Sibling of :class:`primer.int.LLM`, :class:`primer.int.Embedder`,
:class:`primer.int.ToolsetProvider`, and :class:`primer.int.Storage`.
A :class:`VectorStore` instance is bound to one backend (in-memory,
FAISS, pgvector, Qdrant, Pinecone, Weaviate, etc.) and accepts
:class:`primer.model.vector.EmbeddingRecord` rows keyed by the
composite ``(collection_id, document_id, chunk_id)``.

The interface covers the full "register a collection, index its chunked
documents, search by vector, fetch back, retire" lifecycle:

* :meth:`VectorStore.create_collection` -- declare a collection and its
  vector dimensionality. Backends that materialise per-collection
  storage (a vector table, an index namespace) do their setup here.
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
from typing import Any, Literal

from primer.model.vector import EmbeddingRecord, SearchResult, Vector


class VectorStore(ABC):
    """Provider-agnostic interface to a vector index.

    Subclasses bind to a single backend. One vector store may host
    embeddings from many collections; the backend partitions internally
    by ``collection_id`` if needed.
    """

    @abstractmethod
    async def create_collection(
        self,
        collection_id: str,
        *,
        dimensions: int,
        distance: Literal["cosine", "l2", "ip"] = "cosine",
    ) -> None:
        """Register a new collection and prepare its storage / index.

        Idempotent: calling for an already-registered collection with
        the same dimensions and distance metric is a no-op. Calling
        with a different dimensionality after data has been inserted
        is a :class:`primer.model.except_.ConflictError`.

        Parameters
        ----------
        collection_id
            Identifier of the collection. Backends that derive table /
            index names from this MUST sanitise it (allow only
            ``[A-Za-z0-9_-]``); reject other characters with
            :class:`primer.model.except_.BadRequestError`.
        dimensions
            Length of the vectors that will be stored in this
            collection. Must match the embedding model's output
            dimensionality.
        distance
            Distance metric used for similarity search. Defaults to
            ``"cosine"`` (the conventional choice for normalised
            embeddings). Backends MAY override the default at the
            provider config level.
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
        collection_id: str,
        vector: Vector,
        k: int,
    ) -> list[SearchResult]:
        """Return the top ``k`` records most similar to ``vector`` within ``collection_id``.

        Parameters
        ----------
        collection_id
            Scopes the search to a single collection. Cross-collection
            search is intentionally not supported -- collections may
            have different dimensionalities and distance metrics, so
            mixing their results is meaningless. Callers that need to
            search several collections issue one call per collection
            and merge the result lists themselves.
        vector
            The query vector. Length must match the dimensionality
            declared for ``collection_id`` at
            :meth:`create_collection` time; backends raise
            :class:`primer.model.except_.BadRequestError` on mismatch.
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
            :class:`primer.model.vector.SearchResult` for the
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
    async def search_by_meta(
        self,
        collection_id: str,
        meta: dict[str, Any],
    ) -> list[EmbeddingRecord]:
        """Return every record in ``collection_id`` whose ``meta`` matches.

        Match semantics: a record matches when, for every ``(key,
        value)`` pair in the supplied ``meta``, the record's ``meta``
        has that key with that value. Nested objects match
        recursively (sub-keys inside ``value`` must also be present
        and equal in the record). Keys absent from the supplied
        ``meta`` are unconstrained. Passing an empty ``meta`` matches
        every record in the collection.

        Parameters
        ----------
        collection_id
            Scopes the search to a single collection.
        meta
            Key / value pairs the record's ``meta`` must contain. Use
            JSON-compatible scalars (str, int, float, bool, None) or
            nested dicts of the same.

        Returns
        -------
        list[EmbeddingRecord]
            Matching records ordered by ``(document_id, chunk_id)``
            ascending. No similarity scoring is involved -- this is a
            pure metadata filter.

        Raises
        ------
        primer.model.except_.BadRequestError
            ``collection_id`` has not been created.
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

    @abstractmethod
    async def drop_collection(self, collection_id: str) -> None:
        """Drop the collection and all its vectors.

        Idempotent: dropping a non-existent collection is a successful
        no-op. Used by callers that need to wipe and recreate (e.g. when
        switching embedding models, since vectors from a different model
        can't be searched against a new query embedding).
        """
