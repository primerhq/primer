"""Embedding provider discovery lists a Gemini key's embedding models (platform revamp, design 13.2).

``POST /v1/embedding_providers/_discover_models`` answered 400 "live model discovery is not supported for embedding provider
'gemini'": only ``openai`` had a branch. Gemini's ListModels names the methods each model supports, so the catalogue that backs
the LLM form (``generateContent`` models) also backs the embedding form (``embedContent`` models): one probe, two methods.

Google is mocked with respx at the HTTP boundary (the key needs a real account); no key here is real. The pins that no probe
echoes a credential (the configured key, a Base URL password) are ``test_probe_errors_do_not_echo_the_key.py`` and
``test_probe_errors_do_not_echo_credentials.py``; the Gemini LLM probe is in both, and this file adds the embedding route's own.
"""

from __future__ import annotations

import httpx
import pytest
import respx

from primer.llm.gemini import GEMINI_BASE_URL

KEY = "AIza-test-KEY-9876"

# One catalogue, both kinds of model, as Google's ListModels serves them.
CATALOGUE = {
    "models": [
        {
            "name": "models/gemini-2.5-flash",
            "displayName": "Gemini 2.5 Flash",
            "supportedGenerationMethods": ["generateContent", "countTokens"],
            "inputTokenLimit": 1048576,
        },
        {
            "name": "models/gemini-embedding-001",
            "displayName": "Gemini Embedding 001",
            "supportedGenerationMethods": ["embedContent", "countTextTokens"],
            "inputTokenLimit": 2048,
        },
    ],
}


def _draft(provider: str = "gemini") -> dict:
    return {"provider": provider, "config": {"api_key": KEY}}


@pytest.mark.asyncio
@respx.mock
async def test_gemini_embedding_discovery_lists_embed_content_models(client) -> None:
    route = respx.get(f"{GEMINI_BASE_URL}/models").mock(return_value=httpx.Response(200, json=CATALOGUE))

    r = await client.post("/v1/embedding_providers/_discover_models", json=_draft())

    assert r.status_code == 200, r.text
    assert [m["name"] for m in r.json()["models"]] == ["gemini-embedding-001"]
    # The window the model reports travels with it, so page 2 of the overlay can show it.
    assert r.json()["models"][0]["context_length"] == 2048
    # The typed key authenticated the probe, in the header: httpx logs every request URL at INFO.
    request = route.calls.last.request
    assert request.headers["x-goog-api-key"] == KEY
    assert KEY not in str(request.url)


@pytest.mark.asyncio
@respx.mock
async def test_a_refused_gemini_key_is_a_400_without_the_key(client) -> None:
    respx.get(f"{GEMINI_BASE_URL}/models").mock(
        return_value=httpx.Response(401, json={"error": {"code": 401, "message": "API key not valid"}}),
    )

    r = await client.post("/v1/embedding_providers/_discover_models", json=_draft())

    assert r.status_code == 400, r.text
    assert "Gemini API key invalid or unauthorized (HTTP 401)" in r.text
    assert KEY not in r.text


# What Google answers for a bad key (its Gemini API errors page): HTTP 400, INVALID_ARGUMENT, reason API_KEY_INVALID (audit G2-19).
GOOGLE_KEY_INVALID = {"error": {
            "code": 400, "message": "API key not valid. Please pass a valid API key.", "status": "INVALID_ARGUMENT",
            "details": [{"@type": "type.googleapis.com/google.rpc.ErrorInfo", "reason": "API_KEY_INVALID", "domain": "googleapis.com",
                         "metadata": {"service": "generativelanguage.googleapis.com"}}],
        }}


@pytest.mark.asyncio
@respx.mock
async def test_googles_400_for_a_bad_key_is_the_key_message(client) -> None:
    respx.get(f"{GEMINI_BASE_URL}/models").mock(return_value=httpx.Response(400, json=GOOGLE_KEY_INVALID))

    r = await client.post("/v1/embedding_providers/_discover_models", json=_draft())

    assert r.status_code == 400, r.text
    assert "Gemini API key invalid or unauthorized (HTTP 400)" in r.json()["detail"]
    assert KEY not in r.text


@pytest.mark.asyncio
@respx.mock
async def test_another_400_is_not_read_as_a_bad_key(client) -> None:
    body = {"error": {"code": 400, "message": "Request contains an invalid argument.", "status": "INVALID_ARGUMENT",
                      "details": [{"@type": "type.googleapis.com/google.rpc.BadRequest"}]}}
    respx.get(f"{GEMINI_BASE_URL}/models").mock(return_value=httpx.Response(400, json=body))

    r = await client.post("/v1/embedding_providers/_discover_models", json=_draft())

    assert r.status_code == 400, r.text
    assert "Gemini discover failed: HTTP 400" in r.json()["detail"]
    assert "API key invalid" not in r.json()["detail"]


@pytest.mark.asyncio
@respx.mock
async def test_a_gemini_failure_that_quotes_the_key_is_masked(client) -> None:
    """The failure text carries the upstream body, which a proxy or a debugging gateway may fill with the key it was sent.

    ``_probe_failure`` masks what a URL carries; the key itself is masked by ``_reraise_masked(exc, stub)``, the wrap the
    OpenAI branch has. A Gemini branch without it would put the key in the answer.
    """
    respx.get(f"{GEMINI_BASE_URL}/models").mock(
        return_value=httpx.Response(500, text=f"upstream rejected the request carrying {KEY}"),
    )

    r = await client.post("/v1/embedding_providers/_discover_models", json=_draft())

    assert r.status_code == 400, r.text
    assert "Gemini discover failed: HTTP 500" in r.json()["detail"], "what failed is still said"
    assert KEY not in r.text


@pytest.mark.asyncio
@respx.mock
async def test_an_unreachable_gemini_is_a_400_that_says_so(client) -> None:
    respx.get(f"{GEMINI_BASE_URL}/models").mock(side_effect=httpx.ConnectError("All connection attempts failed"))

    r = await client.post("/v1/embedding_providers/_discover_models", json=_draft())

    assert r.status_code == 400, r.text
    assert "Gemini discover network error: ConnectError: All connection attempts failed" in r.json()["detail"]
    assert KEY not in r.text


@pytest.mark.asyncio
async def test_huggingface_embedding_discovery_is_still_not_offered(client) -> None:
    """Hugging Face has no model inventory to list, because the operator names its models: the Gemini branch must not swallow it."""
    r = await client.post(
        "/v1/embedding_providers/_discover_models",
        json={"provider": "huggingface", "config": {"token": "hf_test_0123456789"}},
    )

    assert r.status_code == 400, r.text
    assert "live model discovery is not supported for embedding provider 'huggingface'" in r.json()["detail"]
    assert "hf_test_0123456789" not in r.text


@pytest.mark.asyncio
@respx.mock
async def test_llm_gemini_discovery_still_lists_generate_content_models(client) -> None:
    respx.get(f"{GEMINI_BASE_URL}/models").mock(return_value=httpx.Response(200, json=CATALOGUE))

    r = await client.post("/v1/llm_providers/_discover_models", json=_draft())

    assert r.status_code == 200, r.text
    assert [m["name"] for m in r.json()["models"]] == ["gemini-2.5-flash"]
