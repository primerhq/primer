"""Unit tests for the Firecrawl web search adapter.

All tests run against an in-memory httpx.MockTransport — no real
network. Integration tests against the live Firecrawl API would go
in tests/integration/, reading FIRECRAWL_API_KEY from env, skipped
when unset.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
from pydantic import SecretStr

from primer.model.web_search import FirecrawlConfig
from primer.web_search.adapter import (
    SearchHit,
    WebSearchProviderError,
    WebSearchUnavailable,
)
from primer.web_search.firecrawl import FIRECRAWL_BASE_URL, FirecrawlAdapter


# ---------- Test helpers ------------------------------------------


def _mock_client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def _ok_response(data: list[dict]) -> httpx.Response:
    return httpx.Response(200, json={"success": True, "data": data})


def _adapter(client: httpx.AsyncClient, api_key: str = "fc-test") -> FirecrawlAdapter:
    return FirecrawlAdapter(
        FirecrawlConfig(api_key=SecretStr(api_key)),
        client=client,
    )


# ---------- Happy path --------------------------------------------


class TestFirecrawlHappyPath:
    @pytest.mark.asyncio
    async def test_returns_hits_with_description_as_snippet(self) -> None:
        captured: dict[str, Any] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["url"] = str(request.url)
            captured["body"] = json.loads(request.content)
            captured["headers"] = dict(request.headers)
            return _ok_response([
                {"url": "https://example/", "title": "Primer", "description": "A snippet"},
                {"url": "https://other/",   "title": "Other",  "description": "Other text"},
            ])

        adapter = _adapter(_mock_client(handler))
        hits = await adapter.search(query="primer", count=5, safe_search="moderate")

        assert len(hits) == 2
        assert hits[0] == SearchHit(
            title="Primer", url="https://example/", snippet="A snippet",
        )
        assert captured["url"] == f"{FIRECRAWL_BASE_URL}/v1/search"
        assert captured["body"]["query"] == "primer"
        assert captured["body"]["limit"] == 5
        # Bearer auth header carries the plaintext API key.
        assert captured["headers"]["authorization"] == "Bearer fc-test"

    @pytest.mark.asyncio
    async def test_missing_title_falls_back_to_url(self) -> None:
        def handler(request):
            return _ok_response([
                {"url": "https://no-title/", "description": "x"},
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
            {"url": f"https://u{i}/", "title": str(i), "description": ""}
            for i in range(10)
        ])))
        hits = await adapter.search(query="q", count=3, safe_search="moderate")
        assert len(hits) == 3

    @pytest.mark.asyncio
    async def test_missing_description_yields_empty_snippet(self) -> None:
        adapter = _adapter(_mock_client(lambda r: _ok_response([
            {"url": "https://x/", "title": "t"},
        ])))
        hits = await adapter.search(query="q", count=1, safe_search="moderate")
        assert hits[0].snippet == ""


# ---------- Error mapping -----------------------------------------


class TestFirecrawlErrorMapping:
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
    async def test_402_payment_required_raises_provider_error(self) -> None:
        adapter = _adapter(_mock_client(
            lambda r: httpx.Response(402, json={"error": "out of credits"})
        ))
        with pytest.raises(WebSearchProviderError) as exc_info:
            await adapter.search(query="q", count=1, safe_search="moderate")
        assert "payment" in str(exc_info.value).lower()

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

    @pytest.mark.asyncio
    async def test_success_false_on_200_raises_provider_error(self) -> None:
        # Firecrawl wraps success/failure into a top-level flag; a
        # 200 with success=false is still an error from the operator's
        # perspective.
        adapter = _adapter(_mock_client(
            lambda r: httpx.Response(200, json={"success": False, "error": "bad query"})
        ))
        with pytest.raises(WebSearchProviderError) as exc_info:
            await adapter.search(query="q", count=1, safe_search="moderate")
        assert "bad query" in str(exc_info.value)


# ---------- Lifecycle ---------------------------------------------


class TestFirecrawlLifecycle:
    @pytest.mark.asyncio
    async def test_aclose_closes_owned_client(self) -> None:
        adapter = FirecrawlAdapter(FirecrawlConfig(api_key=SecretStr("fc")))
        assert adapter._owns_client is True
        await adapter.aclose()
        assert adapter._client.is_closed is True

    @pytest.mark.asyncio
    async def test_aclose_does_not_close_injected_client(self) -> None:
        client = _mock_client(lambda r: _ok_response([]))
        adapter = FirecrawlAdapter(FirecrawlConfig(api_key=SecretStr("fc")), client=client)
        await adapter.aclose()
        assert client.is_closed is False
        await client.aclose()
