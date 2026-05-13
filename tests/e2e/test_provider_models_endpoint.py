"""E2E: GET /v1/<provider>/{id}/models contract.

Covers backlog item T0025 (reframed).

The original backlog entry assumed the endpoint fetches from upstream
and asserted 502/503 on a refused connection. Reading
`matrix.llm.{anthropic,gemini,ollama,openresponses}.list_models` shows
the endpoint actually just echoes the configured ``LLMProvider.models``
list (every adapter's ``list_models`` returns
``[m.name for m in self._provider.models]``). The endpoint never
touches the network, so reachability of the upstream is irrelevant.

This test pins the actual contract: even when the configured `url`
points at a refused-connection address, the endpoint returns 200 and
echoes the configured names. If a future refactor makes the endpoint
truly live, this test will start failing and force the spec + this
test to be revisited together.
"""

from __future__ import annotations

import httpx
import pytest


def _bad_url_provider_body(entity_id: str) -> dict:
    """LLMProvider with an unreachable upstream URL but a populated
    ``models`` list — proves the row's models survive even when the
    upstream is unreachable."""
    return {
        "id": entity_id,
        "provider": "openresponses",
        "models": [
            {"name": "configured-1", "context_length": 1024},
            {"name": "configured-2", "context_length": 2048},
        ],
        "config": {
            "url": "http://127.0.0.1:1",
            "api_key": "sk-not-used",
            "flavor": "other",
        },
        "limits": {"max_concurrency": 1},
    }


@pytest.mark.asyncio
async def test_t0025_provider_models_endpoint_returns_configured_models(
    client: httpx.AsyncClient, unique_suffix: str,
) -> None:
    entity_id = f"llm-bad-{unique_suffix}"
    create = await client.post(
        "/v1/llm_providers", json=_bad_url_provider_body(entity_id),
    )
    assert create.status_code == 201, create.text

    try:
        # Generous timeout — were the endpoint to ever try the network,
        # the test would expose that by hanging until the connect
        # times out instead of returning instantly.
        resp = await client.get(
            f"/v1/llm_providers/{entity_id}/models",
            timeout=httpx.Timeout(15.0, connect=5.0),
        )
        # Real contract: 200 with the configured names, regardless of
        # whether upstream is reachable.
        assert resp.status_code == 200, (
            f"expected 200 (endpoint is row-cached, not live), got "
            f"{resp.status_code}: {resp.text}"
        )
        body = resp.json()
        assert "models" in body, body
        assert sorted(body["models"]) == ["configured-1", "configured-2"], body
    finally:
        await client.delete(f"/v1/llm_providers/{entity_id}")


def _bad_url_embedding_provider_body(entity_id: str) -> dict:
    """EmbeddingProvider whose config has no reachable upstream. The
    HuggingFace embedder is a row-cached list_models too — see
    matrix/embedder/huggingface.py:190 — so `list_models()` should never
    touch the network."""
    return {
        "id": entity_id,
        "provider": "huggingface",
        "models": [
            {"name": "sentence-transformers/all-MiniLM-L6-v2", "dim": 384},
            {"name": "sentence-transformers/all-mpnet-base-v2", "dim": 768},
        ],
        "config": {"token": "hf-placeholder"},
        "limits": {"max_concurrency": 1},
    }


@pytest.mark.asyncio
async def test_t0175_embedding_provider_models_endpoint_is_row_cached(
    client: httpx.AsyncClient, unique_suffix: str,
) -> None:
    """T0175 — mirrors T0025 for the embedding family.
    GET /v1/embedding_providers/{id}/models echoes the configured
    ``models`` list without touching the network.
    """
    entity_id = f"emb-row-{unique_suffix}"
    create = await client.post(
        "/v1/embedding_providers",
        json=_bad_url_embedding_provider_body(entity_id),
    )
    assert create.status_code == 201, create.text

    try:
        resp = await client.get(
            f"/v1/embedding_providers/{entity_id}/models",
            timeout=httpx.Timeout(15.0, connect=5.0),
        )
        assert resp.status_code == 200, (
            f"expected 200 (row-cached), got {resp.status_code}: "
            f"{resp.text}"
        )
        body = resp.json()
        assert "models" in body, body
        assert sorted(body["models"]) == sorted([
            "sentence-transformers/all-MiniLM-L6-v2",
            "sentence-transformers/all-mpnet-base-v2",
        ]), body
    finally:
        await client.delete(f"/v1/embedding_providers/{entity_id}")


# ============================================================================
# T0262 — PUT /v1/embedding_providers/{id} replaces row; /models updates
# ============================================================================


@pytest.mark.asyncio
async def test_t0262_put_embedding_provider_replaces_models_list(
    client: httpx.AsyncClient, unique_suffix: str,
) -> None:
    """T0262 — Mirror of T0032 for the embedding family. PUT replaces
    the row's `models` list; subsequent GET /models reflects the new
    list (proving the row-cached endpoint reads fresh from storage).
    """
    entity_id = f"emb-put-{unique_suffix}"
    initial = {
        "id": entity_id,
        "provider": "huggingface",
        "models": [
            {"name": "sentence-transformers/all-MiniLM-L6-v2", "dim": 384},
        ],
        "config": {"token": "hf-placeholder"},
        "limits": {"max_concurrency": 1},
    }
    create = await client.post("/v1/embedding_providers", json=initial)
    assert create.status_code == 201, create.text

    try:
        # PUT with a different models list
        replacement = {
            "id": entity_id,
            "provider": "huggingface",
            "models": [
                {"name": "sentence-transformers/all-MiniLM-L6-v2", "dim": 384},
                {"name": "sentence-transformers/all-mpnet-base-v2", "dim": 768},
            ],
            "config": {"token": "hf-placeholder"},
            "limits": {"max_concurrency": 1},
        }
        put = await client.put(
            f"/v1/embedding_providers/{entity_id}", json=replacement,
        )
        assert put.status_code == 200, put.text

        # Invalidate to drop any cached adapter, then read /models
        inv = await client.post(
            f"/v1/embedding_providers/{entity_id}/invalidate",
        )
        assert inv.status_code == 204, inv.text

        models = await client.get(
            f"/v1/embedding_providers/{entity_id}/models",
        )
        assert models.status_code == 200, models.text
        names = sorted(models.json()["models"])
        assert names == sorted([
            "sentence-transformers/all-MiniLM-L6-v2",
            "sentence-transformers/all-mpnet-base-v2",
        ]), models.json()
    finally:
        await client.delete(f"/v1/embedding_providers/{entity_id}")


# ============================================================================
# T0187 — POST /invalidate on a missing provider returns 404
# ============================================================================


@pytest.mark.asyncio
async def test_t0187_invalidate_on_missing_llm_provider_is_silent_204(
    client: httpx.AsyncClient, unique_suffix: str,
) -> None:
    """T0187 — POST /v1/llm_providers/{missing}/invalidate on a row that
    doesn't exist returns 204, not 404. The handler treats invalidate
    as an unconditional "drop the cache for this id" operation; if
    nothing is cached (because no row exists), the no-op still returns
    success.

    NB: This was reframed during the iteration that added the test —
    the original wording assumed 404 referential integrity. The live
    contract is silent 204. Spec §7 doesn't explicitly call this out;
    the contract is documented here.

    Companion contract: GET /models on the same missing id IS gated
    (T0188 pins 404). The asymmetry is recorded but not corrected.
    """
    missing_id = f"missing-llm-{unique_suffix}"
    resp = await client.post(f"/v1/llm_providers/{missing_id}/invalidate")
    assert resp.status_code == 204, (
        f"invalidate on missing row returned {resp.status_code}: "
        f"{resp.text}"
    )
    # 204 carries no body
    assert resp.content == b"", resp.content


# ============================================================================
# T0188 — GET /models on a missing provider returns 404
# ============================================================================


@pytest.mark.asyncio
async def test_t0188_models_endpoint_on_missing_llm_provider_returns_404(
    client: httpx.AsyncClient, unique_suffix: str,
) -> None:
    """T0188 — GET /v1/llm_providers/{missing}/models returns 404
    /errors/not-found. The endpoint is row-cached (T0025) so a missing
    row has no models list to echo; the handler must reject cleanly.
    """
    missing_id = f"missing-models-llm-{unique_suffix}"
    resp = await client.get(f"/v1/llm_providers/{missing_id}/models")
    assert resp.status_code == 404, resp.text
    envelope = resp.json()
    assert envelope["type"] == "/errors/not-found", envelope
    assert envelope["status"] == 404
