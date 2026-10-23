"""Always-available literal/regex search across a collection's bodies.

Linear scan by design (spec section 10): list the collection's content
entries (never loads bodies), then fetch each body by document id and
match line by line. Revisit only if profiling says so.
"""
from __future__ import annotations

import asyncio
from collections.abc import Callable

from pydantic import BaseModel

from primer.common.user_regex import (
    MatchBudget,
    UserRegexError,
    compile_user_regex,
)
from primer.int.document_content import DocumentContentStore
from primer.model.except_ import BadRequestError

_MAX_RESULTS_CAP = 500
_EXCERPT_CHARS = 200
# The total time the regex engine may spend on one grep call, over every
# document it reads (G6-4). The pattern is the caller's: a user over
# ``GET /v1/collections/{id}/grep`` or an agent through the search tool.
GREP_MATCH_SECONDS = 10.0


class GrepHit(BaseModel):
    path: str
    line: int
    excerpt: str


class GrepResult(BaseModel):
    hits: list[GrepHit]
    truncated: bool


def _matching_lines(body: str, found: Callable[[str], bool], limit: int) -> list[tuple[int, str]]:
    """Up to ``limit`` ``(line number, line)`` pairs of ``body`` for which ``found`` holds."""
    out: list[tuple[int, str]] = []
    for lineno, line in enumerate(body.splitlines(), start=1):
        if found(line):
            out.append((lineno, line))
            if len(out) >= limit:
                break
    return out


async def grep_collection(
    content_store: DocumentContentStore,
    *,
    collection_id: str,
    pattern: str,
    path_prefix: str | None = None,
    max_results: int = 50,
    literal: bool = False,
) -> GrepResult:
    """Line search over a collection's bodies: ``pattern`` is a regex, or a plain substring when ``literal``.

    A regex is bounded (G6-4): at most 1000 characters, compiled with the
    ``regex`` engine, and matched in a worker thread with the GIL released
    under one :data:`GREP_MATCH_SECONDS` budget for the call. A pattern that
    is refused or runs the budget out is a :class:`BadRequestError` that
    says why. A literal search is a substring test, with no cap.
    """
    if literal:
        def found(line: str) -> bool:
            return pattern in line
        budget = None
    else:
        try:
            rx = compile_user_regex(pattern, what="grep pattern")
        except UserRegexError as exc:
            raise BadRequestError(str(exc)) from None
        budget = MatchBudget(GREP_MATCH_SECONDS, what="grep pattern")

        def found(line: str) -> bool:
            return budget.search(rx, line) is not None

    cap = max(1, min(max_results, _MAX_RESULTS_CAP))

    entries = await content_store.list(collection_id, prefix=path_prefix or None)
    hits: list[GrepHit] = []
    for entry in sorted(entries, key=lambda e: e.path):
        body = await content_store.get(entry.document_id)
        if not body:
            continue
        try:
            # One more than the room left, so a hit past the cap is seen.
            lines = await asyncio.to_thread(_matching_lines, body, found, cap - len(hits) + 1)
        except TimeoutError as exc:
            assert budget is not None
            raise BadRequestError(
                f"grep stopped: matching the pattern used its {budget.seconds:g} s matching budget. "
                "Overlapping or nested repeats such as (a|aa)+ or (x+)+ backtrack on long lines; "
                "simplify the pattern, narrow path_prefix, or use a literal search."
            ) from exc
        for lineno, line in lines:
            if len(hits) >= cap:
                return GrepResult(hits=hits, truncated=True)
            hits.append(GrepHit(
                path=entry.path, line=lineno,
                excerpt=line.strip()[:_EXCERPT_CHARS],
            ))
    return GrepResult(hits=hits, truncated=False)


__all__ = ["GREP_MATCH_SECONDS", "GrepHit", "GrepResult", "grep_collection"]
