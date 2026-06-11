import httpx
import pytest
from pydantic import SecretStr

from primer.model.web_fetch import ExaFetchConfig, FirecrawlFetchConfig, JinaFetchConfig
from primer.web_fetch.adapter import WebFetchProviderError, WebFetchUnavailable
from primer.web_fetch.exa import ExaAdapter
from primer.web_fetch.firecrawl import FirecrawlAdapter
from primer.web_fetch.jina import JinaAdapter


def _client(handler):
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


@pytest.mark.asyncio
async def test_jina_returns_markdown():
    seen = {}
    def h(req):
        seen["url"] = str(req.url)
        return httpx.Response(200, text="# Title\n\nbody here")
    page = await JinaAdapter(JinaFetchConfig(), client=_client(h)).fetch(url="https://example.com/a")
    assert "body here" in page.content_markdown
    assert seen["url"].endswith("https://example.com/a")


@pytest.mark.asyncio
async def test_firecrawl_maps_markdown_and_title():
    def h(req):
        return httpx.Response(200, json={
            "success": True,
            "data": {"markdown": "clean md", "metadata": {"title": "T", "sourceURL": "https://x"}},
        })
    page = await FirecrawlAdapter(
        FirecrawlFetchConfig(api_key=SecretStr("fc-x")), client=_client(h),
    ).fetch(url="https://example.com")
    assert page.content_markdown == "clean md"
    assert page.title == "T"


@pytest.mark.asyncio
async def test_exa_maps_text():
    def h(req):
        return httpx.Response(200, json={"results": [{"url": "https://x", "title": "T", "text": "page text"}]})
    page = await ExaAdapter(
        ExaFetchConfig(api_key=SecretStr("exa-x")), client=_client(h),
    ).fetch(url="https://example.com")
    assert page.content_markdown == "page text"
    assert page.title == "T"


@pytest.mark.asyncio
async def test_firecrawl_auth_raises_provider_error():
    def h(req): return httpx.Response(401, json={"error": "bad key"})
    with pytest.raises(WebFetchProviderError):
        await FirecrawlAdapter(
            FirecrawlFetchConfig(api_key=SecretStr("fc-x")), client=_client(h),
        ).fetch(url="https://x")


@pytest.mark.asyncio
async def test_exa_rate_limit_raises_unavailable():
    def h(req): return httpx.Response(429, json={})
    with pytest.raises(WebFetchUnavailable):
        await ExaAdapter(
            ExaFetchConfig(api_key=SecretStr("exa-x")), client=_client(h),
        ).fetch(url="https://x")
