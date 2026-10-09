"""The Base URL's password over REST with a REAL SQLite store (ticket 01a11cdf part 3, review of #691).

The other REST and tool tests keep their rows in memory, so their "stored real" checks cannot see a regression of the storage dump (a row written through the masked JSON form would
read back with the mask as its password). Here the app runs on ``SqliteStorageProvider``: a credentialed provider is created, served masked, kept through a PUT of the served body and
refused (422, row untouched) when the mask is sent back for another host, for every family that carries the field: an LLM openchat and an Ollama provider, an embedding provider, a
speech-to-text and a text-to-speech provider.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import pytest
import pytest_asyncio
from httpx import ASGITransport

from primer.api.app import create_test_app
from primer.api.registries import ProviderRegistry
from primer.model.provider import SqliteConfig
from primer.model.providers.embedding import EmbeddingProvider
from primer.model.providers.llm import LLMProvider
from primer.model.providers.speech import SpeechToTextProvider, TextToSpeechProvider
from primer.storage.sqlite import SqliteStorageProvider

MASK = "**********"
PROXY = "http://svc:s3cr3t@proxy.local:8080/v1"
MASKED = f"http://svc:{MASK}@proxy.local:8080/v1"

FAMILIES = {
    "openchat": ("llm_providers", LLMProvider, {"id": "p-1", "provider": "openchat", "models": [{"name": "m", "context_length": 8192}], "config": {"url": PROXY, "flavor": "other"}, "limits": {"max_concurrency": 1}}),
    "ollama": ("llm_providers", LLMProvider, {"id": "p-2", "provider": "ollama", "models": [{"name": "m", "context_length": 8192}], "config": {"url": PROXY}, "limits": {"max_concurrency": 1}}),
    "embedding": ("embedding_providers", EmbeddingProvider, {"id": "p-3", "provider": "openai", "models": [{"name": "m"}], "config": {"url": PROXY}, "limits": {"max_concurrency": 1}}),
    "speech-to-text": ("stt_providers", SpeechToTextProvider, {"id": "p-4", "provider": "openai", "default_model": "whisper-1", "config": {"url": PROXY}, "limits": {"max_concurrency": 1}}),
    "text-to-speech": ("tts_providers", TextToSpeechProvider, {"id": "p-5", "provider": "openai", "default_model": "tts-1", "default_voice": "alloy", "config": {"url": PROXY}, "limits": {"max_concurrency": 1}}),
}


@pytest_asyncio.fixture
async def sp(tmp_path: Path) -> AsyncIterator[SqliteStorageProvider]:
    provider = SqliteStorageProvider(SqliteConfig(path=str(tmp_path / "providers.sqlite")))
    await provider.initialize()
    try:
        yield provider
    finally:
        await provider.aclose()


@pytest_asyncio.fixture
async def client(sp: SqliteStorageProvider) -> AsyncIterator[httpx.AsyncClient]:
    registry = ProviderRegistry(
        sp,  # type: ignore[arg-type]
        llm_factory=lambda p: object(),
        embedder_factory=lambda p: object(),
        cross_encoder_factory=lambda p: object(),
        toolset_factory=lambda t: object(),
    )
    app = create_test_app(storage_provider=sp, provider_registry=registry)  # type: ignore[arg-type]
    async with httpx.AsyncClient(transport=ASGITransport(app=app, raise_app_exceptions=False), base_url="http://test") as c:
        try:
            await c.post("/v1/auth/register", json={"username": "testuser", "password": "testpassword"})
        except Exception:  # noqa: BLE001 - auth may be off in the test app
            pass
        yield c


@pytest.mark.asyncio
@pytest.mark.parametrize("family", sorted(FAMILIES))
async def test_the_row_is_served_masked_stored_real_and_kept_through_a_put(client, sp, family: str) -> None:
    path, model, body = FAMILIES[family]
    created = await client.post(f"/v1/{path}", json=body)
    assert created.status_code in (200, 201), created.text
    row_id = body["id"]

    served = (await client.get(f"/v1/{path}/{row_id}")).json()
    assert served["config"]["url"] == MASKED and "s3cr3t" not in str(served)
    assert str((await sp.get_storage(model).get(row_id)).config.url) == PROXY, "SQLite holds the real URL"

    served["limits"]["max_concurrency"] = 3
    put = await client.put(f"/v1/{path}/{row_id}", json=served)

    assert put.status_code == 200, put.text
    stored = await sp.get_storage(model).get(row_id)
    assert str(stored.config.url) == PROXY and stored.limits.max_concurrency == 3, "read back from SQLite after the PUT"


@pytest.mark.asyncio
@pytest.mark.parametrize("family", sorted(FAMILIES))
async def test_a_mask_sent_back_for_another_host_is_refused_and_the_row_is_untouched(client, sp, family: str) -> None:
    path, model, body = FAMILIES[family]
    await client.post(f"/v1/{path}", json=body)
    row_id = body["id"]
    served = (await client.get(f"/v1/{path}/{row_id}")).json()
    served["config"]["url"] = f"http://svc:{MASK}@attacker.example:9000/v2"

    r = await client.put(f"/v1/{path}/{row_id}", json=served)

    assert r.status_code == 422, r.text
    assert "re-enter the password" in r.text and "s3cr3t" not in r.text
    assert str((await sp.get_storage(model).get(row_id)).config.url) == PROXY
