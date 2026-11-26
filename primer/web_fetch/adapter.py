"""WebFetchAdapter ABC + FetchedPage result + named exceptions + constants.

Mirrors primer/web_search/adapter.py. Two named exception classes are the only
signals the registry + service treat specially; anything else propagates.
"""

from __future__ import annotations

import json
from abc import ABC, abstractmethod
from typing import Any

import httpx
from pydantic import BaseModel, Field

from primer.common.bounded_read import ACCEPT_ENCODING, UnsupportedContentEncoding, read_capped
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

# Most bytes read from a remote provider's reply (review 7.5). The reply is read as a stream and stopped here, so a hostile or runaway page cannot be
# held whole in memory. Far above DEFAULT_MAX_CHARS, so an explicit large max_chars still has its content; a JSON reply cut at the cap is not parseable,
# so those adapters refuse it, and Jina's plain-markdown reply is cut and marked.
MAX_RESPONSE_BYTES = 8 * 1024 * 1024

# Below this many chars of extracted HTML content, the local adapter marks the
# page is_thin (likely JS-rendered) so aggregated mode escalates to a JS-capable
# provider.
THIN_CONTENT_THRESHOLD = 200


class CappedResponse:
    """A provider reply read only to its byte cap: the status, the (possibly cut) body, and whether more followed the cap."""

    def __init__(self, status_code: int, body: bytes | bytearray, truncated: bool) -> None:
        self.status_code = status_code
        self.body = body
        self.truncated = truncated

    @property
    def text(self) -> str:
        return self.body.decode("utf-8", errors="replace")

    def json(self) -> Any:
        """The body as JSON; ``ValueError`` when it is not (a body cut at the cap is not)."""
        return json.loads(self.body)


async def send_capped(
    client: httpx.AsyncClient, method: str, url: str, *, cap: int = MAX_RESPONSE_BYTES, **kwargs: Any,
) -> CappedResponse:
    """Send a request and read the reply as a stream, stopping at ``cap`` decoded bytes (the shared body of every remote fetch and search adapter).

    Raises ``httpx.HTTPError`` like ``client.request`` does, and for a reply it cannot bound (an unsupported ``Content-Encoding``) too, so the
    adapters' transport handling covers it. Only the status and body are returned: the connection is released before this returns.
    """
    headers = {"Accept-Encoding": ACCEPT_ENCODING, **(kwargs.pop("headers", None) or {})}
    request = client.build_request(method, url, headers=headers, **kwargs)
    response = await client.send(request, stream=True)
    try:
        body, truncated = await read_capped(response, cap)
    except UnsupportedContentEncoding as exc:
        raise httpx.DecodingError(str(exc)) from None
    finally:
        await response.aclose()
    return CappedResponse(response.status_code, body, truncated)


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
    "CappedResponse",
    "DEFAULT_MAX_CHARS",
    "MAX_RESPONSE_BYTES",
    "send_capped",
    "THIN_CONTENT_THRESHOLD",
    "FetchedPage",
    "WebFetchAdapter",
    "WebFetchProviderError",
    "WebFetchUnavailable",
]
