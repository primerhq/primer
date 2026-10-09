"""``POST /v1/web_search_providers/_test`` and ``/v1/web_fetch_providers/_test`` do not echo the draft's key (ticket 01a12010, #686).

Both routes build the adapter from the typed draft and return its failure text with a 200. The header adapters refuse a key h11 would reject (every padded form of
``BAD_KEYS``, the same eight the tools are tested over), and every keyed adapter scrubs a provider's text before it cuts it. The adapter the route builds is pointed at a loopback
server (the factory is replaced), so a regression would be seen as a connection, never opened to a real host; a refusal makes none.
"""

from __future__ import annotations

import json

import pytest

from primer.web_fetch.exa import ExaAdapter as FetchExa
from primer.web_search.exa import ExaAdapter as SearchExa
from primer.web_search.firecrawl import FirecrawlAdapter as SearchFirecrawl
from tests.toolset.test_web_tools_do_not_echo_the_provider_key import BAD_KEYS, REFUSAL, _fragment_in
from tests.web_loopback import closing_server, status_server

KEY = "sk-live-Q7r8S9t0U1v2W3x4"
SEARCH_FACTORY = "primer.api.registries.web_search_registry.default_web_search_factory"
FETCH_FACTORY = "primer.api.registries.web_fetch_registry.default_web_fetch_factory"


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", sorted(BAD_KEYS))
async def test_the_search_route_refuses_a_padded_key_without_echoing_it(client, monkeypatch, bad: str) -> None:
    async with closing_server() as server:
        monkeypatch.setattr(SEARCH_FACTORY, lambda provider: SearchExa(provider.config, base_url=server.url))

        r = await client.post(
            "/v1/web_search_providers/_test",
            json={"id": "d", "provider_type": "exa", "config": {"type": "exa", "api_key": BAD_KEYS[bad]}},
        )

        body = r.json()
        assert r.status_code == 200 and body["ok"] is False, r.text
        assert body["error"] == f"exa {REFUSAL}", body["error"]
        assert _fragment_in(r.text, KEY) is None, r.text
        assert server.connections == 0, "the route's adapter connected: the request was sent"


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", sorted(BAD_KEYS))
async def test_the_fetch_route_refuses_a_padded_key_without_echoing_it(client, monkeypatch, bad: str) -> None:
    async with closing_server() as server:
        monkeypatch.setattr(FETCH_FACTORY, lambda provider: FetchExa(provider.config, base_url=server.url))

        r = await client.post(
            "/v1/web_fetch_providers/_test",
            json={"id": "d", "provider_type": "exa", "config": {"type": "exa", "api_key": BAD_KEYS[bad]}},
        )

        body = r.json()
        assert r.status_code == 200 and body["ok"] is False, r.text
        assert body["error"] == f"exa {REFUSAL}", body["error"]
        assert _fragment_in(r.text, KEY) is None, r.text
        assert server.connections == 0, "the route's adapter connected: the request was sent"


@pytest.mark.asyncio
async def test_the_search_route_scrubs_a_vendors_body_before_it_cuts_it(client, monkeypatch) -> None:
    async with status_server(400, "x" * 190 + KEY + " was rejected") as server:
        monkeypatch.setattr(SEARCH_FACTORY, lambda provider: SearchExa(provider.config, base_url=server.url))

        r = await client.post(
            "/v1/web_search_providers/_test",
            json={"id": "d", "provider_type": "exa", "config": {"type": "exa", "api_key": KEY}},
        )

    body = r.json()
    assert r.status_code == 200 and body["ok"] is False, r.text
    assert body["error"].startswith("exa unexpected status 400: xxxx"), body["error"]
    assert _fragment_in(r.text, KEY) is None, r.text


@pytest.mark.asyncio
async def test_the_search_route_scrubs_a_success_false_text_before_it_cuts_it(client, monkeypatch) -> None:
    async with status_server(200, json.dumps({"success": False, "error": "x" * 190 + KEY + " was rejected"})) as server:
        monkeypatch.setattr(SEARCH_FACTORY, lambda provider: SearchFirecrawl(provider.config, base_url=server.url))

        r = await client.post(
            "/v1/web_search_providers/_test",
            json={"id": "d", "provider_type": "firecrawl", "config": {"type": "firecrawl", "api_key": KEY}},
        )

    body = r.json()
    assert r.status_code == 200 and body["ok"] is False, r.text
    assert body["error"].startswith("firecrawl reported failure: xxxx"), body["error"]
    assert _fragment_in(r.text, KEY) is None, r.text


@pytest.mark.asyncio
async def test_the_fetch_route_scrubs_a_vendors_body_before_it_cuts_it(client, monkeypatch) -> None:
    async with status_server(400, "x" * 190 + KEY + " was rejected") as server:
        monkeypatch.setattr(FETCH_FACTORY, lambda provider: FetchExa(provider.config, base_url=server.url))

        r = await client.post(
            "/v1/web_fetch_providers/_test",
            json={"id": "d", "provider_type": "exa", "config": {"type": "exa", "api_key": KEY}},
        )

    body = r.json()
    assert r.status_code == 200 and body["ok"] is False, r.text
    assert body["error"].startswith("exa unexpected status 400: xxxx"), body["error"]
    assert _fragment_in(r.text, KEY) is None, r.text
