"""Shared entry point for running a collection's document search.

Both the REST route ``POST /v1/collections/{id}/search`` and the agent
tool ``system__search_collection`` need the *same* search semantics so a
collection behaves identically however it is queried. That shared
behaviour is:

* when the collection's :attr:`Collection.search` block sets no
  ``cross_encoder``, run a plain vector search; but
* when it does, run the :class:`CollectionSearcher` pipeline
  (``vector -> cross-encoder rerank``).

Before this helper existed, both call sites called
:meth:`VectorStore.search` directly and the per-collection ``search``
config was stored but never applied at query time -- reranking was
silently inert on the live read path. This module is the single place
that wires the configured :class:`CrossEncoder` in and runs the
orchestrator, so the two call sites cannot drift.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

from primer.search.searcher import CollectionSearcher


if TYPE_CHECKING:
    from primer.int.cross_encoder import CrossEncoder
    from primer.int.embedder import Embedder
    from primer.int.vector_store import VectorStore
    from primer.model.collection import Collection
    from primer.model.vector import SearchResult, Vector


class _CrossEncoderResolver(Protocol):
    """Minimal slice of :class:`ProviderRegistry` this helper needs.

    Kept as a Protocol so callers can pass the live ``ProviderRegistry``
    (REST + toolset both already hold one) without this module importing
    the API package.
    """

    async def get_cross_encoder(self, provider_id: str) -> "CrossEncoder": ...


async def run_collection_search(
    *,
    collection: "Collection",
    embedder: "Embedder",
    store: "VectorStore",
    query: str,
    top_k: int,
    cross_encoder_resolver: "_CrossEncoderResolver",
    query_vector: "Vector | None" = None,
) -> list["SearchResult"]:
    """Run ``query`` against ``collection`` honouring its ``search`` config.

    When ``collection.search.cross_encoder`` is unset this is exactly a
    ``store.search(...)`` call (using ``query_vector`` when the caller
    already embedded the query, so we don't double-embed). When it is set
    the :class:`CollectionSearcher` pipeline runs instead, resolving the
    referenced :class:`CrossEncoder` from ``cross_encoder_resolver``.

    The caller is responsible for catching the "collection not indexed
    yet" :class:`BadRequestError` (``"...is not registered..."``) the
    vector store raises before anything is indexed; this helper does not
    swallow it.
    """
    if top_k <= 0:
        from primer.model.except_ import BadRequestError

        raise BadRequestError(f"top_k must be > 0, got {top_k!r}")

    search_cfg = collection.search
    if search_cfg is None:
        from primer.model.except_ import BadRequestError

        raise BadRequestError("collection has no search config")
    cer_cfg = search_cfg.cross_encoder

    # Fast path: no reranking configured -> plain vector search, reusing
    # the caller's already-computed query vector when supplied.
    if cer_cfg is None:
        vector = query_vector
        if vector is None:
            from primer.model.chat import TextPart

            response = await embedder.embed(
                model=search_cfg.embedder.model,
                inputs=[TextPart(text=query)],
            )
            vector = list(response.embeddings[0].vector)
        return await store.search(collection.id, vector, top_k)

    # Reranking path: build the orchestrator with the configured
    # cross-encoder and let it run vector -> rerank. The searcher embeds
    # the query itself, so query/index vectors share dimensionality +
    # metric.
    cross_encoder: "CrossEncoder | None" = None
    if cer_cfg is not None:
        cross_encoder = await cross_encoder_resolver.get_cross_encoder(
            cer_cfg.provider_id
        )

    searcher = CollectionSearcher(
        collection=collection,
        embedder=embedder,
        vector_store=store,
        cross_encoder=cross_encoder,
    )
    return await searcher.search(query, top_k)


__all__ = ["run_collection_search"]
