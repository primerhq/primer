"""Unit tests for the Tavily web search adapter.

All tests run against an in-memory httpx MockTransport — no real
network. Integration tests against the live Tavily API go in
tests/integration/test_tavily_adapter.py (skipped when TAVILY_API_KEY
env var is unset).
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
from pydantic import SecretStr

from primer.model.web_search import TavilyConfig
from primer.web_search.adapter import (
    SearchHit,
    WebSearchProviderError,
    WebSearchUnavailable,
)
from primer.web_search.tavily import TAVILY_BASE_URL, TavilyAdapter


# ---------- Test helpers ------------------------------------------


def _mock_client(handler) -> httpx.AsyncClient:
    """Build an httpx.AsyncClient whose request handler is the given
    callable returning an httpx.Response."""
    transport = httpx.MockTransport(handler)
    return httpx.AsyncClient(transport=transport)


def _ok_response(results: list[dict]) -> httpx.Response:
    return httpx.Response(200, json={"results": results})


def _adapter(client: httpx.AsyncClient, api_key: str = "tvly-test") -> TavilyAdapter:
    return TavilyAdapter(
        TavilyConfig(api_key=SecretStr(api_key)),
        client=client,
    )


# ---------- Happy path --------------------------------------------


class TestTavilyHappyPath:
    @pytest.mark.asyncio
    async def test_returns_search_hits_with_content_mapped_to_snippet(self) -> None:
        captured: dict[str, Any] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["url"] = str(request.url)
            captured["body"] = json.loads(request.content)
            return _ok_response([
                {"title": "Primer", "url": "https://example/", "content": "A snippet"},
                {"title": "Other", "url": "https://other/", "content": "Another"},
            ])

        client = _mock_client(handler)
        adapter = _adapter(client)
        hits = await adapter.search(query="primer", count=5, safe_search="moderate")

        assert len(hits) == 2
        assert hits[0] == SearchHit(
            title="Primer", url="https://example/", snippet="A snippet",
        )
        assert captured["url"] == f"{TAVILY_BASE_URL}/search"
        assert captured["body"]["query"] == "primer"
        assert captured["body"]["max_results"] == 5
        assert captured["body"]["api_key"] == "tvly-test"

    @pytest.mark.asyncio
    async def test_missing_title_falls_back_to_url(self) -> None:
        def handler(request):
            return _ok_response([
                {"url": "https://no-title/", "content": "x"},
            ])

        adapter = _adapter(_mock_client(handler))
        hits = await adapter.search(query="q", count=1, safe_search="off")
        assert hits[0].title == "https://no-title/"

    @pytest.mark.asyncio
    async def test_empty_results_returns_empty_list(self) -> None:
        adapter = _adapter(_mock_client(lambda r: _ok_response([])))
        hits = await adapter.search(query="q", count=5, safe_search="moderate")
        assert hits == []

    @pytest.mark.asyncio
    async def test_count_caps_results_returned(self) -> None:
        adapter = _adapter(_mock_client(lambda r: _ok_response([
            {"title": str(i), "url": f"https://u{i}/", "content": ""}
            for i in range(10)
        ])))
        hits = await adapter.search(query="q", count=3, safe_search="moderate")
        assert len(hits) == 3


class TestSafeSearchMapping:
    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "level,expected_bool",
        [("off", False), ("moderate", True), ("strict", True)],
    )
    async def test_safe_search_collapse(self, level, expected_bool) -> None:
        captured: dict[str, Any] = {}

        def handler(request):
            captured["body"] = json.loads(request.content)
            return _ok_response([])

        adapter = _adapter(_mock_client(handler))
        await adapter.search(query="q", count=1, safe_search=level)
        assert captured["body"]["safe_search"] is expected_bool


class TestErrorMapping:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("status", [401, 403])
    async def test_auth_errors_raise_provider_error(self, status) -> None:
        adapter = _adapter(_mock_client(
            lambda r: httpx.Response(status, json={"error": "bad key"})
        ))
        with pytest.raises(WebSearchProviderError) as exc_info:
            await adapter.search(query="q", count=1, safe_search="moderate")
        assert "auth" in str(exc_info.value).lower()

    @pytest.mark.asyncio
    async def test_429_raises_unavailable(self) -> None:
        adapter = _adapter(_mock_client(
            lambda r: httpx.Response(429, json={"error": "rate-limited"})
        ))
        with pytest.raises(WebSearchUnavailable) as exc_info:
            await adapter.search(query="q", count=1, safe_search="moderate")
        assert "rate" in str(exc_info.value).lower()

    @pytest.mark.asyncio
    @pytest.mark.parametrize("status", [500, 502, 503])
    async def test_5xx_raises_unavailable(self, status) -> None:
        adapter = _adapter(_mock_client(
            lambda r: httpx.Response(status, json={"error": "down"})
        ))
        with pytest.raises(WebSearchUnavailable):
            await adapter.search(query="q", count=1, safe_search="moderate")

    @pytest.mark.asyncio
    async def test_unexpected_status_raises_provider_error(self) -> None:
        adapter = _adapter(_mock_client(
            lambda r: httpx.Response(418, content=b"teapot")
        ))
        with pytest.raises(WebSearchProviderError):
            await adapter.search(query="q", count=1, safe_search="moderate")

    @pytest.mark.asyncio
    async def test_connection_error_raises_unavailable(self) -> None:
        def handler(request):
            raise httpx.ConnectError("DNS failed", request=request)

        adapter = _adapter(_mock_client(handler))
        with pytest.raises(WebSearchUnavailable) as exc_info:
            await adapter.search(query="q", count=1, safe_search="moderate")
        assert "transport" in str(exc_info.value).lower()

    @pytest.mark.asyncio
    async def test_non_json_body_raises_provider_error(self) -> None:
        adapter = _adapter(_mock_client(
            lambda r: httpx.Response(200, content=b"<html>not json</html>")
        ))
        with pytest.raises(WebSearchProviderError):
            await adapter.search(query="q", count=1, safe_search="moderate")


class TestLifecycle:
    @pytest.mark.asyncio
    async def test_aclose_closes_owned_client(self) -> None:
        adapter = TavilyAdapter(TavilyConfig(api_key=SecretStr("tvly")))
        assert adapter._owns_client is True
        await adapter.aclose()
        assert adapter._client.is_closed is True

    @pytest.mark.asyncio
    async def test_aclose_does_not_close_injected_client(self) -> None:
        client = _mock_client(lambda r: _ok_response([]))
        adapter = TavilyAdapter(TavilyConfig(api_key=SecretStr("tvly")), client=client)
        await adapter.aclose()
        assert client.is_closed is False
        await client.aclose()
