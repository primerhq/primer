"""The draft ``_test`` routes of the speech and web providers do not echo a credential back (ticket 01a11cdf part 1; found in the review of #580).

``POST /v1/stt_providers/_test`` and ``/v1/tts_providers/_test`` (admin-only, nothing persisted) answered ``{"ok": false, "error": "HTTPStatusError: ... for url
'http://svc:s3cr3t-pw@...'"}``: httpx prints the whole request URL, userinfo included, and the route returned ``f"{type(exc).__name__}: {exc}"``. The web search and web fetch
routes have the same shape. The LLM and embedding probes already clean this text (``_probe_failure``, #580); these four go through the same cleaning now.

The draft that does not even validate echoed too: pydantic prints ``input_value=<the value as typed>``, cut to the first 25 and the last 24 characters of a long
value, which removes the ``@`` and leaves a slice of the password readable, so the text of a validation error is the layout WITHOUT its input
(``validation_detail``, shared with the LLM and embedding probes).
"""

from __future__ import annotations

import pytest

_PASSWORDS = ["s3cr3t-pw", "pa'ssword"]


def _stt(url: str) -> dict:
    return {"id": "stt-a", "provider": "openai", "default_model": "whisper-1", "config": {"url": url}, "limits": {"max_concurrency": 1}}


def _tts(url: str) -> dict:
    return {"id": "tts-a", "provider": "openai", "default_model": "kokoro", "default_voice": "af", "config": {"url": url}, "limits": {"max_concurrency": 1}}


def _assert_clean(error: str, password: str) -> None:
    assert password not in error, f"the response still carries the password: {error!r}"
    assert "[REDACTED]" in error, f"the credential should be masked, not dropped silently: {error!r}"


# ---- the probe itself fails with the credentialed URL in its text -----------------------------------------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("password", _PASSWORDS)
async def test_the_stt_probe_error_is_cleaned(client, monkeypatch, password: str) -> None:
    async def _boom(*, url, api_key, timeout=10.0):
        raise RuntimeError(f"Server error '500 Internal Server Error' for url '{url}/models'")

    monkeypatch.setattr("primer.api.routers.speech.list_models", _boom)

    r = await client.post("/v1/stt_providers/_test", json=_stt(f"http://svc:{password}@asr.local:8006/v1"))

    assert r.status_code == 200, r.text
    assert r.json()["ok"] is False
    _assert_clean(r.json()["error"], password)


@pytest.mark.asyncio
@pytest.mark.parametrize("password", _PASSWORDS)
async def test_the_tts_probe_error_is_cleaned(client, monkeypatch, password: str) -> None:
    async def _boom(*, url, api_key, timeout=10.0):
        raise RuntimeError(f"Client error '401 Unauthorized' for url '{url}/audio/voices'")

    monkeypatch.setattr("primer.api.routers.speech.list_voices", _boom)

    r = await client.post("/v1/tts_providers/_test", json=_tts(f"http://svc:{password}@tts.local:8004/v1"))

    assert r.status_code == 200, r.text
    assert r.json()["ok"] is False
    _assert_clean(r.json()["error"], password)


@pytest.mark.asyncio
async def test_a_probe_error_with_no_credential_reads_as_it_did(client, monkeypatch) -> None:
    async def _boom(*, url, api_key, timeout=10.0):
        raise RuntimeError("connection refused")

    monkeypatch.setattr("primer.api.routers.speech.list_models", _boom)

    r = await client.post("/v1/stt_providers/_test", json=_stt("http://asr.local:8006/v1"))

    assert r.json() == {"ok": False, "error": "RuntimeError: connection refused"}


# ---- the draft does not validate: pydantic's input is not printed -----------------------------------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(("route", "body"), [("stt_providers", _stt), ("tts_providers", _tts)])
async def test_a_draft_that_does_not_validate_does_not_print_the_url_it_was_given(client, route: str, body) -> None:
    r = await client.post(f"/v1/{route}/_test", json=body("http://svc:s3cr3t-pw@asr.local:notaport/v1"))

    assert r.status_code == 200, r.text
    error = r.json()["error"]
    assert r.json()["ok"] is False and error.startswith("invalid draft"), error
    assert "s3cr3t" not in error and "pw@" not in error, error
    assert "url" in error, "the person still needs to know WHICH field is wrong"


# ---- the web providers -------------------------------------------------------------------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_web_search_probe_error_is_cleaned(client, monkeypatch) -> None:
    from primer.web_search.tavily import TavilyAdapter

    async def _boom(self, *, query, count, safe_search):
        raise RuntimeError("Server error '500' for url 'https://u:pw0rd@api.example.test/search?api_key=SECRETKEY123&q=primer'")

    monkeypatch.setattr(TavilyAdapter, "search", _boom)

    r = await client.post("/v1/web_search_providers/_test", json={"id": "d", "provider_type": "tavily", "config": {"type": "tavily", "api_key": "k"}})

    error = r.json()["error"]
    assert r.json()["ok"] is False
    assert "pw0rd" not in error and "SECRETKEY123" not in error, error


@pytest.mark.asyncio
async def test_a_web_search_adapter_error_is_cleaned_too(client, monkeypatch) -> None:
    from primer.web_search.adapter import WebSearchProviderError
    from primer.web_search.tavily import TavilyAdapter

    async def _boom(self, *, query, count, safe_search):
        raise WebSearchProviderError("tavily answered 502 for https://u:pw0rd@api.example.test/search?api_key=SECRETKEY123")

    monkeypatch.setattr(TavilyAdapter, "search", _boom)

    r = await client.post("/v1/web_search_providers/_test", json={"id": "d", "provider_type": "tavily", "config": {"type": "tavily", "api_key": "k"}})

    error = r.json()["error"]
    assert error.startswith("tavily answered 502"), error
    assert "pw0rd" not in error and "SECRETKEY123" not in error, error


@pytest.mark.asyncio
async def test_the_web_fetch_probe_error_is_cleaned(client, monkeypatch) -> None:
    from primer.web_fetch.jina import JinaAdapter

    async def _boom(self, *, url):
        raise RuntimeError("Server error '500' for url 'https://u:pw0rd@r.jina.ai/https://example.com?token=SECRETKEY123'")

    monkeypatch.setattr(JinaAdapter, "fetch", _boom)

    r = await client.post("/v1/web_fetch_providers/_test", json={"id": "d", "provider_type": "jina", "config": {"type": "jina"}})

    error = r.json()["error"]
    assert r.json()["ok"] is False
    assert "pw0rd" not in error and "SECRETKEY123" not in error, error


@pytest.mark.asyncio
async def test_a_web_fetch_adapter_error_is_cleaned_too(client, monkeypatch) -> None:
    from primer.web_fetch.adapter import WebFetchUnavailable
    from primer.web_fetch.jina import JinaAdapter

    async def _boom(self, *, url):
        raise WebFetchUnavailable("jina transport: ConnectError for https://u:pw0rd@r.jina.ai/x?token=SECRETKEY123")

    monkeypatch.setattr(JinaAdapter, "fetch", _boom)

    r = await client.post("/v1/web_fetch_providers/_test", json={"id": "d", "provider_type": "jina", "config": {"type": "jina"}})

    error = r.json()["error"]
    assert error.startswith("jina transport: ConnectError"), error
    assert "pw0rd" not in error and "SECRETKEY123" not in error, error
