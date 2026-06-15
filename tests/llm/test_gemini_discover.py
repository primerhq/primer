"""Unit tests for the Gemini live model-discovery helper.

Covers :func:`primer.llm.gemini._discover_gemini_models`, which backs
the console's *Fetch models* button. All HTTP is mocked via ``respx``;
no real Gemini API is contacted and no key is hardcoded as a secret to
be sent anywhere real.
"""

from __future__ import annotations

import httpx
import pytest
import respx
from pydantic import SecretStr

from primer.llm.gemini import GEMINI_BASE_URL, _discover_gemini_models
from primer.model.provider import GoogleConfig


class TestDiscoverGeminiModels:
    """``_discover_gemini_models`` live-probes ``GET /v1beta/models``."""

    @respx.mock
    async def test_strips_prefix_filters_methods_and_maps_fields(self) -> None:
        route = respx.get(f"{GEMINI_BASE_URL}/models").mock(
            return_value=httpx.Response(
                200,
                json={
                    "models": [
                        {
                            "name": "models/gemini-2.5-flash",
                            "displayName": "Gemini 2.5 Flash",
                            "inputTokenLimit": 1048576,
                            "outputTokenLimit": 8192,
                            "supportedGenerationMethods": [
                                "generateContent",
                                "countTokens",
                            ],
                        },
                        {
                            # Embedder: no generateContent -> dropped.
                            "name": "models/text-embedding-004",
                            "displayName": "Text Embedding 004",
                            "inputTokenLimit": 2048,
                            "supportedGenerationMethods": [
                                "embedContent",
                            ],
                        },
                        {
                            # No inputTokenLimit -> context_length omitted.
                            "name": "models/gemini-2.5-pro",
                            "supportedGenerationMethods": ["generateContent"],
                        },
                    ],
                },
            ),
        )

        out = await _discover_gemini_models(
            GoogleConfig(api_key=SecretStr("test-key-123")),
        )

        # Only generateContent models survive; prefix stripped.
        assert [m["name"] for m in out] == [
            "gemini-2.5-flash",
            "gemini-2.5-pro",
        ]
        # Field mapping for the fully-populated model.
        assert out[0]["display_name"] == "Gemini 2.5 Flash"
        assert out[0]["context_length"] == 1048576
        # display_name falls back to the bare id; no context_length key.
        assert out[1]["display_name"] == "gemini-2.5-pro"
        assert "context_length" not in out[1]

        # The probe hits /v1beta/models with the key query param.
        req = route.calls.last.request
        assert req.url.path == "/v1beta/models"
        assert req.url.params["key"] == "test-key-123"

    @respx.mock
    async def test_follows_next_page_token(self) -> None:
        responses = [
            httpx.Response(
                200,
                json={
                    "models": [
                        {
                            "name": "models/gemini-2.5-flash",
                            "supportedGenerationMethods": ["generateContent"],
                        },
                    ],
                    "nextPageToken": "PAGE2",
                },
            ),
            httpx.Response(
                200,
                json={
                    "models": [
                        {
                            "name": "models/gemini-2.5-pro",
                            "supportedGenerationMethods": ["generateContent"],
                        },
                    ],
                },
            ),
        ]
        route = respx.get(f"{GEMINI_BASE_URL}/models").mock(
            side_effect=responses,
        )

        out = await _discover_gemini_models(
            GoogleConfig(api_key=SecretStr("test-key-123")),
        )

        assert [m["name"] for m in out] == [
            "gemini-2.5-flash",
            "gemini-2.5-pro",
        ]
        assert len(route.calls) == 2
        assert "pageToken=PAGE2" in str(route.calls[1].request.url)

    @respx.mock
    async def test_dedupes_by_id(self) -> None:
        respx.get(f"{GEMINI_BASE_URL}/models").mock(
            return_value=httpx.Response(
                200,
                json={
                    "models": [
                        {
                            "name": "models/gemini-2.5-flash",
                            "supportedGenerationMethods": ["generateContent"],
                        },
                        {
                            "name": "models/gemini-2.5-flash",
                            "supportedGenerationMethods": ["generateContent"],
                        },
                    ],
                },
            ),
        )

        out = await _discover_gemini_models(
            GoogleConfig(api_key=SecretStr("test-key-123")),
        )

        assert [m["name"] for m in out] == ["gemini-2.5-flash"]

    @respx.mock
    async def test_no_api_key_sends_empty_key(self) -> None:
        route = respx.get(f"{GEMINI_BASE_URL}/models").mock(
            return_value=httpx.Response(200, json={"models": []}),
        )

        out = await _discover_gemini_models(GoogleConfig())

        assert out == []
        assert route.calls.last.request.url.params["key"] == ""

    @respx.mock
    async def test_401_raises_http_status_error(self) -> None:
        respx.get(f"{GEMINI_BASE_URL}/models").mock(
            return_value=httpx.Response(
                401,
                json={"error": {"code": 401, "message": "API key invalid"}},
            ),
        )

        with pytest.raises(httpx.HTTPStatusError) as exc_info:
            await _discover_gemini_models(
                GoogleConfig(api_key=SecretStr("bad-key")),
            )

        assert exc_info.value.response.status_code == 401
