"""One collection query, for every rung of the search ladder (semantic, full-text, literal, regex).

The collections ``search`` tool and agent memory's lookup share this code, so a collection answers the same way however it is asked. The ladder is the
tool's own: ``auto`` takes the best rung the collection offers right now (semantic when its index is ready and the person may use its providers, else
full-text); a named rung that is not available is a ``ConflictError``. Failures of the stores are ``PrimerError`` for the caller to map.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from primer.knowledge.fulltext import fulltext_collection
from primer.knowledge.grep import grep_collection
from primer.model.collection import Collection, Document
from primer.model.except_ import ConflictError


@dataclass(frozen=True)
class QueryHit:
    """One hit as a caller that only needs where and how well sees it; ``score`` is a semantic score, or None (full-text and grep scores are not comparable)."""

    path: str
    excerpt: str
    score: float | None = None


@dataclass
class QueryOutcome:
    """What the ladder answered, as the ``search`` tool reports it: the hits as the wire dicts of the rung used, the rung, whether the cap cut the
    list, and the note ``auto`` adds when it fell back."""

    hits: list[dict[str, Any]]
    mode_used: str
    truncated: bool = False
    note: str | None = None
    best: str = field(default="fulltext", repr=False)


async def search_capability(
    *, sp: Any, provider_registry: Any, semantic_search_registry: Any, collection: Collection, person: Any = None,
) -> tuple[str, bool]:
    """The best rung this collection offers right now, and whether it is the person's providers that hold semantic back."""
    if (collection.search is not None and collection.search.state == "ready"
            and provider_registry is not None and semantic_search_registry is not None):
        from primer.knowledge.search_use import unusable_search_provider

        if person is not None and await unusable_search_provider(sp, person, collection.search) is not None:
            return "fulltext", True
        return "semantic", False
    return "fulltext", False


async def _semantic(
    sp: Any, provider_registry: Any, semantic_search_registry: Any, coll: Collection, query: str, top_k: int, path_prefix: str | None,
) -> list[dict[str, Any]]:
    """Embed the query and ask the vector store; the hits in the tool's wire shape."""
    from primer.model.chat import TextPart
    from primer.model.embedding import ExtendedEmbeddingConfig
    from primer.search.run import run_collection_search

    norm_prefix = (path_prefix or "").strip().lstrip("/")
    fetch_k = max(top_k * 5, 100) if norm_prefix else top_k
    embedder = await provider_registry.get_embedder(coll.search.embedder.provider_id)
    response = await embedder.embed(
        model=coll.search.embedder.model, inputs=[TextPart(text=query)], config=ExtendedEmbeddingConfig(task_type="retrieval_query"),
    )
    vector = list(response.embeddings[0].vector)
    store = await semantic_search_registry.get_store(coll.search.vector_store_provider_id)
    hits = await run_collection_search(
        collection=coll, embedder=embedder, store=store, query=query, top_k=fetch_k,
        cross_encoder_resolver=provider_registry, query_vector=vector,
    )
    docs = sp.get_storage(Document)
    out: list[dict[str, Any]] = []
    for hit in hits:
        path = hit.record.meta.get("path")
        if not path:
            doc = await docs.get(hit.record.document_id)
            path = doc.path if doc is not None else hit.record.document_id
        if norm_prefix and not path.startswith(norm_prefix):
            continue
        out.append({"path": path, "excerpt": hit.record.text, "score": hit.score})
        if len(out) >= top_k:
            break
    return out


async def query_collection(
    *, sp: Any, provider_registry: Any, semantic_search_registry: Any, collection: Collection, query: str, limit: int,
    path_prefix: str | None = None, mode: str = "auto", person: Any = None,
) -> QueryOutcome:
    """The ladder with everything the ``search`` tool reports. Raises ``ConflictError`` for a named rung the collection does not offer."""
    best, not_yours = await search_capability(
        sp=sp, provider_registry=provider_registry, semantic_search_registry=semantic_search_registry, collection=collection, person=person,
    )
    note = None
    if mode == "auto":
        mode = best
        if not_yours:
            note = "semantic search is not available to you on this collection; fell back to fulltext"
        elif best != "semantic" and collection.search is not None:
            note = f"semantic index is {collection.search.state}; fell back to fulltext"
    elif mode == "semantic" and best != "semantic":
        if not_yours:
            raise ConflictError(
                f"semantic search is not available to you on {collection.id!r}; available: fulltext, literal, regex"
            )
        raise ConflictError(f"semantic search is not enabled on {collection.id!r}; available: fulltext, literal, regex")

    if mode == "semantic":
        hits = await _semantic(sp, provider_registry, semantic_search_registry, collection, query, limit, path_prefix)
        return QueryOutcome(hits, "semantic", False, note, best)
    if mode == "fulltext":
        result = await fulltext_collection(
            sp.get_content_store(), collection_id=collection.id, query=query, path_prefix=path_prefix, max_results=limit,
        )
    else:  # literal | regex
        result = await grep_collection(
            sp.get_content_store(), collection_id=collection.id, pattern=query, path_prefix=path_prefix, max_results=limit,
            literal=mode == "literal",
        )
    return QueryOutcome([h.model_dump() for h in result.hits], mode, result.truncated, note, best)


async def run_collection_query(
    *, sp: Any, provider_registry: Any, semantic_search_registry: Any, collection: Collection, query: str, limit: int,
    path_prefix: str | None = None, mode: str = "auto", person: Any = None,
) -> tuple[list[QueryHit], str]:
    """``(hits, mode used)``: the hits reduced to where, an excerpt and a score (None unless the rung is semantic)."""
    outcome = await query_collection(
        sp=sp, provider_registry=provider_registry, semantic_search_registry=semantic_search_registry, collection=collection,
        query=query, limit=limit, path_prefix=path_prefix, mode=mode, person=person,
    )
    # Only a semantic score can be compared across queries and against a threshold; a full-text score (bm25) cannot, so it is not offered.
    semantic = outcome.mode_used == "semantic"
    return [QueryHit(h["path"], h["excerpt"], h["score"] if semantic else None) for h in outcome.hits], outcome.mode_used


__all__ = ["QueryHit", "QueryOutcome", "query_collection", "run_collection_query", "search_capability"]
