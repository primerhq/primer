"""Argument models and handler factories for the ``web`` toolset.

Three tools live here:

* ``web_search`` — delegates to a :class:`WebSearchService` and returns
  a JSON-serialised ``[{title, url, snippet}, …]`` array.
* ``web_fetch`` - delegates to a :class:`WebFetchService` and returns
  clean markdown of a page's main content with a title/source header.
* ``http_request`` — wraps :class:`httpx.AsyncClient` and returns a
  JSON-serialised ``{status, headers, body, truncated}`` object,
  capping the body at a configurable byte limit.

The handlers translate argument-validation failures into
:class:`BadRequestError` (so the registry surfaces them) and
upstream-runtime failures into a :class:`ToolCallResult` with
``is_error=True`` so the LLM can react on the next turn rather than
the executor crashing.
"""

from __future__ import annotations

import json
import logging
import os
from typing import TYPE_CHECKING, Any, Literal
from urllib.parse import urlsplit

import httpx
from pydantic import BaseModel, Field, HttpUrl, ValidationError

from primer.model.chat import Tool, ToolCallResult, ToolExample
from primer.model.except_ import BadRequestError, NotFoundError
from primer.model.yield_ import ToolContext
from primer.toolset._describe import make_tool
from primer.web_fetch.adapter import (
    FetchedPage,
    WebFetchProviderError,
    WebFetchUnavailable,
)
from primer.web_search.adapter import (
    SearchHit,
    WebSearchProviderError,
    WebSearchUnavailable,
)


if TYPE_CHECKING:
    from primer.api.registries.workspace_registry import WorkspaceRegistry
    from primer.toolset.internal import ToolHandler
    from primer.web_fetch.service import WebFetchService
    from primer.web_search.service import WebSearchService


logger = logging.getLogger(__name__)


HttpMethod = Literal[
    "GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"
]


# ---- Argument models -------------------------------------------------------


class WebSearchArgs(BaseModel):
    """Arguments for the ``web_search`` tool."""

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
    """Arguments for the ``http_request`` tool."""

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


class WebFetchArgs(BaseModel):
    """Arguments for the ``web_fetch`` tool."""

    url: HttpUrl = Field(..., description="Absolute URL of the page to read (http or https).")
    max_chars: int | None = Field(
        default=None, gt=0,
        description="Optional: cap the returned markdown to this many characters.",
    )
    max_lines: int | None = Field(
        default=None, gt=0,
        description="Optional: cap the returned markdown to this many lines.",
    )


class DownloadArgs(BaseModel):
    """Arguments for the ``download`` tool (workspace only)."""

    url: HttpUrl = Field(
        ...,
        description="Absolute URL of the file to download (http or https).",
    )
    path: str | None = Field(
        default=None,
        description=(
            "Optional workspace-relative destination. When omitted (or "
            "ending with '/'), the filename is taken from the URL's last "
            "path segment and the file is written under that directory (or "
            "the workspace root). Must not escape the workspace."
        ),
    )
    max_bytes: int | None = Field(
        default=None,
        gt=0,
        description=(
            "Optional per-call maximum download size in bytes. The download "
            "is rejected (nothing is written) the moment the stream exceeds "
            "this cap. Defaults to the toolset's configured cap when omitted."
        ),
    )


# ---- Tool descriptors (the JSON schemas the LLM sees) ----------------------


def make_web_search_descriptor(toolset_id: str) -> Tool:
    return make_tool(
        id="web_search",
        toolset_id=toolset_id,
        purpose=(
            "Search the public web and return up to ``count`` "
            "title/url/snippet results."
        ),
        when=(
            "Use when you need fact lookup, current events, or to find canonical "
            "documentation pages. To READ a result page, use ``web_fetch`` (not "
            "``http_request``)."
        ),
        args_schema=WebSearchArgs.model_json_schema(),
        examples=[
            ToolExample(args={"query": "python 3.13 release notes"}, returns="up to 5 title/url/snippet hits"),
            ToolExample(args={"query": "anthropic api pricing", "count": 10}, returns="up to 10 hits"),
        ],
        required_role="user",
    )


def make_http_request_descriptor(toolset_id: str) -> Tool:
    return make_tool(
        id="http_request",
        toolset_id=toolset_id,
        purpose=(
            "Perform an HTTP request against ``url`` and return JSON with "
            "the response status, headers, and (byte-capped) body."
        ),
        when=(
            "Use when you need JSON/API endpoints, webhooks, or to inspect raw "
            "status/headers/bytes; NOT for reading human web pages (use "
            "``web_fetch``). The body is truncated past the configured byte cap."
        ),
        args_schema=HttpRequestArgs.model_json_schema(),
        examples=[
            ToolExample(args={"url": "https://api.github.com/repos/python/cpython"}, returns="status, headers, JSON body"),
            ToolExample(args={"url": "https://api.example.com/items", "method": "POST", "body": "{\"x\": 1}"}, returns="the POST response"),
        ],
        required_role="user",
    )


def make_web_fetch_descriptor(toolset_id: str) -> Tool:
    return make_tool(
        id="web_fetch",
        toolset_id=toolset_id,
        purpose=(
            "Fetch a URL and return clean markdown of the page's main content "
            "(navigation, sidebars, and scripts removed)."
        ),
        when=(
            "Use when you need to READ a web page or document. For JSON/API "
            "endpoints or to inspect raw headers/bytes, use ``http_request`` "
            "instead. Pass ``max_chars``/``max_lines`` to bound the output."
        ),
        args_schema=WebFetchArgs.model_json_schema(),
        examples=[
            ToolExample(args={"url": "https://docs.python.org/3/whatsnew/3.13.html"}, returns="clean markdown of the page"),
            ToolExample(args={"url": "https://example.com/article", "max_chars": 4000}, returns="first ~4000 chars of clean markdown"),
        ],
        required_role="user",
    )


def make_download_descriptor(toolset_id: str) -> Tool:
    return make_tool(
        id="download",
        toolset_id=toolset_id,
        purpose="Download the file at a URL into the agent's workspace at a path.",
        when=(
            "Use when you need to save a remote file (dataset, asset, "
            "release artifact) into the workspace for later processing "
            "(workspace only). To READ a web page as text use ``web_fetch``; "
            "to inspect raw bytes/headers use ``http_request``."
        ),
        args_schema=DownloadArgs.model_json_schema(),
        examples=[
            ToolExample(
                args={"url": "https://example.com/data.csv"},
                returns="``{path: 'data.csv', bytes: 1234, url: '...'}``",
                note="writes data.csv at the workspace root",
            ),
            ToolExample(
                args={"url": "https://example.com/a.pdf", "path": "docs/a.pdf"},
                returns="``{path: 'docs/a.pdf', bytes: ..., url: '...'}``",
            ),
        ],
        yields=False,
        requires_workspace=True,
        required_role="user",
    )


# ---- Handlers --------------------------------------------------------------


def make_web_search_handler(service: "WebSearchService") -> ToolHandler:
    """Build the async handler for the ``web_search`` tool.

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
    """Build the async handler for the ``http_request`` tool.

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


def make_web_fetch_handler(service: "WebFetchService") -> ToolHandler:
    """Build the async handler for the ``web_fetch`` tool.

    Dispatches via the WebFetchService (active-config singleton -> provider /
    aggregated fallback), returns clean markdown with a small title/source
    header. Machine metadata goes in ``extended``.
    """

    async def _handle(arguments: dict[str, Any]) -> ToolCallResult:
        try:
            args = WebFetchArgs.model_validate(arguments)
        except ValidationError as exc:
            raise BadRequestError(f"web-fetch: invalid arguments: {exc}") from exc

        try:
            page: FetchedPage = await service.fetch(
                url=str(args.url),
                max_chars=args.max_chars,
                max_lines=args.max_lines,
            )
        except WebFetchProviderError as exc:
            logger.warning("web-fetch service misconfigured", extra={"error": str(exc)})
            return ToolCallResult(output=f"web-fetch not available: {exc}", is_error=True)
        except WebFetchUnavailable as exc:
            logger.info("web-fetch all providers exhausted", extra={"error": str(exc)})
            return ToolCallResult(output=f"web-fetch failed: {exc}", is_error=True)

        header = f"# {page.title}\n" if page.title else ""
        note = (
            "\n\n(extracted content was short; the page may require JavaScript "
            "rendering, configure a JS-capable web-fetch provider)"
            if page.is_thin else ""
        )
        output = f"{header}Source: {page.final_url}\n\n{page.content_markdown}{note}"
        return ToolCallResult(
            output=output,
            is_error=False,
            extended={
                "status": page.status,
                "content_type": page.content_type,
                "final_url": page.final_url,
                "char_count": len(page.content_markdown),
                "line_count": page.content_markdown.count("\n") + 1,
                "truncated_by_limit": page.truncated_by_limit,
                "is_thin": page.is_thin,
            },
        )

    return _handle


def make_download_handler(
    *,
    http_client: httpx.AsyncClient,
    workspace_registry: "WorkspaceRegistry",
    byte_cap: int,
) -> "ToolHandler":
    """Build the async handler for the ``download`` tool (workspace only).

    Streams the remote file with a HARD size cap: the byte total is
    checked as chunks arrive and the download is rejected the instant it
    exceeds the cap, so a truncated (corrupt) file is never written. The
    fully-buffered bytes are then written into the workspace via the
    workspace backend, which owns path-confinement (a traversal or
    reserved-path write raises :class:`BadRequestError`).

    Closes over a shared :class:`httpx.AsyncClient` (connection pooling),
    the :class:`WorkspaceRegistry` (to resolve the live workspace from
    ``ctx.workspace_id``), and the default ``byte_cap``.
    """
    if byte_cap <= 0:
        raise ValueError(f"byte_cap must be > 0, got {byte_cap!r}")

    async def _handle(
        arguments: dict[str, Any], *, ctx: ToolContext
    ) -> ToolCallResult:
        # Defensive guard: chat suppression should keep this tool out of
        # any non-workspace context, but never attempt file I/O without a
        # live workspace.
        if ctx is None or ctx.workspace_id is None:
            return ToolCallResult(
                output=(
                    "download requires a workspace session (no workspace "
                    "is bound to this turn)"
                ),
                is_error=True,
            )

        try:
            args = DownloadArgs.model_validate(arguments)
        except ValidationError as exc:
            raise BadRequestError(
                f"download: invalid arguments: {exc}"
            ) from exc

        url_str = str(args.url)
        # Derive the destination path. A trailing '/' (or an omitted path)
        # means "a directory" -> append the URL's filename.
        url_name = os.path.basename(urlsplit(url_str).path)
        if args.path and not args.path.endswith("/"):
            dest = args.path
        else:
            if not url_name:
                return ToolCallResult(
                    output=(
                        "download: cannot derive a filename from the URL; "
                        "pass an explicit 'path'"
                    ),
                    is_error=True,
                )
            dest = f"{args.path}{url_name}" if args.path else url_name

        cap = args.max_bytes if args.max_bytes is not None else byte_cap

        # Stream with a hard cap. A truncated file is corrupt, so reject
        # (write nothing) the moment the running total exceeds the cap -
        # do NOT read-all-then-truncate.
        chunks: list[bytes] = []
        total = 0
        try:
            async with http_client.stream("GET", url_str) as resp:
                resp.raise_for_status()
                async for chunk in resp.aiter_bytes():
                    total += len(chunk)
                    if total > cap:
                        return ToolCallResult(
                            output=(
                                f"download: file exceeds the maximum of "
                                f"{cap} bytes; nothing was written"
                            ),
                            is_error=True,
                        )
                    chunks.append(chunk)
        except httpx.HTTPStatusError as exc:
            return ToolCallResult(
                output=(
                    f"download failed: server returned "
                    f"{exc.response.status_code} for {url_str}"
                ),
                is_error=True,
            )
        except httpx.RequestError as exc:
            return ToolCallResult(
                output=f"download failed: {type(exc).__name__}: {exc}",
                is_error=True,
            )

        data = b"".join(chunks)
        try:
            ws = await workspace_registry.get_workspace(ctx.workspace_id)
            await ws.write_file(dest, data)
        except (BadRequestError, NotFoundError) as exc:
            return ToolCallResult(
                output=f"download: cannot write {dest!r}: {exc}",
                is_error=True,
            )

        return ToolCallResult(
            output=json.dumps(
                {"path": dest, "bytes": total, "url": url_str},
                ensure_ascii=False,
            ),
            is_error=False,
        )

    return _handle


__all__ = [
    "DownloadArgs",
    "HttpMethod",
    "HttpRequestArgs",
    "WebFetchArgs",
    "WebSearchArgs",
    "make_download_descriptor",
    "make_download_handler",
    "make_http_request_descriptor",
    "make_http_request_handler",
    "make_web_fetch_descriptor",
    "make_web_fetch_handler",
    "make_web_search_descriptor",
    "make_web_search_handler",
]
