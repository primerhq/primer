"""The web_search / web_fetch tools must not echo the deployment's provider key (ticket 01a12010).

A pasted key often ends in a newline. httpx hands it to h11 as a header value and h11 refuses it:
``LocalProtocolError: Illegal header value b'<the key>\\n'``. The adapters' transport wrappers put that text into
``WebSearchUnavailable`` / ``WebFetchUnavailable``, and the tools return it to the AGENT (``web-search failed: ...``), so the key lands in the
session transcript, goes to the model vendor and is visible to every user of the session; the services and the tools log it too.

The adapters mask the key they hold in the text they raise (``primer.llm._failure.scrubbed_event_text`` with the adapter's own config, the rule the
LLM adapters use), so every caller downstream, aggregated summaries and logs included, sees the masked text.

HERMETIC: the header cases point ``base_url`` at a local server that accepts a connection and closes it. httpcore connects BEFORE h11 checks the
header, so the failure is the real one and no network (a real host) is involved. Tavily sends its key in the JSON body, so h11 never sees it; its
case, and the "any transport that echoes the key" case for all six adapters, use an in-memory transport.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any

import httpx
import pytest
from pydantic import SecretStr

import primer.web_fetch.exa as fetch_exa
import primer.web_fetch.firecrawl as fetch_firecrawl
import primer.web_fetch.jina as fetch_jina
import primer.web_search.exa as search_exa
import primer.web_search.firecrawl as search_firecrawl
import primer.web_search.tavily as search_tavily
from primer.model.web_fetch import (
    ACTIVE_WEB_FETCH_CONFIG_ID,
    ActiveWebFetchConfig,
    AggregatedFetchConfig,
    ExaFetchConfig,
    FirecrawlFetchConfig,
    JinaFetchConfig,
    SingleFetchConfig,
)
from primer.model.web_search import (
    ACTIVE_WEB_SEARCH_CONFIG_ID,
    ActiveWebSearchConfig,
    AggregatedProviderConfig,
    ExaConfig,
    FirecrawlConfig,
    SingleProviderConfig,
    TavilyConfig,
)
from primer.toolset.web.tools import make_web_fetch_handler, make_web_search_handler
from primer.web_fetch.adapter import WebFetchUnavailable
from primer.web_fetch.service import WebFetchService
from primer.web_search.adapter import WebSearchUnavailable
from primer.web_search.service import WebSearchService

KEY_CORE = "sk-live-A1b2C3d4E5f6G7h8"
PASTED_KEY = KEY_CORE + "\n"          # a pasted key with its trailing newline


# ---- the hermetic server ---------------------------------------------------------------------------------------------------------------------


@pytest.fixture
async def closing_server_url():
    """``http://127.0.0.1:<port>`` of a server that accepts a connection and closes it."""

    async def accept_and_close(_reader, writer) -> None:
        writer.close()

    server = await asyncio.start_server(accept_and_close, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    yield f"http://127.0.0.1:{port}"
    server.close()
    await server.wait_closed()


# ---- the six adapters ------------------------------------------------------------------------------------------------------------------------


def _search_exa(key: str, **kw: Any):
    return search_exa.ExaAdapter(ExaConfig(api_key=SecretStr(key)), **kw)


def _search_firecrawl(key: str, **kw: Any):
    return search_firecrawl.FirecrawlAdapter(FirecrawlConfig(api_key=SecretStr(key)), **kw)


def _search_tavily(key: str, **kw: Any):
    return search_tavily.TavilyAdapter(TavilyConfig(api_key=SecretStr(key)), **kw)


def _fetch_exa(key: str, **kw: Any):
    return fetch_exa.ExaAdapter(ExaFetchConfig(api_key=SecretStr(key)), **kw)


def _fetch_firecrawl(key: str, **kw: Any):
    return fetch_firecrawl.FirecrawlAdapter(FirecrawlFetchConfig(api_key=SecretStr(key)), **kw)


def _fetch_jina(key: str, **kw: Any):
    return fetch_jina.JinaAdapter(JinaFetchConfig(api_key=SecretStr(key)), **kw)


# kind, build(key, **kw), a call that makes the adapter send its request
SEARCH_HEADER = {"exa": _search_exa, "firecrawl": _search_firecrawl}               # the key rides in a header: h11 refuses a newline in it
FETCH_HEADER = {"exa": _fetch_exa, "firecrawl": _fetch_firecrawl, "jina": _fetch_jina}
SEARCH_ALL = {**SEARCH_HEADER, "tavily": _search_tavily}


async def _search(adapter) -> None:
    await adapter.search(query="q", count=1, safe_search="moderate")


async def _fetch(adapter) -> None:
    await adapter.fetch(url="https://example.com/page")


# ---- the adapters' own text ------------------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("name", sorted(SEARCH_HEADER))
async def test_a_search_adapter_masks_a_header_key_h11_refuses(name: str, closing_server_url: str) -> None:
    adapter = SEARCH_HEADER[name](PASTED_KEY, base_url=closing_server_url)
    try:
        with pytest.raises(WebSearchUnavailable) as caught:
            await _search(adapter)
    finally:
        await adapter.aclose()
    text = str(caught.value)
    assert "LocalProtocolError" in text, text            # the reason is kept
    assert KEY_CORE not in text, text                     # the key is not
    assert text.startswith(f"{name} transport: "), text


@pytest.mark.parametrize("name", sorted(FETCH_HEADER))
async def test_a_fetch_adapter_masks_a_header_key_h11_refuses(name: str, closing_server_url: str) -> None:
    adapter = FETCH_HEADER[name](PASTED_KEY, base_url=closing_server_url)
    try:
        with pytest.raises(WebFetchUnavailable) as caught:
            await _fetch(adapter)
    finally:
        await adapter.aclose()
    text = str(caught.value)
    assert "LocalProtocolError" in text, text
    assert KEY_CORE not in text, text
    assert text.startswith(f"{name} transport: "), text


class _EchoingTransport(httpx.AsyncBaseTransport):
    """A transport that fails the way a proxy or a debugging layer does: with the request, header values and body, in the error text."""

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        body = request.content.decode("utf-8", "replace")
        raise httpx.ReadError(f"connection reset; request was {dict(request.headers)} {body}", request=request)


@pytest.mark.parametrize("name", sorted(SEARCH_ALL))
async def test_a_search_adapter_masks_a_key_any_transport_echoes(name: str) -> None:
    adapter = SEARCH_ALL[name]("sk-live-A1b2C3d4E5f6G7h8", client=httpx.AsyncClient(transport=_EchoingTransport()))
    with pytest.raises(WebSearchUnavailable) as caught:
        await _search(adapter)
    text = str(caught.value)
    assert "ReadError" in text and "connection reset" in text, text
    assert KEY_CORE not in text, text


@pytest.mark.parametrize("name", sorted(FETCH_HEADER))
async def test_a_fetch_adapter_masks_a_key_any_transport_echoes(name: str) -> None:
    adapter = FETCH_HEADER[name]("sk-live-A1b2C3d4E5f6G7h8", client=httpx.AsyncClient(transport=_EchoingTransport()))
    with pytest.raises(WebFetchUnavailable) as caught:
        await _fetch(adapter)
    text = str(caught.value)
    assert "ReadError" in text and "connection reset" in text, text
    assert KEY_CORE not in text, text


async def test_a_keyless_jina_adapter_still_reports_its_transport_error() -> None:
    """Jina's key is optional (its reader answers anonymously): with no key there is nothing to mask and the reason must still come through."""
    adapter = fetch_jina.JinaAdapter(JinaFetchConfig(api_key=None), client=httpx.AsyncClient(transport=_EchoingTransport()))
    with pytest.raises(WebFetchUnavailable) as caught:
        await _fetch(adapter)
    text = str(caught.value)
    assert text.startswith("jina transport: ReadError: connection reset"), text


# ---- what the agent is given, and what is logged -----------------------------------------------------------------------------------------------


class _Registry:
    def __init__(self, adapters: dict[str, Any]) -> None:
        self._adapters = adapters

    async def get(self, provider_id: str) -> Any:
        return self._adapters[provider_id]


class _Row:
    def __init__(self, row: Any) -> None:
        self.row = row

    async def get(self, _id: str) -> Any:
        return self.row


def _log_texts(caplog: pytest.LogCaptureFixture) -> list[str]:
    """Everything every record of Primer's own loggers carries: its message and every string attribute (``extra={"error": ...}`` is one).

    Primer's loggers only (the tests raise the level of ``primer``, not of the root): ``httpcore`` logs ``send_request_headers.failed
    exception=LocalProtocolError(...)`` at DEBUG with the header value in it, in any process that runs at DEBUG, for every httpx call. That line is
    the library's, not the adapters', and is not what this ticket covers; see the PR.
    """
    texts: list[str] = []
    for record in caplog.records:
        if not record.name.startswith("primer"):
            continue
        texts.append(record.getMessage())
        texts.extend(str(value) for value in vars(record).values() if isinstance(value, str))
    return texts


def _search_service(adapters: dict[str, Any], active: Any) -> WebSearchService:
    return WebSearchService(registry=_Registry(adapters), active_config_storage=_Row(active))


def _fetch_service(adapters: dict[str, Any], active: Any) -> WebFetchService:
    return WebFetchService(registry=_Registry(adapters), active_config_storage=_Row(active))


def _single_search(pid: str) -> ActiveWebSearchConfig:
    return ActiveWebSearchConfig(id=ACTIVE_WEB_SEARCH_CONFIG_ID, config=SingleProviderConfig(provider_id=pid))


def _aggregated_search(pids: list[str]) -> ActiveWebSearchConfig:
    return ActiveWebSearchConfig(id=ACTIVE_WEB_SEARCH_CONFIG_ID, config=AggregatedProviderConfig(provider_ids=pids))


def _single_fetch(pid: str) -> ActiveWebFetchConfig:
    return ActiveWebFetchConfig(id=ACTIVE_WEB_FETCH_CONFIG_ID, config=SingleFetchConfig(provider_id=pid))


def _aggregated_fetch(pids: list[str]) -> ActiveWebFetchConfig:
    return ActiveWebFetchConfig(id=ACTIVE_WEB_FETCH_CONFIG_ID, config=AggregatedFetchConfig(provider_ids=pids))


@pytest.mark.parametrize("mode", ["single", "aggregated"])
@pytest.mark.parametrize("name", sorted(SEARCH_HEADER))
async def test_the_web_search_tool_and_its_logs_carry_no_key(
    name: str, mode: str, closing_server_url: str, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG, logger="primer")
    adapter = SEARCH_HEADER[name](PASTED_KEY, base_url=closing_server_url)
    active = _single_search("p") if mode == "single" else _aggregated_search(["p"])
    handler = make_web_search_handler(_search_service({"p": adapter}, active))
    try:
        result = await handler({"query": "q", "count": 1})
    finally:
        await adapter.aclose()
    assert result.is_error is True
    assert result.output.startswith("web-search failed: "), result.output
    assert "LocalProtocolError" in result.output, result.output       # the agent is told WHY, not the key
    assert KEY_CORE not in result.output, result.output
    logged = _log_texts(caplog)
    assert any("LocalProtocolError" in text for text in logged), "the failure was not logged at all, so the log check proves nothing"
    assert not [text for text in logged if KEY_CORE in text], [text for text in logged if KEY_CORE in text]


@pytest.mark.parametrize("mode", ["single", "aggregated"])
@pytest.mark.parametrize("name", sorted(FETCH_HEADER))
async def test_the_web_fetch_tool_and_its_logs_carry_no_key(
    name: str, mode: str, closing_server_url: str, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG, logger="primer")
    adapter = FETCH_HEADER[name](PASTED_KEY, base_url=closing_server_url)
    active = _single_fetch("p") if mode == "single" else _aggregated_fetch(["p"])
    handler = make_web_fetch_handler(_fetch_service({"p": adapter}, active))
    try:
        result = await handler({"url": "https://example.com/page"})
    finally:
        await adapter.aclose()
    assert result.is_error is True
    assert result.output.startswith("web-fetch failed: "), result.output
    assert "LocalProtocolError" in result.output, result.output
    assert KEY_CORE not in result.output, result.output
    logged = _log_texts(caplog)
    assert any("LocalProtocolError" in text for text in logged), "the failure was not logged at all, so the log check proves nothing"
    assert not [text for text in logged if KEY_CORE in text], [text for text in logged if KEY_CORE in text]


# ---- the guard -------------------------------------------------------------------------------------------------------------------------------

_KEYED_ADAPTERS = (
    "primer/web_search/exa.py",
    "primer/web_search/firecrawl.py",
    "primer/web_search/tavily.py",
    "primer/web_fetch/exa.py",
    "primer/web_fetch/firecrawl.py",
    "primer/web_fetch/jina.py",
)


def test_no_keyed_adapter_formats_a_transport_error_unmasked() -> None:
    """A seventh keyed adapter, or an edit of one of these, that goes back to ``f"... transport: {type(exc).__name__}: {exc}"`` fails here."""
    root = Path(__file__).resolve().parents[2]
    for relative in _KEYED_ADAPTERS:
        source = (root / relative).read_text(encoding="utf-8")
        assert "transport: {type(exc).__name__}: {exc}" not in source, f"{relative} puts the transport error's text in its message unmasked"
        assert "scrubbed_event_text" in source or "transport_failure" in source, f"{relative} does not mask its transport errors"
