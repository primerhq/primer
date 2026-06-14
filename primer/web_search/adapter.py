"""WebSearchAdapter ABC + result type + named exceptions.

The ABC defines the provider-agnostic interface every concrete
web-search backend implements. The two named exception classes are
the only signals the registry + service treat specially: anything
else propagates unchanged so programmer bugs don't get silently
swallowed.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Literal

from pydantic import BaseModel, Field

from primer.model.except_ import PrimerError


SafeSearchLevel = Literal["off", "moderate", "strict"]


class SearchHit(BaseModel):
    """One result returned from a web search.

    Wire-shape locked: this is what the `web__web_search` tool
    serialises into its result envelope, and must round-trip with
    the existing tool's output schema. No new fields without bumping
    the tool's wire contract.
    """

    title: str = Field(
        ...,
        description=(
            "Result title as the engine returned it. May be empty "
            "for some engines."
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


class WebSearchUnavailable(PrimerError):
    """Provider is reachable but cannot serve right now.

    Maps to HTTP 429, 5xx, quota exhausted, transient network errors.
    The aggregator treats this as "skip, try next" without surfacing
    to the operator (logged at INFO).
    """


class WebSearchProviderError(PrimerError):
    """Operator-visible misconfiguration.

    Maps to HTTP 401/403, malformed responses, unexpected statuses.
    Still triggers fallback in aggregated mode, but logged at WARN
    so the operator can see something needs fixing.
    """


class WebSearchAdapter(ABC):
    """Provider-agnostic web search interface."""

    @abstractmethod
    async def search(
        self,
        *,
        query: str,
        count: int,
        safe_search: SafeSearchLevel,
    ) -> list[SearchHit]:
        """Run a web search.

        Concretes raise :class:`WebSearchUnavailable` for transient /
        quota errors, :class:`WebSearchProviderError` for
        configuration errors. Any other exception class is a bug —
        propagated to the caller.
        """

    async def aclose(self) -> None:
        """Release backend resources. Default no-op."""
        return


__all__ = [
    "SafeSearchLevel",
    "SearchHit",
    "WebSearchAdapter",
    "WebSearchProviderError",
    "WebSearchUnavailable",
]
