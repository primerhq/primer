"""Tests for entity-specific endpoints on ``/v1/llm_providers``.

Currently focused on ``POST /v1/llm_providers/_discover_models`` for the
OpenRouter branch: the route validates a draft :class:`OpenRouterConfig`
then calls :func:`_discover_openrouter_models`, returning the rich
catalogue under ``{"models": [...]}``. The OpenRouter HTTP catalogue
endpoint is mocked via respx so the tests are pure in-process.
"""

from __future__ import annotations

import httpx
import pytest
import respx

from primer.llm.anthropic import ANTHROPIC_BASE_URL
from primer.llm.gemini import GEMINI_BASE_URL
from primer.llm.openrouter import OPENROUTER_BASE_URL


class TestDiscoverOpenRouter:
    @respx.mock
    @pytest.mark.asyncio
    async def test_discovers_models_with_pricing(self, client) -> None:
        respx.get(f"{OPENROUTER_BASE_URL}/models").mock(
            return_value=httpx.Response(
                200,
                json={
                    "data": [
                        {
                            "id": "anthropic/claude-3.5-sonnet",
                            "name": "Claude 3.5 Sonnet",
                            "context_length": 200000,
                            "pricing": {"prompt": "3", "completion": "15"},
                            "architecture": {"modality": "text"},
                        },
                    ],
                },
            ),
        )
        r = await client.post(
            "/v1/llm_providers/_discover_models",
            json={
                "provider": "openrouter",
                "config": {"api_key": "sk-or-v1-abc"},
            },
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert "models" in body
        assert body["models"][0]["id"] == "anthropic/claude-3.5-sonnet"
        assert body["models"][0]["context_length"] == 200000
        assert body["models"][0]["input_price_per_million"] == "3"
        assert body["models"][0]["output_price_per_million"] == "15"

    @respx.mock
    @pytest.mark.asyncio
    async def test_bad_api_key_surfaces_4xx(self, client) -> None:
        respx.get(f"{OPENROUTER_BASE_URL}/models").mock(
            return_value=httpx.Response(
                401, json={"error": {"message": "invalid api key"}},
            ),
        )
        r = await client.post(
            "/v1/llm_providers/_discover_models",
            json={
                "provider": "openrouter",
                "config": {"api_key": "sk-or-v1-bad"},
            },
        )
        assert r.status_code >= 400
        # The route translates the upstream 401 into a 4xx with the
        # OpenRouter message embedded.
        assert (
            "invalid api key" in r.text.lower()
            or "openrouter" in r.text.lower()
            or "401" in r.text
        )


class TestDiscoverAnthropic:
    @respx.mock
    @pytest.mark.asyncio
    async def test_discovers_models_live(self, client) -> None:
        respx.get(f"{ANTHROPIC_BASE_URL}/models").mock(
            return_value=httpx.Response(
                200,
                json={
                    "data": [
                        {
                            "type": "model",
                            "id": "claude-opus-4-5",
                            "display_name": "Claude Opus 4.5",
                        },
                    ],
                    "has_more": False,
                    "last_id": "claude-opus-4-5",
                },
            ),
        )
        r = await client.post(
            "/v1/llm_providers/_discover_models",
            json={
                "provider": "anthropic",
                "config": {"api_key": "sk-ant-abc"},
            },
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["models"][0]["name"] == "claude-opus-4-5"
        assert body["models"][0]["display_name"] == "Claude Opus 4.5"
        # Dogfood round 2: /v1/models exposes no context window and there
        # is no other way to learn it for Anthropic - the route must NOT
        # invent one (a seeded fake is what shipped a real user a wrong,
        # confident-looking "32k" meter denominator).
        assert not body["models"][0].get("context_length")

    @respx.mock
    @pytest.mark.asyncio
    async def test_bad_api_key_surfaces_4xx(self, client) -> None:
        respx.get(f"{ANTHROPIC_BASE_URL}/models").mock(
            return_value=httpx.Response(
                401,
                json={"error": {"type": "authentication_error",
                                "message": "invalid x-api-key"}},
            ),
        )
        r = await client.post(
            "/v1/llm_providers/_discover_models",
            json={
                "provider": "anthropic",
                "config": {"api_key": "sk-ant-bad"},
            },
        )
        assert r.status_code >= 400
        assert (
            "anthropic" in r.text.lower()
            or "401" in r.text
            or "invalid x-api-key" in r.text.lower()
        )


class TestDiscoverGemini:
    @respx.mock
    @pytest.mark.asyncio
    async def test_discovers_models_live(self, client) -> None:
        respx.get(f"{GEMINI_BASE_URL}/models").mock(
            return_value=httpx.Response(
                200,
                json={
                    "models": [
                        {
                            "name": "models/gemini-2.5-flash",
                            "displayName": "Gemini 2.5 Flash",
                            "inputTokenLimit": 1048576,
                            "supportedGenerationMethods": ["generateContent"],
                        },
                        {
                            # No inputTokenLimit -> stays unknown, not seeded.
                            "name": "models/gemini-2.5-pro",
                            "supportedGenerationMethods": ["generateContent"],
                        },
                        {
                            # Embedder dropped by the helper's filter.
                            "name": "models/text-embedding-004",
                            "supportedGenerationMethods": ["embedContent"],
                        },
                    ],
                },
            ),
        )
        r = await client.post(
            "/v1/llm_providers/_discover_models",
            json={
                "provider": "gemini",
                "config": {"api_key": "test-key-123"},
            },
        )
        assert r.status_code == 200, r.text
        body = r.json()
        names = [m["name"] for m in body["models"]]
        assert names == ["gemini-2.5-flash", "gemini-2.5-pro"]
        assert body["models"][0]["display_name"] == "Gemini 2.5 Flash"
        assert body["models"][0]["context_length"] == 1048576
        # Missing inputTokenLimit stays unknown - no seeded default.
        assert not body["models"][1].get("context_length")

    @respx.mock
    @pytest.mark.asyncio
    async def test_bad_api_key_surfaces_4xx(self, client) -> None:
        respx.get(f"{GEMINI_BASE_URL}/models").mock(
            return_value=httpx.Response(
                401,
                json={"error": {"code": 401, "message": "API key invalid"}},
            ),
        )
        r = await client.post(
            "/v1/llm_providers/_discover_models",
            json={
                "provider": "gemini",
                "config": {"api_key": "bad-key"},
            },
        )
        assert r.status_code >= 400
        assert (
            "gemini" in r.text.lower()
            or "401" in r.text
            or "invalid" in r.text.lower()
        )


class TestDiscoverOpenChat:
    """openchat is an OpenAI-compatible Chat Completions provider, so its
    /v1/models endpoint is live-discoverable via the shared probe (same
    path openresponses uses)."""

    @respx.mock
    @pytest.mark.asyncio
    async def test_discovers_models_from_v1_models(self, client) -> None:
        respx.get("http://oc-test.local/v1/models").mock(
            return_value=httpx.Response(
                200,
                json={"data": [
                    {"id": "llama-3.1-8b-instruct"},
                    {"id": "qwen2.5-coder-7b"},
                ]},
            ),
        )
        r = await client.post(
            "/v1/llm_providers/_discover_models",
            json={
                "provider": "openchat",
                "config": {"url": "http://oc-test.local/v1", "flavor": "lmstudio"},
            },
        )
        assert r.status_code == 200, r.text
        body = r.json()
        names = [m["name"] for m in body["models"]]
        assert names == ["llama-3.1-8b-instruct", "qwen2.5-coder-7b"]
        # A bare /v1/models entry (real OpenAI shape) exposes no context
        # window - stays unknown, not seeded.
        assert not body["models"][0].get("context_length")

    @respx.mock
    @pytest.mark.asyncio
    async def test_reads_a_real_context_length_when_the_server_reports_one(
        self, client,
    ) -> None:
        """Dogfood round 2: OpenAI-compatible SERVERS this probe actually
        talks to in practice (vLLM, llama.cpp server, LM Studio, text-
        generation-webui) commonly report the model's real window right
        on the /v1/models entry, under one of a few different field
        names - use it instead of guessing."""
        respx.get("http://oc-test.local/v1/models").mock(
            return_value=httpx.Response(
                200,
                json={"data": [
                    {"id": "llama-3.1-8b-instruct", "context_length": 131072},
                    {"id": "qwen2.5-coder-7b", "max_model_len": 32768},
                    {"id": "mistral-7b", "max_context_length": 8192},
                    {"id": "bare-model"},
                ]},
            ),
        )
        r = await client.post(
            "/v1/llm_providers/_discover_models",
            json={
                "provider": "openchat",
                "config": {"url": "http://oc-test.local/v1", "flavor": "lmstudio"},
            },
        )
        assert r.status_code == 200, r.text
        by_name = {m["name"]: m for m in r.json()["models"]}
        assert by_name["llama-3.1-8b-instruct"]["context_length"] == 131072
        assert by_name["qwen2.5-coder-7b"]["context_length"] == 32768
        assert by_name["mistral-7b"]["context_length"] == 8192
        assert not by_name["bare-model"].get("context_length")

    @respx.mock
    @pytest.mark.asyncio
    async def test_unreachable_surfaces_4xx(self, client) -> None:
        respx.get("http://oc-down.local/v1/models").mock(
            side_effect=httpx.ConnectError("connection refused"),
        )
        r = await client.post(
            "/v1/llm_providers/_discover_models",
            json={
                "provider": "openchat",
                "config": {"url": "http://oc-down.local/v1", "flavor": "lmstudio"},
            },
        )
        assert r.status_code >= 400
        assert "probe failed" in r.text.lower()


class TestDiscoverModelsOnASavedProvider:
    """``GET /v1/llm_providers/{id}/discovered_models``.

    The draft-config variant cannot serve the provider detail page: the row
    the console holds has its secrets redacted, so replaying that config
    would probe upstream with ``"**********"`` as the API key. This variant
    reads the stored row server-side instead.
    """

    @pytest.mark.asyncio
    async def test_unknown_provider_is_404(self, client) -> None:
        r = await client.get("/v1/llm_providers/nope/discovered_models")
        assert r.status_code == 404, r.text

    @pytest.mark.asyncio
    async def test_probes_with_the_unredacted_stored_secret(
        self, client, monkeypatch
    ) -> None:
        seen: dict = {}

        async def _fake_probe(config):
            seen.update(config)
            return {"models": [{"name": "m-1"}]}

        monkeypatch.setattr(
            "primer.api.routers.providers._probe_openai_compatible_models",
            _fake_probe,
        )
        create = await client.post(
            "/v1/llm_providers",
            json={
                "id": "saved-probe",
                "description": "probe target",
                "provider": "openresponses",
                "config": {
                    "url": "http://example.invalid",
                    "flavor": "other",
                    "api_key": "sk-real-secret",
                },
                "limits": {"max_concurrency": 1},
            },
        )
        assert create.status_code in (200, 201), create.text
        # The GET the console holds is redacted -- that is the whole reason
        # this endpoint exists.
        got = await client.get("/v1/llm_providers/saved-probe")
        assert got.json()["config"]["api_key"] != "sk-real-secret"

        r = await client.get("/v1/llm_providers/saved-probe/discovered_models")
        assert r.status_code == 200, r.text
        assert r.json()["models"][0]["name"] == "m-1"
        assert seen.get("api_key") == "sk-real-secret"

    @pytest.mark.asyncio
    async def test_never_invents_a_context_length_the_probe_cannot_know(
        self, client, monkeypatch
    ) -> None:
        """Dogfood round 2: /v1/models not reporting a context window
        must leave the field unset, not seed a plausible-looking fake -
        a ModelProfile.context_length is required, so an unknown value
        is an honest prompt for the operator to fill in on the form, not
        something this route gets to guess at."""
        async def _fake_probe(config):
            return {"models": [{"name": "m-1"}]}

        monkeypatch.setattr(
            "primer.api.routers.providers._probe_openai_compatible_models",
            _fake_probe,
        )
        await client.post(
            "/v1/llm_providers",
            json={
                "id": "saved-probe-2",
                "description": "probe target",
                "provider": "openresponses",
                "config": {"url": "http://example.invalid", "flavor": "other"},
                "limits": {"max_concurrency": 1},
            },
        )
        r = await client.get("/v1/llm_providers/saved-probe-2/discovered_models")
        assert r.status_code == 200, r.text
        assert not r.json()["models"][0].get("context_length")


class TestProbeStateStamping:
    """Platform wave P2 (#4/#5): last_probe_at/last_probe_ok/last_error
    are stamped onto the stored LLMProvider row by the one route that
    actually probes a persisted provider."""

    @pytest.mark.asyncio
    async def test_virgin_row_has_no_probe_state(self, client) -> None:
        await client.post(
            "/v1/llm_providers",
            json={
                "id": "probe-virgin",
                "provider": "openresponses",
                "config": {"url": "http://example.invalid", "flavor": "other"},
                "limits": {"max_concurrency": 1},
            },
        )
        got = await client.get("/v1/llm_providers/probe-virgin")
        assert got.json()["last_probe_at"] is None
        assert got.json()["last_probe_ok"] is False
        assert got.json()["last_error"] is None

    @pytest.mark.asyncio
    async def test_success_stamps_ok_and_at(self, client, monkeypatch) -> None:
        async def _fake_probe(config):
            return {"models": [{"name": "m-1"}]}

        monkeypatch.setattr(
            "primer.api.routers.providers._probe_openai_compatible_models",
            _fake_probe,
        )
        await client.post(
            "/v1/llm_providers",
            json={
                "id": "probe-ok",
                "provider": "openresponses",
                "config": {"url": "http://example.invalid", "flavor": "other"},
                "limits": {"max_concurrency": 1},
            },
        )
        r = await client.get("/v1/llm_providers/probe-ok/discovered_models")
        assert r.status_code == 200, r.text

        got = await client.get("/v1/llm_providers/probe-ok")
        body = got.json()
        assert body["last_probe_at"] is not None
        assert body["last_probe_ok"] is True
        assert body["last_error"] is None

    @pytest.mark.asyncio
    async def test_failure_stamps_error_and_leaves_ok_false(
        self, client, monkeypatch,
    ) -> None:
        from primer.model.except_ import BadRequestError

        async def _fake_probe(config):
            raise BadRequestError("upstream said no")

        monkeypatch.setattr(
            "primer.api.routers.providers._probe_openai_compatible_models",
            _fake_probe,
        )
        await client.post(
            "/v1/llm_providers",
            json={
                "id": "probe-fail",
                "provider": "openresponses",
                "config": {"url": "http://example.invalid", "flavor": "other"},
                "limits": {"max_concurrency": 1},
            },
        )
        r = await client.get("/v1/llm_providers/probe-fail/discovered_models")
        assert r.status_code >= 400

        got = await client.get("/v1/llm_providers/probe-fail")
        body = got.json()
        assert body["last_probe_at"] is not None
        assert body["last_probe_ok"] is False
        assert "upstream said no" in body["last_error"]

    @pytest.mark.asyncio
    async def test_a_later_success_clears_a_prior_error(
        self, client, monkeypatch,
    ) -> None:
        from primer.model.except_ import BadRequestError

        calls = {"n": 0}

        async def _flaky_probe(config):
            calls["n"] += 1
            if calls["n"] == 1:
                raise BadRequestError("first attempt failed")
            return {"models": [{"name": "m-1"}]}

        monkeypatch.setattr(
            "primer.api.routers.providers._probe_openai_compatible_models",
            _flaky_probe,
        )
        await client.post(
            "/v1/llm_providers",
            json={
                "id": "probe-recover",
                "provider": "openresponses",
                "config": {"url": "http://example.invalid", "flavor": "other"},
                "limits": {"max_concurrency": 1},
            },
        )
        first = await client.get("/v1/llm_providers/probe-recover/discovered_models")
        assert first.status_code >= 400
        second = await client.get(
            "/v1/llm_providers/probe-recover/discovered_models",
        )
        assert second.status_code == 200, second.text

        got = await client.get("/v1/llm_providers/probe-recover")
        body = got.json()
        assert body["last_probe_ok"] is True
        assert body["last_error"] is None
