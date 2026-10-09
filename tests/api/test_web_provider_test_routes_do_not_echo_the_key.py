"""``POST /v1/web_search_providers/_test`` and ``/v1/web_fetch_providers/_test`` do not echo the draft's key (ticket 01a12010, #686).

Both routes build the adapter from the typed draft and return its failure text with a 200. The header adapters refuse a key h11 would reject (no request is
made, so these cases need no server at all), and every keyed adapter scrubs a provider's non-2xx body before it cuts it.
"""

from __future__ import annotations

import pytest

from primer.web_fetch.exa import ExaAdapter as FetchExa
from primer.web_search.exa import ExaAdapter as SearchExa
from tests.web_loopback import status_server

KEY = "sk-live-Q7r8S9t0U1v2W3x4"
REFUSAL = "api_key has surrounding whitespace or a control/non-ASCII character; re-enter it"


def _fragment_in(text: str, secret: str, window: int = 6) -> str | None:
    secret = secret.strip()
    return next((secret[i : i + window] for i in range(len(secret) - window + 1) if secret[i : i + window] in text), None)


@pytest.mark.asyncio
@pytest.mark.parametrize("padding", ["\n", "\r\n", "\t", " "])
async def test_the_search_route_refuses_a_padded_key_without_echoing_it(client, padding: str) -> None:
    r = await client.post(
        "/v1/web_search_providers/_test",
        json={"id": "d", "provider_type": "exa", "config": {"type": "exa", "api_key": KEY + padding}},
    )

    body = r.json()
    assert r.status_code == 200 and body["ok"] is False, r.text
    assert body["error"] == f"exa {REFUSAL}", body["error"]
    assert _fragment_in(r.text, KEY) is None, r.text


@pytest.mark.asyncio
@pytest.mark.parametrize("padding", ["\n", "\r\n", "\t", " "])
async def test_the_fetch_route_refuses_a_padded_key_without_echoing_it(client, padding: str) -> None:
    r = await client.post(
        "/v1/web_fetch_providers/_test",
        json={"id": "d", "provider_type": "exa", "config": {"type": "exa", "api_key": KEY + padding}},
    )

    body = r.json()
    assert r.status_code == 200 and body["ok"] is False, r.text
    assert body["error"] == f"exa {REFUSAL}", body["error"]
    assert _fragment_in(r.text, KEY) is None, r.text


@pytest.mark.asyncio
async def test_the_search_route_scrubs_a_vendors_body_before_it_cuts_it(client, monkeypatch) -> None:
    async with status_server(400, "x" * 190 + KEY + " was rejected") as server:
        monkeypatch.setattr(
            "primer.api.registries.web_search_registry.default_web_search_factory",
            lambda provider: SearchExa(provider.config, base_url=server.url),
        )

        r = await client.post(
            "/v1/web_search_providers/_test",
            json={"id": "d", "provider_type": "exa", "config": {"type": "exa", "api_key": KEY}},
        )

    body = r.json()
    assert r.status_code == 200 and body["ok"] is False, r.text
    assert body["error"].startswith("exa unexpected status 400: xxxx"), body["error"]
    assert _fragment_in(r.text, KEY) is None, r.text


@pytest.mark.asyncio
async def test_the_fetch_route_scrubs_a_vendors_body_before_it_cuts_it(client, monkeypatch) -> None:
    async with status_server(400, "x" * 190 + KEY + " was rejected") as server:
        monkeypatch.setattr(
            "primer.api.registries.web_fetch_registry.default_web_fetch_factory",
            lambda provider: FetchExa(provider.config, base_url=server.url),
        )

        r = await client.post(
            "/v1/web_fetch_providers/_test",
            json={"id": "d", "provider_type": "exa", "config": {"type": "exa", "api_key": KEY}},
        )

    body = r.json()
    assert r.status_code == 200 and body["ok"] is False, r.text
    assert body["error"].startswith("exa unexpected status 400: xxxx"), body["error"]
    assert _fragment_in(r.text, KEY) is None, r.text
