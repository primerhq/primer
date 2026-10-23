"""Always-available literal/regex search across a collection's bodies.

Linear scan by design (spec section 10): list the collection's content
entries (never loads bodies), then fetch each body by document id and
match line by line. Revisit only if profiling says so.
"""
from __future__ import annotations

from pydantic import BaseModel

from primer.common.user_regex import (
    MatchBudget,
    UserRegexError,
    UserRegexTimeout,
    compile_user_regex,
    line_prefilter,
    matching_lines,
    run_off_loop,
)
from primer.int.document_content import DocumentContentStore
from primer.model.except_ import BadRequestError

_MAX_RESULTS_CAP = 500
_EXCERPT_CHARS = 200
# The total time the regex engine may spend on one grep call, over every
# document it reads (G6-4). The pattern is the caller's: a user over
# ``GET /v1/collections/{id}/grep`` or an agent through the search tool.
GREP_MATCH_SECONDS = 10.0
# The longest one line search may run (a whole-document scan is bounded by the total).
GREP_PER_SEARCH_SECONDS = 2.0


class GrepHit(BaseModel):
    path: str
    line: int
    excerpt: str


class GrepResult(BaseModel):
    hits: list[GrepHit]
    truncated: bool


def _literal_lines(lines: list[str], needle: str, limit: int) -> list[int]:
    out: list[int] = []
    for index, line in enumerate(lines):
        if needle in line:
            out.append(index)
            if len(out) >= limit:
                break
    return out


def _timeout_error(budget: MatchBudget, exc: UserRegexTimeout, path: str, lines_done: int) -> BadRequestError:
    line = getattr(exc, "line", 0) + 1
    if exc.cause == "per_search" and not budget.exhausted:
        return BadRequestError(
            f"grep stopped at {path} line {line}: one search ran past the {budget.per_search:g} s per-search "
            "bound. Overlapping or nested repeats such as (a|aa)+ or (x+)+ backtrack on long lines; "
            "simplify the pattern."
        )
    return BadRequestError(
        f"grep stopped after {lines_done + line - 1} lines, at {path} line {line}: matching used its "
        f"{budget.seconds:g} s matching budget (the longest single search took {budget.longest:.2f} s). "
        "If one search was long, simplify the pattern (overlapping or nested repeats such as (a|aa)+ "
        "backtrack on long lines); otherwise narrow path_prefix, or use a literal search."
    )


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

    A regex is bounded (G6-4): at most 1000 characters, the engine-parsed
    repeat cap, compiled with the ``regex`` engine (one-shot, never cached),
    and matched on the user-regex executor with the GIL released under one
    :data:`GREP_MATCH_SECONDS` budget for the call and a
    :data:`GREP_PER_SEARCH_SECONDS` bound per line search. A pattern that is
    refused or runs out of time is a :class:`BadRequestError` naming the
    cause. A literal search is a substring test, with no cap.
    """
    rx = prefilter = budget = None
    if not literal:
        try:
            rx = compile_user_regex(pattern, what="grep pattern")
        except UserRegexError as exc:
            raise BadRequestError(str(exc)) from None
        prefilter = line_prefilter(pattern, what="grep pattern")
        budget = MatchBudget(GREP_MATCH_SECONDS, per_search=GREP_PER_SEARCH_SECONDS, what="grep pattern")

    cap = max(1, min(max_results, _MAX_RESULTS_CAP))

    entries = await content_store.list(collection_id, prefix=path_prefix or None)
    hits: list[GrepHit] = []
    lines_done = 0
    for entry in sorted(entries, key=lambda e: e.path):
        body = await content_store.get(entry.document_id)
        if not body:
            continue
        lines = body.splitlines()
        # One more than the room left, so a hit past the cap is seen.
        room = cap - len(hits) + 1
        if literal:
            found = await run_off_loop(_literal_lines, lines, pattern, room)
        else:
            try:
                found = await run_off_loop(matching_lines, rx, lines, budget, prefilter=prefilter, limit=room)
            except UserRegexTimeout as exc:
                raise _timeout_error(budget, exc, entry.path, lines_done) from exc
        lines_done += len(lines)
        for index in found:
            if len(hits) >= cap:
                return GrepResult(hits=hits, truncated=True)
            hits.append(GrepHit(
                path=entry.path, line=index + 1,
                excerpt=lines[index].strip()[:_EXCERPT_CHARS],
            ))
    return GrepResult(hits=hits, truncated=False)


__all__ = ["GREP_MATCH_SECONDS", "GREP_PER_SEARCH_SECONDS", "GrepHit", "GrepResult", "grep_collection"]
