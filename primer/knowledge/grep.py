"""Always-available literal/regex search across a collection's bodies.

Linear scan by design (spec section 10): list the collection's content
entries (never loads bodies), then fetch each body by document id and
match line by line. Revisit only if profiling says so.
"""
from __future__ import annotations

import re

from pydantic import BaseModel

from primer.int.document_content import DocumentContentStore
from primer.model.except_ import BadRequestError

_MAX_RESULTS_CAP = 500
_EXCERPT_CHARS = 200


class GrepHit(BaseModel):
    path: str
    line: int
    excerpt: str


class GrepResult(BaseModel):
    hits: list[GrepHit]
    truncated: bool


async def grep_collection(
    content_store: DocumentContentStore,
    *,
    collection_id: str,
    pattern: str,
    path_prefix: str | None = None,
    max_results: int = 50,
) -> GrepResult:
    try:
        rx = re.compile(pattern)
    except re.error as exc:
        raise BadRequestError(f"invalid regex {pattern!r}: {exc}") from exc
    cap = max(1, min(max_results, _MAX_RESULTS_CAP))

    entries = await content_store.list(collection_id, prefix=path_prefix or None)
    hits: list[GrepHit] = []
    truncated = False
    for entry in sorted(entries, key=lambda e: e.path):
        body = await content_store.get(entry.document_id)
        if not body:
            continue
        for lineno, line in enumerate(body.splitlines(), start=1):
            if rx.search(line):
                if len(hits) >= cap:
                    truncated = True
                    return GrepResult(hits=hits, truncated=truncated)
                hits.append(GrepHit(
                    path=entry.path, line=lineno,
                    excerpt=line.strip()[:_EXCERPT_CHARS],
                ))
    return GrepResult(hits=hits, truncated=truncated)


__all__ = ["GrepHit", "GrepResult", "grep_collection"]
