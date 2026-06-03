"""Unit tests for primer.toolset.web.tools.

Covers:

* Argument validation (Pydantic -> BadRequestError on bad input).
* ``web-search`` handler dispatch + JSON shape.
* ``web-search`` handler distinguishes WebSearchProviderError vs
  WebSearchUnavailable in its failure envelope.
* ``http-request`` handler success / truncation / transport-error
  paths against an :class:`httpx.MockTransport`.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from primer.model.except_ import BadRequestError
from primer.toolset.web.tools import (
    HttpRequestArgs,
    WebSearchArgs,
    make_http_request_descriptor,
    make_http_request_handler,
    make_web_search_descriptor,
    make_web_search_handler,
)
from primer.web_search.adapter import (
    SafeSearchLevel,
    SearchHit,
    WebSearchProviderError,
    WebSearchUnavailable,
)


# ===========================================================================
# Fakes
# ===========================================================================


class _FakeService:
    """Programmable stand-in for WebSearchService.

    ``plan`` is either None (return canned hits) or a BaseException
    instance — in which case search() raises it. Either way, calls
    are recorded.
    """

    def __init__(
        self,
        *,
        hits: list[SearchHit] | None = None,
        plan: BaseException | None = None,
    ) -> None:
        self._hits = list(hits or [])
        self._plan = plan
        self.calls: list[dict[str, Any]] = []

    async def search(
        self,
        *,
        query: str,
        count: int,
        safe_search: SafeSearchLevel,
    ) -> list[SearchHit]:
        self.calls.append(
            {"query": query, "count": count, "safe_search": safe_search}
        )
        if self._plan is not None:
            raise self._plan
        return list(self._hits[:count])


# ===========================================================================
# Argument-model validation
# ===========================================================================


class TestArgValidation:
    def test_web_search_defaults(self) -> None:
        a = WebSearchArgs(query="paris")
        assert a.count == 5
        assert a.safe_search == "moderate"

    def test_web_search_query_required(self) -> None:
        from pydantic import ValidationError

        with pytest.raises(ValidationError):
            WebSearchArgs(query="")

    def test_web_search_count_bounds(self) -> None:
        from pydantic import ValidationError

        with pytest.raises(ValidationError):
            WebSearchArgs(query="x", count=0)
        with pytest.raises(ValidationError):
            WebSearchArgs(query="x", count=26)

    def test_http_request_defaults(self) -> None:
        a = HttpRequestArgs(url="https://example.com/")
        assert a.method == "GET"
        assert a.headers is None
        assert a.body is None
        assert a.timeout_seconds == 30.0

    def test_http_request_url_required(self) -> None:
        from pydantic import ValidationError

        with pytest.raises(ValidationError):
            HttpRequestArgs.model_validate({"url": "not-a-url"})


# ===========================================================================
# Tool descriptors
# ===========================================================================


class TestDescriptors:
    def test_web_search_descriptor_has_correct_id_and_toolset(self) -> None:
        t = make_web_search_descriptor("web")
        assert t.id == "web-search"
        assert t.toolset_id == "web"
        # JSON schema describes the args model.
        props = t.args_schema.get("properties", {})
        assert "query" in props
        assert "count" in props
        assert "safe_search" in props

    def test_http_request_descriptor_has_correct_id_and_toolset(self) -> None:
        t = make_http_request_descriptor("web")
        assert t.id == "http-request"
        assert t.toolset_id == "web"
        props = t.args_schema.get("properties", {})
        assert {"url", "method", "headers", "body", "timeout_seconds"}.issubset(
            props.keys()
        )


# ===========================================================================
# web-search handler
# ===========================================================================


class TestWebSearchHandler:
    @pytest.mark.asyncio
    async def test_dispatches_to_service_and_returns_json(self) -> None:
        service = _FakeService(
            hits=[
                SearchHit(title="Paris", url="https://e/p", snippet="city"),
                SearchHit(title="Berlin", url="https://e/b", snippet="city"),
            ]
        )
        handler = make_web_search_handler(service)
        result = await handler({"query": "capital", "count": 2})
        assert not result.is_error
        payload = json.loads(result.output)
        assert payload == [
            {"title": "Paris", "url": "https://e/p", "snippet": "city"},
            {"title": "Berlin", "url": "https://e/b", "snippet": "city"},
        ]
        assert service.calls == [
            {"query": "capital", "count": 2, "safe_search": "moderate"}
        ]

    @pytest.mark.asyncio
    async def test_invalid_arguments_raises_bad_request(self) -> None:
        service = _FakeService()
        handler = make_web_search_handler(service)
        with pytest.raises(BadRequestError, match="invalid arguments"):
            await handler({"query": ""})
        # Service is never called when args fail validation.
        assert service.calls == []

    @pytest.mark.asyncio
    async def test_unavailable_maps_to_failed_envelope(self) -> None:
        service = _FakeService(plan=WebSearchUnavailable("upstream is down"))
        handler = make_web_search_handler(service)
        result = await handler({"query": "x", "count": 5})
        assert result.is_error is True
        # WebSearchUnavailable -> "web-search failed: <msg>"
        assert result.output == "web-search failed: upstream is down"

    @pytest.mark.asyncio
    async def test_provider_error_maps_to_not_available_envelope(self) -> None:
        service = _FakeService(plan=WebSearchProviderError("auth missing"))
        handler = make_web_search_handler(service)
        result = await handler({"query": "x", "count": 5})
        assert result.is_error is True
        # WebSearchProviderError -> "web-search not available: <msg>"
        assert result.output == "web-search not available: auth missing"


# ===========================================================================
# http-request handler
# ===========================================================================


def _client_with_responses(
    routes: list[tuple[str, int, bytes, dict[str, str] | None]],
) -> httpx.AsyncClient:
    """Build an httpx.AsyncClient backed by a MockTransport.

    Routes are matched against ``request.url`` by substring; first match
    wins. Falls back to 418 (I'm a teapot) so unmatched calls fail loud.
    """

    def _handler(request: httpx.Request) -> httpx.Response:
        for url_substring, status, body, headers in routes:
            if url_substring in str(request.url):
                return httpx.Response(
                    status_code=status,
                    content=body,
                    headers=headers or {},
                )
        return httpx.Response(status_code=418)

    return httpx.AsyncClient(transport=httpx.MockTransport(_handler))


class TestHttpRequestHandler:
    @pytest.mark.asyncio
    async def test_get_returns_status_headers_body(self) -> None:
        client = _client_with_responses(
            [(
                "example.com",
                200,
                b"hello world",
                {"content-type": "text/plain", "x-custom": "1"},
            )]
        )
        handler = make_http_request_handler(
            http_client=client, response_body_byte_cap=1_000_000
        )
        result = await handler({"url": "https://example.com/"})
        assert not result.is_error
        payload = json.loads(result.output)
        assert payload["status"] == 200
        assert payload["body"] == "hello world"
        assert payload["truncated"] is False
        assert payload["headers"]["x-custom"] == "1"
        await client.aclose()

    @pytest.mark.asyncio
    async def test_post_with_body_and_headers(self) -> None:
        captured: dict[str, Any] = {}

        def _handler(request: httpx.Request) -> httpx.Response:
            captured["method"] = request.method
            captured["body"] = request.content.decode("utf-8")
            captured["auth"] = request.headers.get("authorization")
            return httpx.Response(status_code=201, content=b'{"ok":true}')

        client = httpx.AsyncClient(transport=httpx.MockTransport(_handler))
        handler = make_http_request_handler(
            http_client=client, response_body_byte_cap=1_000_000
        )
        result = await handler(
            {
                "url": "https://api.example.com/items",
                "method": "POST",
                "headers": {"Authorization": "Bearer xyz"},
                "body": '{"name": "thing"}',
            }
        )
        payload = json.loads(result.output)
        assert payload["status"] == 201
        assert captured["method"] == "POST"
        assert captured["body"] == '{"name": "thing"}'
        assert captured["auth"] == "Bearer xyz"
        await client.aclose()

    @pytest.mark.asyncio
    async def test_response_body_truncated_past_cap(self) -> None:
        big = b"a" * 5000
        client = _client_with_responses([("example", 200, big, None)])
        handler = make_http_request_handler(
            http_client=client, response_body_byte_cap=100
        )
        result = await handler({"url": "https://example.com/"})
        payload = json.loads(result.output)
        assert payload["truncated"] is True
        assert len(payload["body"]) == 100
        await client.aclose()

    @pytest.mark.asyncio
    async def test_transport_error_is_tool_level_failure(self) -> None:
        def _raise(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("dns failure")

        client = httpx.AsyncClient(transport=httpx.MockTransport(_raise))
        handler = make_http_request_handler(
            http_client=client, response_body_byte_cap=1_000_000
        )
        result = await handler({"url": "https://nope.invalid/"})
        assert result.is_error is True
        assert "ConnectError" in result.output
        await client.aclose()

    @pytest.mark.asyncio
    async def test_invalid_arguments_raise_bad_request(self) -> None:
        client = _client_with_responses([])
        handler = make_http_request_handler(
            http_client=client, response_body_byte_cap=1_000_000
        )
        with pytest.raises(BadRequestError, match="invalid arguments"):
            await handler({"url": "ftp://nope/"})  # not http/https
        await client.aclose()

    def test_factory_rejects_zero_byte_cap(self) -> None:
        client = _client_with_responses([])
        with pytest.raises(ValueError, match="response_body_byte_cap"):
            make_http_request_handler(
                http_client=client, response_body_byte_cap=0
            )
