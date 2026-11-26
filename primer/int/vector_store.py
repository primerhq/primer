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

from primer.model.except_ import BadRequestError, PrimerError
from primer.model.vector import EmbeddingRecord, SearchResult, Vector


class VectorCollectionNotRegisteredError(BadRequestError):
    """The backend catalogue has no entry for the requested collection."""


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
        distance: Literal["cosine", "l2", "ip"] | None = None,
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
            Distance metric for a NEW collection. ``None`` (the default)
            resolves to the provider's configured distance metric. A
            collection that already exists keeps the metric stored at
            creation time: a different requested metric is a
            :class:`primer.model.except_.ConflictError` (the message
            names the stored and the requested metric), and callers
            that keep indexing (the knowledge layer) treat that drift
            as "continue under the stored metric", not as an error.
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

    async def put_many(self, records: list[EmbeddingRecord]) -> None:
        """Insert or replace many rows. The default is a loop over :meth:`put`; backends override it with one write (pgvector: one
        transaction and one statement; Lance: one table version rather than one per chunk, KN-15)."""
        for record in records:
            await self.put(record)

    #: How many times the default :meth:`replace_document` put is attempted after a store failure:
    #: the vectors are in hand, so a retry costs storage work only.
    _REPLACE_PUT_ATTEMPTS = 3

    async def replace_document(
        self,
        collection_id: str,
        document_id: str,
        records: list[EmbeddingRecord],
        *,
        stored_count: int | None = None,
    ) -> None:
        """Replace the chunks stored for ``document_id`` with ``records``.

        Chunk ids are ``str(idx)`` from 0, so a new set at least as long as the stored one
        overwrites every stored chunk id: the upsert alone replaces the document and no
        destructive step runs, so a failed put leaves the old chunks searchable. When the
        document shrank, the higher-indexed chunks must go and the store only deletes whole
        documents, so the delete runs first and the put (its vectors are already in hand) is
        retried while the store blips.

        The default is NOT atomic: the delete and the put are separate writes. Backends with
        transactional writes override it (pgvector: delete and upsert in one transaction, so a
        failed replace rolls back to the old chunks); backends without one (LanceDB) keep this
        ordering, which is the narrowest window the backend allows.

        ``stored_count`` is the number of stored chunks per a read the CALLER already made
        (the indexing paths read the stored texts once per collection / document and pass
        the count through, so a backfill of N documents does not re-read the stored chunks N
        times); when given, the default uses it instead of re-reading, and the caller owns
        its freshness. When omitted the store reads them itself. Overrides that decide from
        the store itself (pgvector's unconditional one-transaction delete + upsert, Lance's
        own row count) accept it and ignore it.
        """
        records = list(records or [])
        stored = stored_count if stored_count is not None else len(await self.list_chunk_texts(collection_id, document_id))
        if stored and len(records) < stored:
            try:
                await self.delete(collection_id, document_id)
            except PrimerError:
                # Tolerate stores that raise on delete when the collection holds no rows (some
                # backends signal an unregistered collection this way). The caller's
                # create_collection already registered it.
                pass
        if not records:
            return
        last: Exception | None = None
        for _ in range(self._REPLACE_PUT_ATTEMPTS):
            try:
                await self.put_many(records)
                return
            except Exception as exc:  # noqa: BLE001 - a blip must not strand the document unindexed; the vectors are in hand
                last = exc
        raise last

    async def list_chunk_texts(
        self, collection_id: str, document_id: str | None = None,
    ) -> list[tuple[str, str, str, str | None]]:
        """``(document_id, chunk_id, text, embedder)`` of every stored chunk (of one document when given), WITHOUT the vectors. ``embedder`` is the
        fingerprint the chunk was written under (``meta["embedder"]``; ``None`` for a chunk written before there was one). For comparing what is
        stored with what a document would produce. The default reads whole records; backends override it with a projection."""
        if document_id is None:
            records = await self.search_by_meta(collection_id, {})
        else:
            records = await self.get(collection_id, document_id)
        return [(r.document_id, r.chunk_id, r.text, (r.meta or {}).get("embedder")) for r in records]

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
    async def count_documents(self, collection_id: str) -> int:
        """Return how many distinct documents ``collection_id`` has indexed.

        Cheap count: backends MUST answer without materializing records
        (no ``text`` / ``vector`` / ``meta`` fetch and decode). The count
        is over distinct ``document_id`` values -- a document with N
        chunks counts once. This is the primitive ``search_status`` uses
        for its ``N/M docs`` progress: counting via
        :meth:`search_by_meta` would pull the whole namespace across the
        wire on every call.

        Parameters
        ----------
        collection_id
            Scopes the count to a single collection.

        Returns
        -------
        int
            The number of distinct documents indexed. Zero when the
            collection holds no rows.

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
