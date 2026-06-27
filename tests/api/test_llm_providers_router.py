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
        # /v1/models exposes no context window; the route seeds a default.
        assert body["models"][0]["context_length"] > 0

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
                            # No inputTokenLimit -> route seeds default.
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
        # Missing inputTokenLimit gets the seeded default.
        assert body["models"][1]["context_length"] > 0

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
        # /v1/models exposes no context window; the route seeds a default.
        assert body["models"][0]["context_length"] > 0

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
