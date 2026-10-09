"""A provider's non-2xx body is scrubbed BEFORE it is cut (ticket 01a12010, #686 review).

The adapters' "unexpected status" branch (a non-200 that is not 401/403/429/5xx) put ``r.text[:200]`` in the message. A vendor's 4xx body that quotes the
key across offset 200 left the key's prefix in the text, which the tools return to the agent and the services log. The body is now scrubbed whole
(``primer.llm._failure.scrubbed_event_text`` with the adapter's own config) and only then cut to the same 200 characters, so a secret is never cut in half
and left partly readable, and the tool still never returns unbounded text.

HERMETIC: a loopback server answers 400 with a body whose key starts at offset 190 and runs past 200.
"""

from __future__ import annotations

import logging

import pytest

from primer.web_fetch.adapter import WebFetchProviderError
from primer.web_search.adapter import WebSearchProviderError
from tests.toolset.test_web_tools_do_not_echo_the_provider_key import (
    _assert_clean_logs,
    _fetch,
    _fetch_exa,
    _fetch_firecrawl,
    _fetch_handler,
    _fragment_in,
    _search,
    _search_exa,
    _search_firecrawl,
    _search_handler,
    _search_tavily,
)
from tests.web_loopback import status_server

KEY = "sk-live-Q7r8S9t0U1v2W3x4"
BODY = "x" * 190 + KEY + " was rejected"           # the key starts at offset 190 and runs past the 200 the message is cut to

SEARCH = {"exa": _search_exa, "firecrawl": _search_firecrawl, "tavily": _search_tavily}
FETCH = {"exa": _fetch_exa, "firecrawl": _fetch_firecrawl}


@pytest.mark.parametrize("name", sorted(SEARCH))
async def test_a_search_adapter_scrubs_the_body_before_cutting_it(name: str) -> None:
    async with status_server(400, BODY) as server:
        adapter = SEARCH[name](KEY, base_url=server.url)
        try:
            with pytest.raises(WebSearchProviderError) as caught:
                await _search(adapter)
        finally:
            await adapter.aclose()
    text = str(caught.value)
    assert text.startswith(f"{name} unexpected status 400: xxxx"), text          # the reason, and the start of the body, are kept
    assert _fragment_in(text, KEY) is None, text


@pytest.mark.parametrize("name", sorted(FETCH))
async def test_a_fetch_adapter_scrubs_the_body_before_cutting_it(name: str) -> None:
    async with status_server(400, BODY) as server:
        adapter = FETCH[name](KEY, base_url=server.url)
        try:
            with pytest.raises(WebFetchProviderError) as caught:
                await _fetch(adapter)
        finally:
            await adapter.aclose()
    text = str(caught.value)
    assert text.startswith(f"{name} unexpected status 400: xxxx"), text
    assert _fragment_in(text, KEY) is None, text


@pytest.mark.parametrize("name", sorted(SEARCH))
async def test_the_body_shown_is_still_cut(name: str) -> None:
    """Scrub first does not mean show it all: a megabyte body gives the same bounded message as before."""
    async with status_server(400, "y" * 1_000_000) as server:
        adapter = SEARCH[name](KEY, base_url=server.url)
        try:
            with pytest.raises(WebSearchProviderError) as caught:
                await _search(adapter)
        finally:
            await adapter.aclose()
    text = str(caught.value)
    prefix = f"{name} unexpected status 400: "
    assert text.startswith(prefix + "yyyy") and len(text) == len(prefix) + 200, len(text)


@pytest.mark.parametrize("name", sorted(SEARCH))
async def test_the_web_search_tool_and_its_logs_carry_no_part_of_the_key(name: str, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG, logger="primer")
    async with status_server(400, BODY) as server:
        adapter = SEARCH[name](KEY, base_url=server.url)
        try:
            result = await _search_handler(adapter, "single")({"query": "q", "count": 1})
        finally:
            await adapter.aclose()
    assert result.is_error is True and result.output.startswith(f"web-search not available: {name} unexpected status 400: xxxx"), result.output
    assert _fragment_in(result.output, KEY) is None, result.output
    _assert_clean_logs(caplog, "unexpected status 400", KEY)


@pytest.mark.parametrize("name", sorted(FETCH))
async def test_the_web_fetch_tool_and_its_logs_carry_no_part_of_the_key(name: str, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG, logger="primer")
    async with status_server(400, BODY) as server:
        adapter = FETCH[name](KEY, base_url=server.url)
        try:
            result = await _fetch_handler(adapter, "single")({"url": "https://example.com/page"})
        finally:
            await adapter.aclose()
    assert result.is_error is True and result.output.startswith(f"web-fetch not available: {name} unexpected status 400: xxxx"), result.output
    assert _fragment_in(result.output, KEY) is None, result.output
    _assert_clean_logs(caplog, "unexpected status 400", KEY)
