"""E2E: CrossEncoderProvider /models endpoint contract pins.

T0154 (CRUD round-trip with invalidate) was pruned — the same walk
is exercised by test_full_journey_no_llm.py which creates +
lists + queries /models + deletes a CrossEncoderProvider as part
of the multi-subsystem operator journey. The remaining tests pin
behaviours specific to this provider family that the journey doesn't
assert: row-cached /models echo (T0234), invalidate/models asymmetry
on missing rows (T0235), and PUT-replaces-models propagation (T0263).
"""

from __future__ import annotations

import httpx
import pytest


# ============================================================================
# T0234 — CrossEncoderProvider /models echoes configured names
# ============================================================================


@pytest.mark.asyncio
async def test_t0234_cross_encoder_models_endpoint_is_row_cached(
    client: httpx.AsyncClient, unique_suffix: str,
) -> None:
    """T0234 — Mirror of T0025 (LLM) and T0175 (Embedder) for the
    CrossEncoder family: GET /v1/cross_encoder_providers/{id}/models
    must echo the configured model names without touching the network.
    """
    entity_id = f"ce-row-{unique_suffix}"
    body = {
        "id": entity_id,
        "provider": "huggingface",
        "models": [
            {"name": "BAAI/bge-reranker-v2-m3"},
            {"name": "cross-encoder/ms-marco-MiniLM-L-6-v2"},
        ],
        "config": {"token": None},
        "limits": {"max_concurrency": 1},
    }
    create = await client.post("/v1/cross_encoder_providers", json=body)
    assert create.status_code == 201, create.text

    try:
        resp = await client.get(
            f"/v1/cross_encoder_providers/{entity_id}/models",
            timeout=httpx.Timeout(15.0, connect=5.0),
        )
        assert resp.status_code == 200, resp.text
        body_out = resp.json()
        assert "models" in body_out, body_out
        assert sorted(body_out["models"]) == sorted([
            "BAAI/bge-reranker-v2-m3",
            "cross-encoder/ms-marco-MiniLM-L-6-v2",
        ]), body_out
    finally:
        await client.delete(f"/v1/cross_encoder_providers/{entity_id}")


# ============================================================================
# T0235 — CrossEncoderProvider /invalidate asymmetry with /models
# ============================================================================


@pytest.mark.asyncio
async def test_t0235_cross_encoder_invalidate_and_models_asymmetry(
    client: httpx.AsyncClient, unique_suffix: str,
) -> None:
    """T0235 — Mirror of T0187 + T0188 for the CrossEncoder family:
    /invalidate on a missing row is silent 204 (unconditional cache
    drop), while /models on the same missing id is 404. Pins the
    asymmetric contract is consistent across all three provider families.
    """
    missing_id = f"missing-ce-{unique_suffix}"

    inv = await client.post(
        f"/v1/cross_encoder_providers/{missing_id}/invalidate",
    )
    assert inv.status_code == 204, (
        f"invalidate on missing CE row should be 204; got "
        f"{inv.status_code}: {inv.text}"
    )
    assert inv.content == b"", inv.content

    models = await client.get(
        f"/v1/cross_encoder_providers/{missing_id}/models",
    )
    assert models.status_code == 404, models.text
    envelope = models.json()
    assert envelope["type"] == "/errors/not-found", envelope


# ============================================================================
# T0263 — PUT /v1/cross_encoder_providers/{id} replaces row; /models updates
# ============================================================================


@pytest.mark.asyncio
async def test_t0263_put_cross_encoder_replaces_models_list(
    client: httpx.AsyncClient, unique_suffix: str,
) -> None:
    """T0263 — Mirror of T0032/T0262 for the cross-encoder family.
    PUT replaces the row's `models` list; subsequent /models reflects
    the new list. Closes the shared-CRUD PUT pin across all 3
    provider families.
    """
    entity_id = f"ce-put-{unique_suffix}"
    initial = {
        "id": entity_id,
        "provider": "huggingface",
        "models": [{"name": "BAAI/bge-reranker-v2-m3"}],
        "config": {"token": None},
        "limits": {"max_concurrency": 1},
    }
    create = await client.post("/v1/cross_encoder_providers", json=initial)
    assert create.status_code == 201, create.text

    try:
        replacement = {
            "id": entity_id,
            "provider": "huggingface",
            "models": [
                {"name": "BAAI/bge-reranker-v2-m3"},
                {"name": "cross-encoder/ms-marco-MiniLM-L-6-v2"},
            ],
            "config": {"token": None},
            "limits": {"max_concurrency": 2},
        }
        put = await client.put(
            f"/v1/cross_encoder_providers/{entity_id}", json=replacement,
        )
        assert put.status_code == 200, put.text

        # Invalidate then read /models
        inv = await client.post(
            f"/v1/cross_encoder_providers/{entity_id}/invalidate",
        )
        assert inv.status_code == 204, inv.text

        models = await client.get(
            f"/v1/cross_encoder_providers/{entity_id}/models",
        )
        assert models.status_code == 200, models.text
        names = sorted(models.json()["models"])
        assert names == sorted([
            "BAAI/bge-reranker-v2-m3",
            "cross-encoder/ms-marco-MiniLM-L-6-v2",
        ]), models.json()
    finally:
        await client.delete(f"/v1/cross_encoder_providers/{entity_id}")
