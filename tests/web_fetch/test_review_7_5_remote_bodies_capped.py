"""Review 7.5: the remote web-fetch and web-search adapters read a reply to a byte cap instead of buffering it whole."""
from __future__ import annotations

import httpx
import pytest
from pydantic import SecretStr

from primer.model.web_fetch import ExaFetchConfig, FirecrawlFetchConfig, JinaFetchConfig
from primer.model.web_search import ExaConfig, FirecrawlConfig, TavilyConfig
from primer.web_fetch.adapter import MAX_RESPONSE_BYTES, WebFetchUnavailable
from primer.web_fetch.exa import ExaAdapter
from primer.web_fetch.firecrawl import FirecrawlAdapter
from primer.web_fetch.jina import JinaAdapter
from primer.web_search.adapter import WebSearchUnavailable
from primer.web_search.exa import ExaAdapter as ExaSearch
from primer.web_search.firecrawl import FirecrawlAdapter as FirecrawlSearch
from primer.web_search.tavily import TavilyAdapter

CHUNK = 64 * 1024
# A body that would be 1 GiB if read to its end; ``sent`` shows how much of it was actually pulled off the wire.
LIMITLESS = 1024 * 1024 * 1024


class _Endless(httpx.AsyncByteStream):
    def __init__(self, prefix: bytes = b"") -> None:
        self.sent = 0
        self._prefix = prefix

    async def __aiter__(self):
        if self._prefix:
            self.sent += len(self._prefix)
            yield self._prefix
        while self.sent < LIMITLESS:
            self.sent += CHUNK
            yield b"a" * CHUNK


def _client(stream: _Endless, status: int = 200) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(lambda req: httpx.Response(status, stream=stream)))


def _assert_stopped_near_cap(stream: _Endless) -> None:
    assert stream.sent <= MAX_RESPONSE_BYTES + 2 * 1024 * 1024, f"read {stream.sent} bytes of a {LIMITLESS} byte reply"


@pytest.mark.asyncio
async def test_jina_cuts_and_marks_an_oversized_reply():
    stream = _Endless()
    page = await JinaAdapter(JinaFetchConfig(), client=_client(stream)).fetch(url="https://example.com/a")
    _assert_stopped_near_cap(stream)
    assert page.content_markdown.startswith("aaaa")
    assert page.content_markdown.endswith("bytes]")
    assert "response truncated" in page.content_markdown


@pytest.mark.asyncio
async def test_jina_small_reply_is_untouched():
    client = httpx.AsyncClient(transport=httpx.MockTransport(lambda req: httpx.Response(200, text="# T\n\nbody")))
    page = await JinaAdapter(JinaFetchConfig(), client=client).fetch(url="https://example.com/a")
    assert page.content_markdown == "# T\n\nbody"


@pytest.mark.asyncio
@pytest.mark.parametrize("make", [
    lambda c: FirecrawlAdapter(FirecrawlFetchConfig(api_key=SecretStr("fc-x")), client=c),
    lambda c: ExaAdapter(ExaFetchConfig(api_key=SecretStr("exa-x")), client=c),
])
async def test_remote_json_fetchers_refuse_an_oversized_reply_without_buffering_it(make):
    stream = _Endless(prefix=b'{"data": {"markdown": "')
    with pytest.raises(WebFetchUnavailable, match="larger than"):
        await make(_client(stream)).fetch(url="https://example.com")
    _assert_stopped_near_cap(stream)


@pytest.mark.asyncio
@pytest.mark.parametrize("make", [
    lambda c: TavilyAdapter(TavilyConfig(api_key=SecretStr("tv-x")), client=c),
    lambda c: ExaSearch(ExaConfig(api_key=SecretStr("exa-x")), client=c),
    lambda c: FirecrawlSearch(FirecrawlConfig(api_key=SecretStr("fc-x")), client=c),
])
async def test_remote_searchers_refuse_an_oversized_reply_without_buffering_it(make):
    stream = _Endless(prefix=b'{"results": [{"url": "u", "content": "')
    with pytest.raises(WebSearchUnavailable, match="larger than"):
        await make(_client(stream)).search(query="q", count=3, safe_search="moderate")
    _assert_stopped_near_cap(stream)


@pytest.mark.asyncio
async def test_a_large_but_bounded_reply_still_parses():
    text = "x" * (3 * 1024 * 1024)
    client = httpx.AsyncClient(transport=httpx.MockTransport(
        lambda req: httpx.Response(200, json={"results": [{"url": "https://x", "title": "T", "text": text}]})
    ))
    page = await ExaAdapter(ExaFetchConfig(api_key=SecretStr("exa-x")), client=client).fetch(url="https://example.com")
    assert page.content_markdown == text


@pytest.mark.asyncio
async def test_an_unsupported_content_encoding_is_a_transport_failure_not_a_crash():
    client = httpx.AsyncClient(transport=httpx.MockTransport(
        lambda req: httpx.Response(200, headers={"content-encoding": "br"}, stream=_Endless(prefix=b"zzzz"))
    ))
    with pytest.raises(WebFetchUnavailable):
        await JinaAdapter(JinaFetchConfig(), client=client).fetch(url="https://example.com/a")
