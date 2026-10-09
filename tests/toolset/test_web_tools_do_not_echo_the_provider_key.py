"""The web_search / web_fetch tools must not echo the deployment's provider key (ticket 01a12010, #686).

A pasted key often ends in a newline. httpx hands it to h11 as a header value and h11 refuses it: ``LocalProtocolError: Illegal header value b'<the
key>\\n'``. The adapters' transport wrappers put that text into ``WebSearchUnavailable`` / ``WebFetchUnavailable``, the tools return it to the AGENT
(``web-search failed: ...``: the session transcript, the model vendor, every user of the session), the services and tools log it, and the OpenTelemetry
httpx client span records the raw exception (status description, ``exception.message``, stack trace) when OTLP export is on.

Two layers, both pinned here:

* The header adapters (Exa and Firecrawl search and fetch, Jina fetch) REFUSE to send a key that h11 would reject (surrounding whitespace, a control
  or a non-ASCII character) and raise their provider error, with no key in the text. No request is made, so no span, no ``httpcore`` DEBUG line and no
  ``UnicodeEncodeError`` for a non-ASCII key either; aggregated mode falls back as for any misconfigured provider.
* Every keyed adapter masks the key it holds in the text of any transport error it raises (``primer.llm._failure.scrubbed_event_text`` with the
  adapter's own config, the rule the LLM adapters use): a transport that echoes the request (a proxy, a debugging layer) carries the key of a VALID key too,
  and Tavily keeps its key in the JSON body, where h11 never looks.

HERMETIC: the refusal tests point ``base_url`` at a local server that accepts and closes and assert it saw no connection; the mask tests use an in-memory
transport that fails with the request in its text.
"""

from __future__ import annotations

import ast
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
from primer.web_fetch.adapter import WebFetchProviderError, WebFetchUnavailable
from primer.web_fetch.service import WebFetchService
from primer.web_search.adapter import WebSearchProviderError, WebSearchUnavailable
from primer.web_search.service import WebSearchService
from tests.web_loopback import closing_server

KEY_CORE = "sk-live-A1b2C3d4E5f6G7h8"
# A key the generic ``Bearer <token>`` mask cannot hide (no run of 8 token characters before a "!"): only the mask by the configured value does, so a
# adapter that hands the scrub the WRONG config (None, another key) leaves it in the text.
PINNED_KEY = "sk-live!A1b2!C3d4!E5f6!G7h8"
REFUSAL = "api_key has surrounding whitespace or a control/non-ASCII character; re-enter it"

# every way a key can be one h11 refuses (or that cannot be sent as a header at all)
BAD_KEYS = {
    "trailing-lf": KEY_CORE + "\n",
    "trailing-crlf": KEY_CORE + "\r\n",
    "trailing-tab": KEY_CORE + "\t",
    "trailing-space": KEY_CORE + " ",
    "leading-space": " " + KEY_CORE,
    "trailing-nul": KEY_CORE + "\x00",
    "interior-control": "sk-live-A1b2\x01C3d4E5f6G7h8",
    "non-ascii": "sk-live-A1b2éC3d4E5f6G7h8",
}


def _fragment_in(text: str, secret: str, window: int = 6) -> str | None:
    """A ``window``-character piece of ``secret`` that survives in ``text``, or None: not just the whole key, any readable slice of it."""
    secret = secret.strip()
    return next((secret[i : i + window] for i in range(len(secret) - window + 1) if secret[i : i + window] in text), None)


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


SEARCH_HEADER = {"exa": _search_exa, "firecrawl": _search_firecrawl}               # the key rides in a header: h11 refuses a newline in it
FETCH_HEADER = {"exa": _fetch_exa, "firecrawl": _fetch_firecrawl, "jina": _fetch_jina}
SEARCH_ALL = {**SEARCH_HEADER, "tavily": _search_tavily}

# what an echoed header looks like once the configured key is masked: the Bearer adapters and the x-api-key one
ECHOED_MASK = {
    ("search", "exa"): "'x-api-key': '[REDACTED]'",
    ("search", "firecrawl"): "'authorization': 'Bearer [REDACTED]'",
    ("search", "tavily"): '"api_key":"[REDACTED]"',
    ("fetch", "exa"): "'x-api-key': '[REDACTED]'",
    ("fetch", "firecrawl"): "'authorization': 'Bearer [REDACTED]'",
    ("fetch", "jina"): "'authorization': 'Bearer [REDACTED]'",
}


async def _search(adapter) -> None:
    await adapter.search(query="q", count=1, safe_search="moderate")


async def _fetch(adapter) -> None:
    await adapter.fetch(url="https://example.com/page")


class _EchoingTransport(httpx.AsyncBaseTransport):
    """A transport that fails the way a proxy or a debugging layer does: with the request, header values and body, in the error text."""

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        body = request.content.decode("utf-8", "replace")
        raise httpx.ReadError(f"connection reset; request was {dict(request.headers)} {body}", request=request)


def _echoing_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=_EchoingTransport())


# ---- layer 1: a key h11 would refuse is not sent ----------------------------------------------------------------------------------------------


@pytest.mark.parametrize("bad", sorted(BAD_KEYS))
@pytest.mark.parametrize("name", sorted(SEARCH_HEADER))
async def test_a_search_adapter_refuses_to_send_a_key_h11_would_reject(name: str, bad: str) -> None:
    async with closing_server() as server:
        adapter = SEARCH_HEADER[name](BAD_KEYS[bad], base_url=server.url)
        try:
            with pytest.raises(WebSearchProviderError) as caught:
                await _search(adapter)
        finally:
            await adapter.aclose()
        text = str(caught.value)
        assert text == f"{name} {REFUSAL}", text                                      # the reason, and nothing of the key
        assert _fragment_in(text, KEY_CORE) is None, text
        assert server.connections == 0, "the adapter connected: the request was sent"


@pytest.mark.parametrize("bad", sorted(BAD_KEYS))
@pytest.mark.parametrize("name", sorted(FETCH_HEADER))
async def test_a_fetch_adapter_refuses_to_send_a_key_h11_would_reject(name: str, bad: str) -> None:
    async with closing_server() as server:
        adapter = FETCH_HEADER[name](BAD_KEYS[bad], base_url=server.url)
        try:
            with pytest.raises(WebFetchProviderError) as caught:
                await _fetch(adapter)
        finally:
            await adapter.aclose()
        text = str(caught.value)
        assert text == f"{name} {REFUSAL}", text
        assert _fragment_in(text, KEY_CORE) is None, text
        assert server.connections == 0, "the adapter connected: the request was sent"


@pytest.mark.parametrize("name", sorted(SEARCH_ALL))
async def test_a_valid_key_is_sent(name: str) -> None:
    """The check refuses only what cannot be sent: a plain key (an inner space is fine for h11) reaches the transport."""
    seen: list[httpx.Request] = []

    class _Recording(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
            seen.append(request)
            return httpx.Response(500)          # a server error is a known failure for every adapter: the point is that the request got here

    adapter = SEARCH_ALL[name]("sk-live A1b2-C3d4", client=httpx.AsyncClient(transport=_Recording()))
    with pytest.raises(WebSearchUnavailable):
        await _search(adapter)
    assert len(seen) == 1


async def test_tavily_keeps_its_key_in_the_body_so_h11_never_sees_it() -> None:
    """Tavily is not a header adapter: a padded key is JSON-escaped in the body and the request goes out; its transport errors are masked all the same."""
    async with closing_server() as server:
        adapter = _search_tavily(BAD_KEYS["trailing-lf"], base_url=server.url)
        try:
            with pytest.raises(WebSearchUnavailable) as caught:
                await _search(adapter)
        finally:
            await adapter.aclose()
        assert server.connections == 1
        assert _fragment_in(str(caught.value), KEY_CORE) is None, str(caught.value)


async def test_a_keyless_jina_adapter_is_not_refused() -> None:
    """Jina's key is optional (the reader answers anonymously): with no key there is nothing to refuse and nothing to mask; the reason still comes through."""
    adapter = fetch_jina.JinaAdapter(JinaFetchConfig(api_key=None), client=_echoing_client())
    with pytest.raises(WebFetchUnavailable) as caught:
        await _fetch(adapter)
    assert str(caught.value).startswith("jina transport: ReadError: connection reset"), str(caught.value)


# ---- layer 2: a transport error is masked with the adapter's own key ---------------------------------------------------------------------------


@pytest.mark.parametrize("name", sorted(SEARCH_ALL))
async def test_a_search_adapter_masks_the_key_any_transport_echoes(name: str) -> None:
    adapter = SEARCH_ALL[name](PINNED_KEY, client=_echoing_client())
    with pytest.raises(WebSearchUnavailable) as caught:
        await _search(adapter)
    text = str(caught.value)
    assert text.startswith(f"{name} transport: ReadError: connection reset"), text
    assert _fragment_in(text, PINNED_KEY) is None, text
    assert ECHOED_MASK[("search", name)] in text, text       # THIS adapter's key, masked where it was sent
    # the original exception is not chained: a traceback printed anywhere shows the masked text only (``from None``)
    assert caught.value.__cause__ is None and caught.value.__suppress_context__ is True


@pytest.mark.parametrize("name", sorted(FETCH_HEADER))
async def test_a_fetch_adapter_masks_the_key_any_transport_echoes(name: str) -> None:
    adapter = FETCH_HEADER[name](PINNED_KEY, client=_echoing_client())
    with pytest.raises(WebFetchUnavailable) as caught:
        await _fetch(adapter)
    text = str(caught.value)
    assert text.startswith(f"{name} transport: ReadError: connection reset"), text
    assert _fragment_in(text, PINNED_KEY) is None, text
    assert ECHOED_MASK[("fetch", name)] in text, text
    assert caught.value.__cause__ is None and caught.value.__suppress_context__ is True


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
    """Everything every record of Primer's own loggers carries: its message and every string attribute (``extra={"error": ...}`` is one)."""
    texts: list[str] = []
    for record in caplog.records:
        if not record.name.startswith("primer"):
            continue
        texts.append(record.getMessage())
        texts.extend(str(value) for value in vars(record).values() if isinstance(value, str))
    return texts


def _search_handler(adapter: Any, mode: str):
    active = (
        ActiveWebSearchConfig(id=ACTIVE_WEB_SEARCH_CONFIG_ID, config=SingleProviderConfig(provider_id="p"))
        if mode == "single"
        else ActiveWebSearchConfig(id=ACTIVE_WEB_SEARCH_CONFIG_ID, config=AggregatedProviderConfig(provider_ids=["p"]))
    )
    return make_web_search_handler(WebSearchService(registry=_Registry({"p": adapter}), active_config_storage=_Row(active)))


def _fetch_handler(adapter: Any, mode: str):
    active = (
        ActiveWebFetchConfig(id=ACTIVE_WEB_FETCH_CONFIG_ID, config=SingleFetchConfig(provider_id="p"))
        if mode == "single"
        else ActiveWebFetchConfig(id=ACTIVE_WEB_FETCH_CONFIG_ID, config=AggregatedFetchConfig(provider_ids=["p"]))
    )
    return make_web_fetch_handler(WebFetchService(registry=_Registry({"p": adapter}), active_config_storage=_Row(active)))


def _assert_clean_logs(caplog: pytest.LogCaptureFixture, reason: str, secret: str) -> None:
    logged = _log_texts(caplog)
    assert any(reason in text for text in logged), "the failure was not logged at all, so the log check proves nothing"
    leaked = [text for text in logged if _fragment_in(text, secret) is not None]
    assert not leaked, leaked


@pytest.mark.parametrize("bad", sorted(BAD_KEYS))
@pytest.mark.parametrize("mode", ["single", "aggregated"])
@pytest.mark.parametrize("name", sorted(SEARCH_HEADER))
async def test_the_web_search_tool_refuses_a_padded_key_and_says_why(name: str, mode: str, bad: str, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG, logger="primer")
    async with closing_server() as server:
        adapter = SEARCH_HEADER[name](BAD_KEYS[bad], base_url=server.url)
        try:
            result = await _search_handler(adapter, mode)({"query": "q", "count": 1})
        finally:
            await adapter.aclose()
        assert server.connections == 0
    assert result.is_error is True
    if mode == "single":
        assert result.output == f"web-search not available: {name} {REFUSAL}", result.output
    else:       # the aggregated summary: every provider failed, each with its reason
        assert result.output == f"web-search failed: all 1 providers failed: p: WebSearchProviderError: {name} {REFUSAL}", result.output
    assert _fragment_in(result.output, KEY_CORE) is None, result.output
    _assert_clean_logs(caplog, REFUSAL, KEY_CORE)


@pytest.mark.parametrize("bad", sorted(BAD_KEYS))
@pytest.mark.parametrize("mode", ["single", "aggregated"])
@pytest.mark.parametrize("name", sorted(FETCH_HEADER))
async def test_the_web_fetch_tool_refuses_a_padded_key_and_says_why(name: str, mode: str, bad: str, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG, logger="primer")
    async with closing_server() as server:
        adapter = FETCH_HEADER[name](BAD_KEYS[bad], base_url=server.url)
        try:
            result = await _fetch_handler(adapter, mode)({"url": "https://example.com/page"})
        finally:
            await adapter.aclose()
        assert server.connections == 0
    assert result.is_error is True
    if mode == "single":
        assert result.output == f"web-fetch not available: {name} {REFUSAL}", result.output
    else:
        assert result.output == f"web-fetch failed: all 1 providers failed: p: WebFetchProviderError: {name} {REFUSAL}", result.output
    assert _fragment_in(result.output, KEY_CORE) is None, result.output
    _assert_clean_logs(caplog, REFUSAL, KEY_CORE)


@pytest.mark.parametrize("mode", ["single", "aggregated"])
@pytest.mark.parametrize("name", sorted(SEARCH_ALL))
async def test_the_web_search_tool_and_its_logs_carry_no_echoed_key(name: str, mode: str, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG, logger="primer")
    adapter = SEARCH_ALL[name](PINNED_KEY, client=_echoing_client())
    result = await _search_handler(adapter, mode)({"query": "q", "count": 1})
    assert result.is_error is True and result.output.startswith("web-search failed: "), result.output
    assert "ReadError" in result.output, result.output       # the agent is told WHY, not the key
    assert _fragment_in(result.output, PINNED_KEY) is None, result.output
    _assert_clean_logs(caplog, "ReadError", PINNED_KEY)


@pytest.mark.parametrize("mode", ["single", "aggregated"])
@pytest.mark.parametrize("name", sorted(FETCH_HEADER))
async def test_the_web_fetch_tool_and_its_logs_carry_no_echoed_key(name: str, mode: str, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG, logger="primer")
    adapter = FETCH_HEADER[name](PINNED_KEY, client=_echoing_client())
    result = await _fetch_handler(adapter, mode)({"url": "https://example.com/page"})
    assert result.is_error is True and result.output.startswith("web-fetch failed: "), result.output
    assert "ReadError" in result.output, result.output
    assert _fragment_in(result.output, PINNED_KEY) is None, result.output
    _assert_clean_logs(caplog, "ReadError", PINNED_KEY)


# ---- the guard -------------------------------------------------------------------------------------------------------------------------------

_KEYED_ADAPTERS = (
    "primer/web_fetch/exa.py",
    "primer/web_fetch/firecrawl.py",
    "primer/web_fetch/jina.py",
    "primer/web_search/exa.py",
    "primer/web_search/firecrawl.py",
    "primer/web_search/tavily.py",
)
_HEADER_ADAPTERS = tuple(path for path in _KEYED_ADAPTERS if not path.endswith("tavily.py"))


def _keyed_adapter_sources(root: Path | None = None) -> dict[str, str]:
    """The modules under primer/web_search and primer/web_fetch (and anything below them) that mention ``api_key``: found, not listed, so a seventh keyed adapter is seen."""
    root = root or Path(__file__).resolve().parents[2]
    sources = {}
    for package in ("primer/web_search", "primer/web_fetch"):
        for path in sorted((root / package).rglob("*.py")):
            text = path.read_text(encoding="utf-8")
            if "api_key" in text:
                sources[str(path.relative_to(root))] = text
    return sources


def _assert_the_keyed_adapters_are_covered(root: Path | None = None) -> None:
    found = sorted(_keyed_adapter_sources(root))
    assert found == sorted(_KEYED_ADAPTERS), f"keyed adapters this module does not cover (or lost): {sorted(set(found) ^ set(_KEYED_ADAPTERS))}"


def test_the_keyed_adapters_are_the_six_this_module_covers() -> None:
    """A new keyed adapter has to be added to the lists above, with its tests, before this passes."""
    _assert_the_keyed_adapters_are_covered()


def test_the_discovery_sees_a_seventh_keyed_adapter(tmp_path: Path) -> None:
    """The guard itself is pinned: a tree holding the six AND a planted seventh module that mentions ``api_key`` (also one in a sub-package) fails it."""
    for relative in _KEYED_ADAPTERS:
        target = tmp_path / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("api_key = None\n", encoding="utf-8")
    _assert_the_keyed_adapters_are_covered(tmp_path)          # the six alone pass

    planted = tmp_path / "primer" / "web_search" / "brave.py"
    planted.write_text("class BraveAdapter:\n    def __init__(self, config):\n        self.api_key = config.api_key\n", encoding="utf-8")
    with pytest.raises(AssertionError, match="brave"):
        _assert_the_keyed_adapters_are_covered(tmp_path)
    planted.unlink()

    nested = tmp_path / "primer" / "web_fetch" / "providers" / "brave.py"
    nested.parent.mkdir()
    nested.write_text("api_key = 1\n", encoding="utf-8")
    with pytest.raises(AssertionError, match="brave"):
        _assert_the_keyed_adapters_are_covered(tmp_path)


def _chains_a_transport_failure(source: str) -> list[int]:
    """Line numbers of ``raise ... transport_failure(...) from <anything but None>``, found in the syntax tree (not by looking at one line)."""
    lines = []
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.Raise) or node.cause is None or node.exc is None:
            continue
        calls_it = any(isinstance(sub, ast.Call) and getattr(sub.func, "id", getattr(sub.func, "attr", None)) == "transport_failure" for sub in ast.walk(node.exc))
        if calls_it and not (isinstance(node.cause, ast.Constant) and node.cause.value is None):
            lines.append(node.lineno)
    return lines


def test_no_keyed_adapter_formats_a_transport_error_unmasked() -> None:
    """An edit of one of these (or a new keyed adapter) that goes back to ``f"... transport: {type(exc).__name__}: {exc}"`` fails here."""
    for relative, source in _keyed_adapter_sources().items():
        assert "transport: {type(exc).__name__}: {exc}" not in source, f"{relative} puts the transport error's text in its message unmasked"
        assert "transport_failure(" in source, f"{relative} does not mask its transport errors"
        assert not _chains_a_transport_failure(source), f"{relative} chains the raw transport exception (raise ... from exc) at lines {_chains_a_transport_failure(source)}"


def test_the_chain_check_sees_a_from_exc_however_the_raise_is_laid_out() -> None:
    one_line = "def f():\n    try:\n        pass\n    except Exception as exc:\n        raise E(transport_failure('x', exc, c)) from exc\n"
    laid_out = "def f():\n    try:\n        pass\n    except Exception as exc:\n        raise E(\n            transport_failure('x', exc, c)\n        ) from (\n            exc\n        )\n"
    clean = one_line.replace("from exc", "from None")

    assert _chains_a_transport_failure(one_line) == [5]
    assert _chains_a_transport_failure(laid_out) == [5]
    assert _chains_a_transport_failure(clean) == []


def test_every_header_adapter_checks_its_key_before_sending() -> None:
    for relative, source in _keyed_adapter_sources().items():
        if relative in _HEADER_ADAPTERS:
            assert "require_sendable_key(" in source, f"{relative} sends its key as a header without checking that h11 can carry it"
