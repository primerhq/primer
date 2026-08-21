"""Full-text rung of the collection search ladder.

Thin shim over ``DocumentContentStore.search_fulltext`` shaped like
``grep.py`` so the unified search tool treats every rung symmetrically:
a result object with ``hits`` and a ``truncated`` flag.
"""
from __future__ import annotations

from pydantic import BaseModel

_MAX_RESULTS_CAP = 500


class FulltextSearchHit(BaseModel):
    path: str
    excerpt: str
    score: float


class FulltextResult(BaseModel):
    hits: list[FulltextSearchHit]
    truncated: bool


async def fulltext_collection(
    content_store,
    *,
    collection_id: str,
    query: str,
    path_prefix: str | None = None,
    max_results: int = 50,
) -> FulltextResult:
    cap = max(1, min(max_results, _MAX_RESULTS_CAP))
    # Ask for one more than the cap: the only way to know the cap bit.
    raw = await content_store.search_fulltext(
        collection_id, query, path_prefix=path_prefix, limit=cap + 1,
    )
    truncated = len(raw) > cap
    return FulltextResult(
        hits=[
            FulltextSearchHit(path=h.path, excerpt=h.excerpt, score=h.score)
            for h in raw[:cap]
        ],
        truncated=truncated,
    )
