"""Unit tests for the Exa web search adapter.

All tests run against an in-memory httpx.MockTransport — no real
network. Integration tests against the live Exa API would go in
tests/integration/, reading EXA_API_KEY from env, skipped when unset.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
from pydantic import SecretStr

from primer.model.web_search import ExaConfig
from primer.web_search.adapter import (
    SearchHit,
    WebSearchProviderError,
    WebSearchUnavailable,
)
from primer.web_search.exa import EXA_BASE_URL, ExaAdapter


# ---------- Test helpers ------------------------------------------


def _mock_client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def _ok_response(results: list[dict]) -> httpx.Response:
    return httpx.Response(200, json={"results": results})


def _adapter(client: httpx.AsyncClient, api_key: str = "exa-test") -> ExaAdapter:
    return ExaAdapter(
        ExaConfig(api_key=SecretStr(api_key)),
        client=client,
    )


# ---------- Happy path --------------------------------------------


class TestExaHappyPath:
    @pytest.mark.asyncio
    async def test_returns_hits_with_text_as_snippet(self) -> None:
        captured: dict[str, Any] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["url"] = str(request.url)
            captured["body"] = json.loads(request.content)
            captured["headers"] = dict(request.headers)
            return _ok_response([
                {"url": "https://example/", "title": "Primer", "text": "A snippet"},
                {"url": "https://other/",   "title": "Other",  "text": "Other text"},
            ])

        adapter = _adapter(_mock_client(handler))
        hits = await adapter.search(query="primer", count=5, safe_search="moderate")

        assert len(hits) == 2
        assert hits[0] == SearchHit(
            title="Primer", url="https://example/", snippet="A snippet",
        )
        assert captured["url"] == f"{EXA_BASE_URL}/search"
        assert captured["body"]["query"] == "primer"
        assert captured["body"]["numResults"] == 5
        assert captured["body"]["type"] == "auto"
        # Snippet population requires contents={text: True}.
        assert captured["body"]["contents"] == {"text": True}
        # Auth header is x-api-key (NOT bearer).
        assert captured["headers"]["x-api-key"] == "exa-test"
        assert "authorization" not in captured["headers"]

    @pytest.mark.asyncio
    async def test_missing_title_falls_back_to_url(self) -> None:
        def handler(request):
            return _ok_response([
                {"url": "https://no-title/", "text": "x"},
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
            {"url": f"https://u{i}/", "title": str(i), "text": ""}
            for i in range(10)
        ])))
        hits = await adapter.search(query="q", count=3, safe_search="moderate")
        assert len(hits) == 3

    @pytest.mark.asyncio
    async def test_missing_text_yields_empty_snippet(self) -> None:
        adapter = _adapter(_mock_client(lambda r: _ok_response([
            {"url": "https://x/", "title": "t"},
        ])))
        hits = await adapter.search(query="q", count=1, safe_search="moderate")
        assert hits[0].snippet == ""


# ---------- Error mapping -----------------------------------------


class TestExaErrorMapping:
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


# ---------- Lifecycle ---------------------------------------------


class TestExaLifecycle:
    @pytest.mark.asyncio
    async def test_aclose_closes_owned_client(self) -> None:
        adapter = ExaAdapter(ExaConfig(api_key=SecretStr("exa")))
        assert adapter._owns_client is True
        await adapter.aclose()
        assert adapter._client.is_closed is True

    @pytest.mark.asyncio
    async def test_aclose_does_not_close_injected_client(self) -> None:
        client = _mock_client(lambda r: _ok_response([]))
        adapter = ExaAdapter(ExaConfig(api_key=SecretStr("exa")), client=client)
        await adapter.aclose()
        assert client.is_closed is False
        await client.aclose()
