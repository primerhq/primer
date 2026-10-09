"""The provider routes serve a Base URL without its password and keep it through a PUT (ticket 01a11cdf part 3, option A; lead rulings of 2026-10-09).

``config.url`` of an LLM, embedding or speech provider may carry ``user:password@`` (a reverse proxy in front of the server). ``GET``, the list and the ``POST``/``PUT`` responses used to
serve it in clear next to a masked ``api_key``. They serve ``http://svc:**********@host/v1`` now (the username stays because a password is present; a lone ``https://TOKEN@host`` is
masked whole), the stored row keeps the real URL, a full-replace ``PUT`` of the served body keeps the stored password (``preserve_masked_secrets``, as for ``api_key``), and the probes read
the stored row so they still authenticate.
"""

from __future__ import annotations

import pytest

from primer.model.providers.embedding import EmbeddingProvider
from primer.model.providers.llm import LLMProvider

MASK = "**********"
PROXY = "http://svc:s3cr3t@proxy.local:8080/v1"
MASKED = f"http://svc:{MASK}@proxy.local:8080/v1"


def _llm(url: str, row_id: str = "llm-a", max_concurrency: int = 1, api_key: str = "sk-live-abcdef") -> dict:
    return {
        "id": row_id, "provider": "openchat", "models": [{"name": "m", "context_length": 8192}],
        "config": {"url": url, "api_key": api_key, "flavor": "other"}, "limits": {"max_concurrency": max_concurrency},
    }


# ---- served masked ------------------------------------------------------------------------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_post_get_and_list_serve_the_url_without_its_password(client) -> None:
    created = await client.post("/v1/llm_providers", json=_llm(PROXY))
    assert created.status_code in (200, 201), created.text

    one = await client.get("/v1/llm_providers/llm-a")
    listed = await client.get("/v1/llm_providers")

    for r in (created, one, listed):
        assert "s3cr3t" not in r.text, r.text
    assert created.json()["config"]["url"] == MASKED
    assert one.json()["config"]["url"] == MASKED
    assert [item["config"]["url"] for item in listed.json()["items"] if item["id"] == "llm-a"] == [MASKED]


@pytest.mark.asyncio
async def test_the_stored_row_keeps_the_real_url(client, app) -> None:
    await client.post("/v1/llm_providers", json=_llm(PROXY))

    stored = await app.state.storage_provider.get_storage(LLMProvider).get("llm-a")

    assert str(stored.config.url) == PROXY


@pytest.mark.asyncio
async def test_a_url_without_userinfo_is_served_as_it_is(client) -> None:
    await client.post("/v1/llm_providers", json=_llm("http://lmstudio.local:1234/v1"))

    assert (await client.get("/v1/llm_providers/llm-a")).json()["config"]["url"] == "http://lmstudio.local:1234/v1"


@pytest.mark.asyncio
async def test_a_lone_token_in_the_username_slot_is_served_masked_whole(client) -> None:
    await client.post("/v1/llm_providers", json=_llm("https://ghp_abcdefghij@proxy.local/v1"))

    r = await client.get("/v1/llm_providers/llm-a")

    assert r.json()["config"]["url"] == f"https://{MASK}@proxy.local/v1" and "ghp_abcdefghij" not in r.text


# ---- a full-replace PUT of the served body ----------------------------------------------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_put_of_the_served_body_keeps_the_stored_password(client, app) -> None:
    await client.post("/v1/llm_providers", json=_llm(PROXY))
    served = (await client.get("/v1/llm_providers/llm-a")).json()
    served["limits"]["max_concurrency"] = 3

    r = await client.put("/v1/llm_providers/llm-a", json=served)

    assert r.status_code == 200, r.text
    stored = await app.state.storage_provider.get_storage(LLMProvider).get("llm-a")
    assert str(stored.config.url) == PROXY and stored.limits.max_concurrency == 3
    assert stored.config.api_key.get_secret_value() == "sk-live-abcdef", "the api_key beside it keeps its own rule"


@pytest.mark.asyncio
async def test_a_put_that_changes_the_host_and_leaves_the_mask_keeps_the_password(client, app) -> None:
    await client.post("/v1/llm_providers", json=_llm(PROXY))
    served = (await client.get("/v1/llm_providers/llm-a")).json()
    served["config"]["url"] = f"http://svc:{MASK}@other.local:9000/v2"

    r = await client.put("/v1/llm_providers/llm-a", json=served)

    assert r.status_code == 200, r.text
    stored = await app.state.storage_provider.get_storage(LLMProvider).get("llm-a")
    assert str(stored.config.url) == "http://svc:s3cr3t@other.local:9000/v2"


@pytest.mark.asyncio
async def test_a_put_with_a_new_password_stores_it(client, app) -> None:
    await client.post("/v1/llm_providers", json=_llm(PROXY))
    served = (await client.get("/v1/llm_providers/llm-a")).json()
    served["config"]["url"] = "http://svc:newpass@proxy.local:8080/v1"

    r = await client.put("/v1/llm_providers/llm-a", json=served)

    assert r.status_code == 200, r.text
    assert "newpass" not in r.text
    stored = await app.state.storage_provider.get_storage(LLMProvider).get("llm-a")
    assert str(stored.config.url) == "http://svc:newpass@proxy.local:8080/v1"


@pytest.mark.asyncio
async def test_a_put_that_removes_the_credential_removes_it(client, app) -> None:
    await client.post("/v1/llm_providers", json=_llm(PROXY))
    served = (await client.get("/v1/llm_providers/llm-a")).json()
    served["config"]["url"] = "http://proxy.local:8080/v1"

    await client.put("/v1/llm_providers/llm-a", json=served)

    stored = await app.state.storage_provider.get_storage(LLMProvider).get("llm-a")
    assert str(stored.config.url) == "http://proxy.local:8080/v1"


# ---- the saved-provider probe still authenticates ------------------------------------------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_saved_provider_probe_reads_the_stored_url_with_its_password(client, monkeypatch) -> None:
    await client.post("/v1/llm_providers", json=_llm(PROXY))
    seen: dict = {}

    async def _probe(config):
        seen["url"] = str(config["url"])
        return {"models": [{"name": "m"}]}

    monkeypatch.setattr("primer.api.routers.providers._probe_openai_compatible_models", _probe)

    r = await client.get("/v1/llm_providers/llm-a/discovered_models")

    assert r.status_code == 200, r.text
    assert seen["url"] == PROXY, "the probe reads the stored row, never the masked wire form"
    assert "s3cr3t" not in r.text


# ---- the other families that share the type ------------------------------------------------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_an_embedding_provider_is_served_masked_and_kept_through_a_put(client, app) -> None:
    body = {"id": "emb-a", "provider": "openai", "models": [{"name": "m"}], "config": {"url": PROXY}, "limits": {"max_concurrency": 1}}
    created = await client.post("/v1/embedding_providers", json=body)
    assert created.status_code in (200, 201), created.text
    served = (await client.get("/v1/embedding_providers/emb-a")).json()
    assert served["config"]["url"] == MASKED and "s3cr3t" not in str(served)

    served["limits"]["max_concurrency"] = 2
    r = await client.put("/v1/embedding_providers/emb-a", json=served)

    assert r.status_code == 200, r.text
    stored = await app.state.storage_provider.get_storage(EmbeddingProvider).get("emb-a")
    assert str(stored.config.url) == PROXY


@pytest.mark.asyncio
async def test_a_speech_provider_is_served_masked(client) -> None:
    body = {"id": "stt-a", "provider": "openai", "default_model": "whisper-1", "config": {"url": PROXY}, "limits": {"max_concurrency": 1}}
    created = await client.post("/v1/stt_providers", json=body)
    assert created.status_code in (200, 201), created.text

    r = await client.get("/v1/stt_providers/stt-a")

    assert r.json()["config"]["url"] == MASKED and "s3cr3t" not in r.text
