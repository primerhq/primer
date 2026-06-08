"""Argument models and handler factories for the ``web`` toolset.

Two tools live here:

* ``web-search`` — delegates to a :class:`WebSearchService` and returns
  a JSON-serialised ``[{title, url, snippet}, …]`` array.
* ``http-request`` — wraps :class:`httpx.AsyncClient` and returns a
  JSON-serialised ``{status, headers, body, truncated}`` object,
  capping the body at a configurable byte limit.

Both handlers translate argument-validation failures into
:class:`BadRequestError` (so the registry surfaces them) and
upstream-runtime failures into a :class:`ToolCallResult` with
``is_error=True`` so the LLM can react on the next turn rather than
the executor crashing.
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, Any, Literal

import httpx
from pydantic import BaseModel, Field, HttpUrl, ValidationError

from primer.model.chat import Tool, ToolCallResult, ToolExample
from primer.model.except_ import BadRequestError
from primer.toolset._describe import make_tool
from primer.web_search.adapter import (
    SearchHit,
    WebSearchProviderError,
    WebSearchUnavailable,
)


if TYPE_CHECKING:
    from primer.web_search.service import WebSearchService


logger = logging.getLogger(__name__)


HttpMethod = Literal[
    "GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"
]


# ---- Argument models -------------------------------------------------------


class WebSearchArgs(BaseModel):
    """Arguments for the ``web-search`` tool."""

    query: str = Field(
        ...,
        min_length=1,
        description="Free-text search query.",
    )
    count: int = Field(
        default=5,
        ge=1,
        le=25,
        description=(
            "Maximum number of results to return. Backends may return "
            "fewer."
        ),
    )
    safe_search: Literal["off", "moderate", "strict"] = Field(
        default="moderate",
        description=(
            "SafeSearch level passed to the backend. ``moderate`` is "
            "the default; ``strict`` filters explicit content where "
            "the engine supports it; ``off`` disables filtering."
        ),
    )


class HttpRequestArgs(BaseModel):
    """Arguments for the ``http-request`` tool."""

    url: HttpUrl = Field(
        ...,
        description="Absolute URL to fetch (http or https).",
    )
    method: HttpMethod = Field(
        default="GET",
        description="HTTP method.",
    )
    headers: dict[str, str] | None = Field(
        default=None,
        description=(
            "Optional request headers. Keys and values must both be "
            "strings."
        ),
    )
    body: str | None = Field(
        default=None,
        description=(
            "Optional request body as a string. Callers are "
            "responsible for serialising structured payloads (JSON, "
            "form encoding, etc.) before invocation."
        ),
    )
    timeout_seconds: float = Field(
        default=30.0,
        gt=0,
        le=300,
        description="Per-request timeout in seconds.",
    )


# ---- Tool descriptors (the JSON schemas the LLM sees) ----------------------


def make_web_search_descriptor(toolset_id: str) -> Tool:
    return make_tool(
        id="web-search",
        toolset_id=toolset_id,
        purpose=(
            "Search the public web and return up to ``count`` "
            "title/url/snippet results."
        ),
        when=(
            "Use when you need fact lookup, current events, or to find "
            "canonical documentation pages; not for fetching a known URL "
            "(use ``http-request``)."
        ),
        args_schema=WebSearchArgs.model_json_schema(),
        examples=[
            ToolExample(args={"query": "python 3.13 release notes"}, returns="up to 5 title/url/snippet hits"),
            ToolExample(args={"query": "anthropic api pricing", "count": 10}, returns="up to 10 hits"),
        ],
    )


def make_http_request_descriptor(toolset_id: str) -> Tool:
    return make_tool(
        id="http-request",
        toolset_id=toolset_id,
        purpose=(
            "Perform an HTTP request against ``url`` and return JSON with "
            "the response status, headers, and (byte-capped) body."
        ),
        when=(
            "Use when you need to call a specific known URL or HTTP API; "
            "not for open-ended web search (use ``web-search``). The body "
            "is truncated past the configured byte cap."
        ),
        args_schema=HttpRequestArgs.model_json_schema(),
        examples=[
            ToolExample(args={"url": "https://api.github.com/repos/python/cpython"}, returns="status, headers, JSON body"),
            ToolExample(args={"url": "https://api.example.com/items", "method": "POST", "body": "{\"x\": 1}"}, returns="the POST response"),
        ],
    )


# ---- Handlers --------------------------------------------------------------


def make_web_search_handler(service: "WebSearchService") -> ToolHandler:
    """Build the async handler for the ``web-search`` tool.

    Dispatches via the WebSearchService — the service consults the
    active config singleton, resolves the active provider (or walks
    the aggregated fallback chain), and returns hits. Wire schema
    unchanged from prior versions; only the dispatch internals moved.
    """

    async def _handle(arguments: dict[str, Any]) -> ToolCallResult:
        try:
            args = WebSearchArgs.model_validate(arguments)
        except ValidationError as exc:
            raise BadRequestError(
                f"web-search: invalid arguments: {exc}"
            ) from exc

        try:
            hits: list[SearchHit] = await service.search(
                query=args.query,
                count=args.count,
                safe_search=args.safe_search,
            )
        except WebSearchProviderError as exc:
            logger.warning(
                "web-search service misconfigured",
                extra={"error": str(exc)},
            )
            return ToolCallResult(
                output=f"web-search not available: {exc}",
                is_error=True,
            )
        except WebSearchUnavailable as exc:
            logger.info(
                "web-search all providers exhausted",
                extra={"error": str(exc)},
            )
            return ToolCallResult(
                output=f"web-search failed: {exc}",
                is_error=True,
            )

        payload = [hit.model_dump() for hit in hits]
        return ToolCallResult(
            output=json.dumps(payload, ensure_ascii=False),
            is_error=False,
        )

    return _handle


def make_http_request_handler(
    *,
    http_client: httpx.AsyncClient,
    response_body_byte_cap: int,
) -> ToolHandler:
    """Build the async handler for the ``http-request`` tool.

    Closes over a shared :class:`httpx.AsyncClient` so connection
    pooling is preserved across calls, and over the byte cap so the
    truncation policy can vary per deployment.
    """
    if response_body_byte_cap <= 0:
        raise ValueError(
            f"response_body_byte_cap must be > 0, got {response_body_byte_cap!r}"
        )

    async def _handle(arguments: dict[str, Any]) -> ToolCallResult:
        try:
            args = HttpRequestArgs.model_validate(arguments)
        except ValidationError as exc:
            raise BadRequestError(
                f"http-request: invalid arguments: {exc}"
            ) from exc

        try:
            response = await http_client.request(
                method=args.method,
                url=str(args.url),
                headers=args.headers,
                content=args.body,
                timeout=args.timeout_seconds,
            )
        except httpx.RequestError as exc:
            logger.warning(
                "http-request transport failure",
                extra={
                    "url": str(args.url),
                    "method": args.method,
                    "error": str(exc),
                },
            )
            return ToolCallResult(
                output=f"http-request failed: {type(exc).__name__}: {exc}",
                is_error=True,
            )

        body_bytes = response.content or b""
        truncated = len(body_bytes) > response_body_byte_cap
        if truncated:
            body_bytes = body_bytes[:response_body_byte_cap]
        body_text = body_bytes.decode("utf-8", errors="replace")

        payload = {
            "status": response.status_code,
            "headers": dict(response.headers.items()),
            "body": body_text,
            "truncated": truncated,
        }
        return ToolCallResult(
            output=json.dumps(payload, ensure_ascii=False),
            is_error=False,
        )

    return _handle


__all__ = [
    "HttpMethod",
    "HttpRequestArgs",
    "WebSearchArgs",
    "make_http_request_descriptor",
    "make_http_request_handler",
    "make_web_search_descriptor",
    "make_web_search_handler",
]
