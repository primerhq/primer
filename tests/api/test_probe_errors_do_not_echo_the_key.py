"""A probe's failure never carries the configured API key, whatever the key looks like (security ticket 01a11eda-2031).

A key pasted with a trailing newline, space or tab is not a valid header value. ``h11`` refuses it before the request leaves
(``LocalProtocolError: Illegal header value b'Bearer sk-...\\n'``) and the message holds the key whole. The probes interpolated that into the 400
they raise (``OpenRouter discover network error: LocalProtocolError: ...``), and that text went on to the ``POST .../_discover_models`` answer, the
``last_error`` the saved-provider route stamps on the stored row (kept until the next probe), the ``llm_provider`` predicate of ``GET /v1/setup/state``
and the ``{"ok": false, "error": ...}`` of the draft ``_test`` routes of the speech and web providers.

``_probe_failure`` masked URL credentials only. Every probe/discovery exit now masks the provider's CONFIGURED credentials as well, with the same rules
the adapters' in-stream failures use (``primer.llm._failure``), whatever shape the key has: itself, its escaped form (the ``\\n`` an error prints for a
real newline) and its normalised form. These tests drive the real routes and the real httpx/h11 error: nothing is mocked, so a stand-in transport
cannot hide the wrapper (respx replaces the transport h11 sits in, so it is not used here).
"""

from __future__ import annotations

import asyncio

import pytest
import pytest_asyncio

KEY = "sk-live-Q7xZ9pL2mN4vB8kR1tY6wE3"
TAIL = "Q7xZ9pL2mN4vB8kR1tY6wE3"
# A key with a newline INSIDE it: h11 refuses it just the same, and the escaped form of each half is what a repr prints.
INTERNAL = "sk-live-Q7xZ9pL2mN4v\nB8kR1tY6wE3"
# h11 refuses a header value with leading or trailing whitespace
PADDING = ["\n", " ", "\t", "\r\n"]


def _assert_clean(text: str) -> None:
    assert KEY not in text and TAIL not in text, f"the key leaked in {text!r}"
    assert "B8kR1tY6wE3" not in text and "sk-live-Q7xZ9pL2mN4v" not in text, f"half of the key leaked in {text!r}"


@pytest_asyncio.fixture
async def port():
    """A TCP port that accepts and hangs up: the failure happens when the request is written, after the connection, as it does against a real server."""
    server = await asyncio.start_server(lambda reader, writer: writer.close(), "127.0.0.1", 0)
    try:
        yield server.sockets[0].getsockname()[1]
    finally:
        server.close()
        await server.wait_closed()


@pytest.fixture(autouse=True)
def _hermetic(monkeypatch, port):
    """The vendor probes (openrouter, anthropic, gemini) and the web adapters (exa, firecrawl, jina) are pointed at the loopback fixture.

    httpcore opens the connection (DNS, TCP, TLS) BEFORE h11 validates the headers, so with the vendors' real hosts these tests opened real connections to
    third parties: offline the LLM cases failed, and the web cases passed vacuously because they never asserted the failure reason (#672 review, B2).
    """
    import primer.api.registries.web_fetch_registry as fetch_registry
    import primer.api.registries.web_search_registry as search_registry
    import primer.llm.anthropic as anthropic
    import primer.llm.gemini as gemini
    import primer.llm.openrouter as openrouter
    from primer.model.web_fetch import WebFetchProviderType
    from primer.model.web_search import WebSearchProviderType

    base = f"http://127.0.0.1:{port}"
    monkeypatch.setattr(openrouter, "OPENROUTER_BASE_URL", base + "/api/v1")
    monkeypatch.setattr(anthropic, "ANTHROPIC_BASE_URL", base + "/v1")
    monkeypatch.setattr(gemini, "GEMINI_BASE_URL", base + "/v1beta")

    def search_factory(provider):
        from primer.web_search.exa import ExaAdapter
        from primer.web_search.firecrawl import FirecrawlAdapter

        if provider.provider_type == WebSearchProviderType.EXA:
            return ExaAdapter(provider.config, base_url=base)
        return FirecrawlAdapter(provider.config, base_url=base)

    def fetch_factory(provider):
        from primer.web_fetch.exa import ExaAdapter
        from primer.web_fetch.firecrawl import FirecrawlAdapter

        if provider.provider_type == WebFetchProviderType.EXA:
            return ExaAdapter(provider.config, base_url=base)
        return FirecrawlAdapter(provider.config, base_url=base)

    monkeypatch.setattr(search_registry, "default_web_search_factory", search_factory)
    monkeypatch.setattr(fetch_registry, "default_web_fetch_factory", fetch_factory)


def _llm_cases(port: int, key: str) -> dict[str, dict]:
    return {
        "openchat": {"url": f"http://127.0.0.1:{port}/v1", "flavor": "other", "api_key": key},
        "openresponses": {"url": f"http://127.0.0.1:{port}/v1", "flavor": "other", "api_key": key},
        "ollama": {"url": f"http://127.0.0.1:{port}", "api_key": key},
        "openrouter": {"api_key": key},
        "anthropic": {"api_key": key},
        "gemini": {"api_key": key},
    }


class TestLlmDiscovery:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("provider", ["openchat", "openresponses", "ollama", "openrouter", "anthropic", "gemini"])
    @pytest.mark.parametrize("padding", PADDING, ids=["newline", "space", "tab", "crlf"])
    async def test_a_key_the_header_refuses_is_not_echoed_by_the_draft_probe(self, client, port, provider, padding) -> None:
        config = _llm_cases(port, KEY + padding)[provider]

        r = await client.post("/v1/llm_providers/_discover_models", json={"provider": provider, "config": config})

        assert r.status_code == 400, r.text
        _assert_clean(r.text)
        detail = r.json()["detail"]
        assert "LocalProtocolError" in detail and "Illegal header value" in detail, f"what failed must still be said: {detail!r}"
        assert "[REDACTED]" in detail

    @pytest.mark.asyncio
    async def test_a_saved_provider_does_not_echo_the_key_in_the_answer_the_stored_row_or_the_setup_state(self, client, port) -> None:
        r = await client.post("/v1/llm_providers", json={
            "id": "saved-ws-key", "provider": "openchat", "limits": {"max_concurrency": 1},
            "config": {"url": f"http://127.0.0.1:{port}/v1", "flavor": "other", "api_key": KEY + "\n"},
        })
        assert r.status_code in (200, 201), r.text

        probe = await client.get("/v1/llm_providers/saved-ws-key/discovered_models")
        assert probe.status_code == 400, probe.text
        _assert_clean(probe.text)

        row = await client.get("/v1/llm_providers/saved-ws-key")
        assert row.status_code == 200, row.text
        _assert_clean(row.text)
        assert "LocalProtocolError" in row.json()["last_error"]

        state = await client.get("/v1/setup/state")
        assert state.status_code == 200, state.text
        _assert_clean(state.text)

    @pytest.mark.asyncio
    async def test_the_embedding_probe_does_not_echo_the_key(self, client, port) -> None:
        r = await client.post("/v1/embedding_providers/_discover_models", json={
            "provider": "openai", "config": {"url": f"http://127.0.0.1:{port}/v1", "flavor": "lmstudio", "api_key": KEY + "\n"},
        })

        assert r.status_code == 400, r.text
        _assert_clean(r.text)
        assert "LocalProtocolError" in r.json()["detail"]

    @pytest.mark.asyncio
    async def test_an_error_that_carries_no_credential_is_reported_as_it_was(self, client) -> None:
        """Nothing listens on port 9: a refused connection says so, and the clean words are not touched."""
        r = await client.post("/v1/llm_providers/_discover_models", json={
            "provider": "openchat", "config": {"url": "http://127.0.0.1:9/v1", "flavor": "other", "api_key": KEY},
        })

        assert r.status_code == 400, r.text
        assert r.json()["detail"] == "openai-compatible probe failed: ConnectError: All connection attempts failed"


class TestDraftTestRoutes:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("route,extra", [("stt_providers", {}), ("tts_providers", {"default_voice": "af_heart"})])
    async def test_the_speech_test_routes_do_not_echo_the_key(self, client, port, route, extra) -> None:
        r = await client.post(f"/v1/{route}/_test", json={
            "id": "draft", "provider": "openai", "default_model": "m", "limits": {"max_concurrency": 1}, **extra,
            "config": {"url": f"http://127.0.0.1:{port}/v1", "api_key": KEY + "\n"},
        })

        assert r.status_code == 200, r.text
        body = r.json()
        assert body["ok"] is False
        _assert_clean(r.text)
        assert "LocalProtocolError" in body["error"]

    @pytest.mark.asyncio
    @pytest.mark.parametrize("provider_type", ["exa", "firecrawl"])
    async def test_the_web_fetch_test_route_does_not_echo_the_key(self, client, provider_type) -> None:
        r = await client.post("/v1/web_fetch_providers/_test", json={
            "id": "draft", "provider_type": provider_type, "config": {"type": provider_type, "api_key": KEY + "\n"},
        })

        assert r.status_code == 200, r.text
        assert r.json()["ok"] is False
        _assert_clean(r.text)
        assert "LocalProtocolError" in r.json()["error"], f"the route must fail for the header, not vacuously: {r.json()['error']!r}"

    @pytest.mark.asyncio
    @pytest.mark.parametrize("provider_type", ["exa", "firecrawl"])
    async def test_the_web_search_test_route_does_not_echo_the_key(self, client, provider_type) -> None:
        r = await client.post("/v1/web_search_providers/_test", json={
            "id": "draft", "provider_type": provider_type, "config": {"type": provider_type, "api_key": KEY + "\n"},
        })

        assert r.status_code == 200, r.text
        assert r.json()["ok"] is False
        _assert_clean(r.text)
        assert "LocalProtocolError" in r.json()["error"], f"the route must fail for the header, not vacuously: {r.json()['error']!r}"


class TestAudioEnumeration:
    @pytest.mark.asyncio
    async def test_the_audio_pickers_probe_logs_the_failure_without_the_key(self, client, port, caplog) -> None:
        """``GET /v1/audio/models`` degrades to [] when a probe fails and logs why; the log is read by whoever reads the server's logs."""
        import logging

        created = await client.post("/v1/stt_providers", json={
            "id": "stt-ws-key", "provider": "openai", "default_model": "m", "limits": {"max_concurrency": 1},
            "config": {"url": f"http://127.0.0.1:{port}/v1", "api_key": KEY + "\n"},
        })
        assert created.status_code in (200, 201), created.text
        active = await client.put("/v1/speech_active_config", json={"stt_provider_id": "stt-ws-key", "tts_provider_id": None})
        assert active.status_code == 200, active.text

        with caplog.at_level(logging.WARNING, logger="primer.api.routers.audio"):
            r = await client.get("/v1/audio/models")

        assert r.status_code == 200 and r.json()["stt"] == [], r.text
        text = "\n".join(rec.getMessage() for rec in caplog.records)
        assert "audio enumeration probe failed" in text, text
        _assert_clean(text)
        assert "Illegal header value" in text


# ---- round 1 review: an internal newline, an upstream body that holds the key across offset 200, and unchanged messages ----------------------------


class TestInternalNewline:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("provider", ["openchat", "ollama"])
    async def test_a_key_with_a_newline_inside_it_is_not_echoed_by_the_draft_probe(self, client, port, provider) -> None:
        config = _llm_cases(port, INTERNAL)[provider]

        r = await client.post("/v1/llm_providers/_discover_models", json={"provider": provider, "config": config})

        assert r.status_code == 400, r.text
        _assert_clean(r.text)
        assert "Illegal header value" in r.json()["detail"]


@pytest_asyncio.fixture
async def http_500_port():
    """A server that answers every request with a 500 whose body holds the key from offset 190, so the old ``[:200]`` cut left its first ten characters."""
    body = ("x" * 190 + KEY).encode()

    async def handle(reader, writer):
        try:
            await reader.readuntil(b"\r\n\r\n")
            writer.write(
                b"HTTP/1.1 500 Internal Server Error\r\nContent-Type: text/plain\r\nContent-Length: " + str(len(body)).encode()
                + b"\r\nConnection: close\r\n\r\n" + body
            )
            await writer.drain()
        finally:
            writer.close()

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    try:
        yield server.sockets[0].getsockname()[1]
    finally:
        server.close()
        await server.wait_closed()


class TestUpstreamBodyAcrossTheOldCut:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("provider", ["openrouter", "anthropic", "gemini"])
    async def test_a_key_that_straddles_offset_200_of_the_body_is_masked_whole(self, client, http_500_port, monkeypatch, provider) -> None:
        """The probe cut ``exc.response.text[:200]`` BEFORE masking, so a key across offset 200 kept its first characters."""
        import primer.llm.anthropic as anthropic
        import primer.llm.gemini as gemini
        import primer.llm.openrouter as openrouter

        base = f"http://127.0.0.1:{http_500_port}"
        monkeypatch.setattr(openrouter, "OPENROUTER_BASE_URL", base + "/api/v1")
        monkeypatch.setattr(anthropic, "ANTHROPIC_BASE_URL", base + "/v1")
        monkeypatch.setattr(gemini, "GEMINI_BASE_URL", base + "/v1beta")

        r = await client.post("/v1/llm_providers/_discover_models", json={"provider": provider, "config": {"api_key": KEY}})

        assert r.status_code == 400, r.text
        assert "HTTP 500" in r.json()["detail"], "the probe must have reached the 500"
        assert "sk-live-" not in r.text and "Q7xZ9" not in r.text, f"the first characters of the key survived the cut: {r.json()['detail']!r}"


class TestMessagesWithoutACredentialAreUnchanged:
    """One assertion per route family: a refused connection says so, and nothing is masked that was not a credential."""

    @pytest.mark.asyncio
    async def test_the_draft_test_routes(self, client) -> None:
        r = await client.post("/v1/stt_providers/_test", json={
            "id": "draft", "provider": "openai", "default_model": "m", "limits": {"max_concurrency": 1},
            "config": {"url": "http://127.0.0.1:9/v1", "api_key": KEY},
        })

        assert r.status_code == 200, r.text
        error = r.json()["error"]
        assert "All connection attempts failed" in error and "[REDACTED]" not in error, error

    @pytest.mark.asyncio
    async def test_the_embedding_probe(self, client) -> None:
        r = await client.post("/v1/embedding_providers/_discover_models", json={
            "provider": "openai", "config": {"url": "http://127.0.0.1:9/v1", "flavor": "lmstudio", "api_key": KEY},
        })

        assert r.status_code == 400, r.text
        detail = r.json()["detail"]
        assert "All connection attempts failed" in detail and "[REDACTED]" not in detail, detail

    @pytest.mark.asyncio
    async def test_the_audio_pickers_log_line(self, client, caplog) -> None:
        import logging

        created = await client.post("/v1/stt_providers", json={
            "id": "stt-unchanged", "provider": "openai", "default_model": "m", "limits": {"max_concurrency": 1},
            "config": {"url": "http://127.0.0.1:9/v1", "api_key": KEY},
        })
        assert created.status_code in (200, 201), created.text
        await client.put("/v1/speech_active_config", json={"stt_provider_id": "stt-unchanged", "tts_provider_id": None})

        with caplog.at_level(logging.WARNING, logger="primer.api.routers.audio"):
            r = await client.get("/v1/audio/models")

        assert r.status_code == 200, r.text
        text = "\n".join(rec.getMessage() for rec in caplog.records)
        assert "audio enumeration probe failed" in text and "[REDACTED]" not in text, text


class TestRowsStoredBeforeTheFix:
    """Author's call (#672 review): a ``last_error`` written before the probes masked the key is scrubbed when it is SERVED, with the row's own config,
    rather than nulled by a one-off migration: the row keeps the words of the failure, a migration would have to guess which texts held a key, and the
    scrub needs nothing the row does not carry."""

    @pytest.mark.asyncio
    async def test_a_stored_last_error_that_holds_the_key_is_served_masked(self, client, app, port) -> None:
        from primer.model.provider import LLMProvider

        created = await client.post("/v1/llm_providers", json={
            "id": "legacy-last-error", "provider": "openchat", "limits": {"max_concurrency": 1},
            "config": {"url": f"http://127.0.0.1:{port}/v1", "flavor": "other", "api_key": KEY + "\n"},
        })
        assert created.status_code in (200, 201), created.text
        storage = app.state.storage_provider.get_storage(LLMProvider)
        row = await storage.get("legacy-last-error")
        raw = f"openai-compatible probe failed: LocalProtocolError: Illegal header value b'Bearer {KEY}\\n'"
        await storage.update(row.model_copy(update={"last_error": raw}))

        got = await client.get("/v1/llm_providers/legacy-last-error")

        assert got.status_code == 200, got.text
        _assert_clean(got.text)
        assert "Illegal header value" in got.json()["last_error"], "what failed is still said"

    @pytest.mark.asyncio
    async def test_a_stored_last_error_without_the_key_is_served_as_it_was(self, client, app, port) -> None:
        from primer.model.provider import LLMProvider

        await client.post("/v1/llm_providers", json={
            "id": "legacy-clean-error", "provider": "openchat", "limits": {"max_concurrency": 1},
            "config": {"url": f"http://127.0.0.1:{port}/v1", "flavor": "other", "api_key": KEY},
        })
        storage = app.state.storage_provider.get_storage(LLMProvider)
        row = await storage.get("legacy-clean-error")
        text = "openai-compatible probe failed: ConnectError: All connection attempts failed"
        await storage.update(row.model_copy(update={"last_error": text}))

        got = await client.get("/v1/llm_providers/legacy-clean-error")

        assert got.json()["last_error"] == text
