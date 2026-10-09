"""The embedding provider routes validate the config as the class the provider names (ticket 01a11cdf part 2).

Before: ``POST /v1/embedding_providers`` accepted an ``openai`` row with no url (it validated as a Gemini config with the url dropped), and
``POST /v1/embedding_providers/_discover_models`` went on to send the raw typed string to httpx, whose ``InvalidURL: Invalid port`` text returned a password segment of a
credentialed Base URL. Now both are the 422 / 400 of the config's own validation, with the layout of the error and never its input.
"""

from __future__ import annotations

import pytest


def _row(config: dict, row_id: str = "emb-a") -> dict:
    return {"id": row_id, "provider": "openai", "models": [{"name": "text-embedding-3-small"}], "config": config, "limits": {"max_concurrency": 1}}


@pytest.mark.asyncio
async def test_an_openai_embedding_provider_with_no_url_is_refused(client) -> None:
    r = await client.post("/v1/embedding_providers", json=_row({"api_key": "k"}))

    assert r.status_code == 422, r.text
    assert "url" in r.text
    assert (await client.get("/v1/embedding_providers/emb-a")).status_code == 404, "nothing was stored"


@pytest.mark.asyncio
async def test_an_openai_embedding_provider_with_a_bad_url_is_refused(client) -> None:
    r = await client.post("/v1/embedding_providers", json=_row({"url": "not a url", "api_key": "k"}))

    assert r.status_code == 422, r.text


@pytest.mark.asyncio
async def test_a_valid_openai_embedding_provider_is_created_and_keeps_its_url(client) -> None:
    r = await client.post("/v1/embedding_providers", json=_row({"url": "http://emb.local:1234/v1", "api_key": "sk-secret-XXXX"}))

    assert r.status_code in (200, 201), r.text
    assert r.json()["config"]["url"].startswith("http://emb.local:1234/v1")
    assert "sk-secret-XXXX" not in r.text


@pytest.mark.asyncio
async def test_a_huggingface_embedding_provider_with_an_empty_config_is_created(client) -> None:
    """A public local model needs no HuggingFace token. Main never validated the config at create, so every client that posts ``config: {}`` (the e2e suite does, for
    all-MiniLM-L6-v2) must still get a 201."""
    body = {"id": "hf-local", "provider": "huggingface", "models": [{"name": "sentence-transformers/all-MiniLM-L6-v2"}], "config": {}, "limits": {"max_concurrency": 1}}

    r = await client.post("/v1/embedding_providers", json=body)

    assert r.status_code in (200, 201), r.text
    served = (await client.get("/v1/embedding_providers/hf-local")).json()
    assert served["provider"] == "huggingface" and not (served["config"] or {}).get("token")


@pytest.mark.asyncio
async def test_a_huggingface_embedding_provider_can_be_updated_to_carry_a_token_and_back(client) -> None:
    body = {"id": "hf-local", "provider": "huggingface", "models": [{"name": "m"}], "config": {}, "limits": {"max_concurrency": 1}}
    assert (await client.post("/v1/embedding_providers", json=body)).status_code in (200, 201)

    with_token = await client.put("/v1/embedding_providers/hf-local", json={**body, "config": {"token": "hf_live_0123456789"}})
    without = await client.put("/v1/embedding_providers/hf-local", json={**body, "config": {}})

    assert with_token.status_code == 200 and "hf_live_0123456789" not in with_token.text, with_token.text
    assert without.status_code == 200, without.text


@pytest.mark.asyncio
async def test_discovering_models_with_a_url_that_does_not_validate_is_a_400_that_does_not_print_the_password(client) -> None:
    r = await client.post(
        "/v1/embedding_providers/_discover_models",
        json={"provider": "openai", "config": {"url": "http://u:s3cr3t-pw@host.local:badport/v1"}},
    )

    assert r.status_code == 400, r.text
    assert "s3cr3t" not in r.text and "pw@" not in r.text, r.text
    assert "url" in r.text, "the person still needs to know which field is wrong"


@pytest.mark.asyncio
async def test_discovering_models_with_a_valid_draft_still_reaches_the_probe(client, monkeypatch) -> None:
    async def _probe(config):
        return {"models": [{"name": "text-embedding-3-small"}]}

    monkeypatch.setattr("primer.api.routers.providers._probe_openai_compatible_models", _probe)

    r = await client.post("/v1/embedding_providers/_discover_models", json={"provider": "openai", "config": {"url": "http://emb.local:1234/v1"}})

    assert r.status_code == 200, r.text
    assert r.json() == {"models": [{"name": "text-embedding-3-small"}]}
