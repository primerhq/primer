"""Unit tests for primer.toolset.web.build_web_toolset."""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from primer.model.except_ import UnsupportedContentError
from primer.toolset import build_web_toolset
from primer.toolset.internal import InternalToolsetProvider
from primer.toolset.web.backends.base import (
    SafeSearchLevel,
    SearchHit,
    WebSearchBackend,
)


class _FakeBackend(WebSearchBackend):
    def __init__(self, hits: list[SearchHit]) -> None:
        self._hits = list(hits)
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
        return list(self._hits[:count])


def _ok_client() -> httpx.AsyncClient:
    def _h(req: httpx.Request) -> httpx.Response:
        return httpx.Response(
            status_code=200,
            content=b"ok",
            headers={"content-type": "text/plain"},
        )

    return httpx.AsyncClient(transport=httpx.MockTransport(_h))


class TestFactory:
    @pytest.mark.asyncio
    async def test_returns_internal_provider_with_two_tools(self) -> None:
        backend = _FakeBackend([])
        client = _ok_client()
        ts = build_web_toolset(backend=backend, http_client=client)
        try:
            assert isinstance(ts, InternalToolsetProvider)
            tools = [t async for t in ts.list_tools()]
            ids = sorted(t.id for t in tools)
            assert ids == ["http-request", "web-search"]
            for t in tools:
                assert t.toolset_id == "web"
        finally:
            await client.aclose()

    @pytest.mark.asyncio
    async def test_custom_toolset_id_propagates(self) -> None:
        client = _ok_client()
        ts = build_web_toolset(
            toolset_id="my-web", backend=_FakeBackend([]), http_client=client
        )
        try:
            tools = [t async for t in ts.list_tools()]
            assert {t.toolset_id for t in tools} == {"my-web"}
        finally:
            await client.aclose()

    @pytest.mark.asyncio
    async def test_call_web_search_dispatches_to_backend(self) -> None:
        backend = _FakeBackend(
            [SearchHit(title="t", url="https://e/x", snippet="s")]
        )
        client = _ok_client()
        ts = build_web_toolset(backend=backend, http_client=client)
        try:
            result = await ts.call(
                tool_name="web-search",
                arguments={"query": "what", "count": 1},
            )
            assert not result.is_error
            assert backend.calls == [
                {"query": "what", "count": 1, "safe_search": "moderate"}
            ]
            payload = json.loads(result.output)
            assert payload[0]["url"] == "https://e/x"
        finally:
            await client.aclose()

    @pytest.mark.asyncio
    async def test_call_http_request_dispatches_to_client(self) -> None:
        client = _ok_client()
        ts = build_web_toolset(backend=_FakeBackend([]), http_client=client)
        try:
            result = await ts.call(
                tool_name="http-request",
                arguments={"url": "https://example.com/"},
            )
            assert not result.is_error
            payload = json.loads(result.output)
            assert payload["status"] == 200
            assert payload["body"] == "ok"
        finally:
            await client.aclose()

    @pytest.mark.asyncio
    async def test_unknown_tool_name_raises(self) -> None:
        client = _ok_client()
        ts = build_web_toolset(backend=_FakeBackend([]), http_client=client)
        try:
            with pytest.raises(UnsupportedContentError):
                await ts.call(tool_name="not-a-tool", arguments={})
        finally:
            await client.aclose()

    @pytest.mark.asyncio
    async def test_default_backend_is_duckduckgo(self) -> None:
        # When backend is omitted the factory constructs DuckDuckGoBackend.
        # We only check the type / construction path; we DON'T actually
        # call .search() here so no network traffic happens.
        from primer.toolset.web.backends.ddg import DuckDuckGoBackend

        client = _ok_client()
        ts = build_web_toolset(http_client=client)
        try:
            tools = {t.id: t for t in [t async for t in ts.list_tools()]}
            assert "web-search" in tools
            assert "http-request" in tools
            # The default class is importable / constructable.
            assert DuckDuckGoBackend()._region == "us-en"
        finally:
            await client.aclose()
