"""WebFetchAdapter ABC + FetchedPage result + named exceptions + constants.

Mirrors primer/web_search/adapter.py. Two named exception classes are the only
signals the registry + service treat specially; anything else propagates.
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from pydantic import BaseModel, Field

from primer.model.except_ import PrimerError


# Default output ceiling applied when the caller gives no max_chars/max_lines.
# This is the ONLY bound on web-fetch output and it applies on every surface
# (workspace agents, chats, MCP). Note: unlike workspace filesystem tools,
# internal-toolset tool output is NOT spilled to a file by the large-result
# handler (that envelope runs only on the WorkspaceTool dispatch path), so the
# ceiling itself is what prevents a pathological page from flooding context.
# A caller wanting more passes an explicit large max_chars; wanting less, a
# smaller max_chars / max_lines.
DEFAULT_MAX_CHARS = 100 * 1024

# Below this many chars of extracted HTML content, the local adapter marks the
# page is_thin (likely JS-rendered) so aggregated mode escalates to a JS-capable
# provider.
THIN_CONTENT_THRESHOLD = 200


class FetchedPage(BaseModel):
    """One fetched + cleaned page."""

    final_url: str = Field(..., description="URL after redirects.")
    title: str = Field(default="", description="Best-effort page title; may be empty.")
    content_markdown: str = Field(..., description="Clean main content as markdown.")
    content_type: str = Field(..., description="Resolved content type, e.g. text/html.")
    status: int = Field(..., description="HTTP status of the fetch.")
    is_thin: bool = Field(
        default=False,
        description="True when extraction yielded suspiciously little content.",
    )
    truncated_by_limit: bool = Field(
        default=False,
        description="True when the service truncated content to a char/line limit.",
    )


class WebFetchUnavailable(PrimerError):
    """Transient: 429, 5xx, network errors. Aggregated mode skips to next."""


class WebFetchProviderError(PrimerError):
    """Operator-visible: 401/403, malformed response, unsupported content."""


class WebFetchAdapter(ABC):
    """Provider-agnostic fetch interface."""

    @abstractmethod
    async def fetch(self, *, url: str) -> FetchedPage:
        """Fetch + clean one URL. Raises WebFetchUnavailable /
        WebFetchProviderError; any other exception is a bug and propagates."""

    async def aclose(self) -> None:
        """Release backend resources. Default no-op."""
        return


__all__ = [
    "DEFAULT_MAX_CHARS",
    "THIN_CONTENT_THRESHOLD",
    "FetchedPage",
    "WebFetchAdapter",
    "WebFetchProviderError",
    "WebFetchUnavailable",
]
