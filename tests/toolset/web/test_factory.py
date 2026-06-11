"""Unit tests for primer.toolset.web.build_web_toolset."""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from primer.model.except_ import UnsupportedContentError
from primer.toolset import build_web_toolset
from primer.toolset.internal import InternalToolsetProvider
from primer.web_search.adapter import SafeSearchLevel, SearchHit


class _FakeWebSearchService:
    """Minimal stand-in for WebSearchService.

    search() records calls and returns a slice of canned hits.
    """

    def __init__(self, hits: list[SearchHit] | None = None) -> None:
        self._hits = list(
            hits
            if hits is not None
            else [SearchHit(title="fake", url="https://fake/", snippet="")]
        )
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


class _FakeWebFetchService:
    """Minimal stand-in for WebFetchService; list_tools never calls fetch()."""

    async def fetch(self, *, url: str, max_chars, max_lines):  # pragma: no cover
        raise AssertionError("fetch should not be called in these tests")


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
        service = _FakeWebSearchService([])
        client = _ok_client()
        ts = build_web_toolset(
            web_search_service=service,
            web_fetch_service=_FakeWebFetchService(),
            http_client=client,
        )
        try:
            assert isinstance(ts, InternalToolsetProvider)
            tools = [t async for t in ts.list_tools()]
            ids = sorted(t.id for t in tools)
            assert ids == ["http-request", "web-fetch", "web-search"]
            for t in tools:
                assert t.toolset_id == "web"
        finally:
            await client.aclose()

    @pytest.mark.asyncio
    async def test_custom_toolset_id_propagates(self) -> None:
        client = _ok_client()
        ts = build_web_toolset(
            toolset_id="my-web",
            web_search_service=_FakeWebSearchService([]),
            web_fetch_service=_FakeWebFetchService(),
            http_client=client,
        )
        try:
            tools = [t async for t in ts.list_tools()]
            assert {t.toolset_id for t in tools} == {"my-web"}
        finally:
            await client.aclose()

    @pytest.mark.asyncio
    async def test_call_web_search_dispatches_to_service(self) -> None:
        service = _FakeWebSearchService(
            [SearchHit(title="t", url="https://e/x", snippet="s")]
        )
        client = _ok_client()
        ts = build_web_toolset(
            web_search_service=service,
            web_fetch_service=_FakeWebFetchService(),
            http_client=client,
        )
        try:
            result = await ts.call(
                tool_name="web-search",
                arguments={"query": "what", "count": 1},
            )
            assert not result.is_error
            assert service.calls == [
                {"query": "what", "count": 1, "safe_search": "moderate"}
            ]
            payload = json.loads(result.output)
            assert payload[0]["url"] == "https://e/x"
        finally:
            await client.aclose()

    @pytest.mark.asyncio
    async def test_call_http_request_dispatches_to_client(self) -> None:
        client = _ok_client()
        ts = build_web_toolset(
            web_search_service=_FakeWebSearchService([]),
            web_fetch_service=_FakeWebFetchService(),
            http_client=client,
        )
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
        ts = build_web_toolset(
            web_search_service=_FakeWebSearchService([]),
            web_fetch_service=_FakeWebFetchService(),
            http_client=client,
        )
        try:
            with pytest.raises(UnsupportedContentError):
                await ts.call(tool_name="not-a-tool", arguments={})
        finally:
            await client.aclose()

    @pytest.mark.asyncio
    async def test_service_is_required(self) -> None:
        # The factory's web_search_service kwarg is now required;
        # calling without it raises a TypeError from Python's call
        # binding. (Smoke check — the signature change is the whole
        # point of this commit.)
        with pytest.raises(TypeError):
            build_web_toolset()  # type: ignore[call-arg]
