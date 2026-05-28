"""Backend abstraction for the ``web-search`` tool.

A :class:`WebSearchBackend` is any object exposing an async
``search(query, count, safe_search) -> list[SearchHit]`` method. Modelled
as a :class:`Protocol` rather than an ABC so the factory can accept
small ad-hoc test doubles without inheritance plumbing.

Backends ship under :mod:`primer.toolset.web.backends`. The default is
:class:`primer.toolset.web.backends.ddg.DuckDuckGoBackend` (no API key,
pure Python). Future Brave / Tavily / Serper / Exa adapters drop in as
additional implementations of the same protocol; the factory accepts
any ``WebSearchBackend``.
"""

from __future__ import annotations

from typing import Literal, Protocol, runtime_checkable

from pydantic import BaseModel, Field


SafeSearchLevel = Literal["off", "moderate", "strict"]


class SearchHit(BaseModel):
    """One result returned from a web search.

    Lowest-common-denominator across the surveyed engines: a title, the
    canonical URL, and a one-paragraph snippet. Backends MUST normalise
    their native shapes (`{title, href, body}` for DDG, `{title, url,
    description}` for Brave, etc.) into this model so callers can sort,
    deduplicate, and present uniformly.
    """

    title: str = Field(
        ...,
        description=(
            "Result title as the engine returned it. May be empty for "
            "some engines."
        ),
    )
    url: str = Field(
        ...,
        description="Canonical URL of the result page.",
    )
    snippet: str = Field(
        default="",
        description=(
            "Engine-supplied excerpt or summary. Empty when the engine "
            "returned none."
        ),
    )


@runtime_checkable
class WebSearchBackend(Protocol):
    """``(query, count, safe_search) -> list[SearchHit]`` interface."""

    async def search(
        self,
        *,
        query: str,
        count: int,
        safe_search: SafeSearchLevel,
    ) -> list[SearchHit]:
        """Perform a web search and return up to ``count`` hits.

        Implementations SHOULD:

        * return at most ``count`` results (engines may cap lower);
        * preserve engine-supplied result order (relevance-first);
        * raise :class:`primer.model.except_.ProviderError` on
          backend-side failures (rate limits, scraper breakage,
          unexpected response shape).
        """
        ...


__all__ = ["SafeSearchLevel", "SearchHit", "WebSearchBackend"]
