"""E2E: error-envelope guarantees per §3 of the app spec.

Covers backlog items T0007 (422 validation), T0008 (409 conflict),
T0009 (DELETE idempotency).
"""

from __future__ import annotations

import httpx
import pytest


def _toolset_body(entity_id: str) -> dict:
    return {
        "id": entity_id,
        "provider": "mcp",
        "config": {
            "transport": "stdio",
            "config": {"command": ["echo"]},
        },
    }


def _llm_body(entity_id: str) -> dict:
    return {
        "id": entity_id,
        "provider": "anthropic",
        "models": [{"name": "claude-sonnet-4-6", "context_length": 200_000}],
        "config": {"api_key": "sk-test-placeholder"},
        "limits": {"max_concurrency": 4},
    }


@pytest.mark.asyncio
async def test_t0007_invalid_llm_provider_returns_422(
    client: httpx.AsyncClient, unique_suffix: str,
) -> None:
    """T0007 — malformed config body yields 422 with /errors/validation."""
    # Provider says 'anthropic' but config shape is for a different
    # provider (missing required `api_key`, has a bogus key). This must
    # fail the discriminated-union validation.
    bad = {
        "id": f"llm-{unique_suffix}",
        "provider": "anthropic",
        "models": [{"name": "x", "context_length": 1024}],
        "config": {"wrong_field": "nope"},
        "limits": {"max_concurrency": 1},
    }
    resp = await client.post("/v1/llm_providers", json=bad)
    assert resp.status_code == 422, resp.text
    body = resp.json()
    assert body["type"] == "/errors/validation-error"
    assert body["status"] == 422


@pytest.mark.asyncio
async def test_t0008_duplicate_toolset_id_returns_409(
    client: httpx.AsyncClient, unique_suffix: str,
) -> None:
    """T0008 — POSTing the same id twice yields 409 with /errors/conflict."""
    entity_id = f"ts-dup-{unique_suffix}"
    body = _toolset_body(entity_id)
    first = await client.post("/v1/toolsets", json=body)
    assert first.status_code == 201, first.text
    try:
        dup = await client.post("/v1/toolsets", json=body)
        assert dup.status_code == 409, dup.text
        envelope = dup.json()
        assert envelope["type"] == "/errors/conflict"
        assert envelope["status"] == 409
    finally:
        await client.delete(f"/v1/toolsets/{entity_id}")


@pytest.mark.asyncio
async def test_t0009_delete_on_missing_returns_404(
    client: httpx.AsyncClient, unique_suffix: str,
) -> None:
    """T0009 — DELETE on an already-deleted row returns 404 with the
    /errors/not-found envelope.

    NB: the original backlog wording said DELETE was idempotent (returned
    204 on missing). The actual CRUD contract in matrix/api/routers/_crud.py
    is "delete, 404 on miss". This test asserts the real behaviour and
    catches any future regression that would silently broaden DELETE to
    return 204 for missing rows.
    """
    entity_id = f"llm-idem-{unique_suffix}"
    create = await client.post("/v1/llm_providers", json=_llm_body(entity_id))
    assert create.status_code == 201, create.text

    first_delete = await client.delete(f"/v1/llm_providers/{entity_id}")
    assert first_delete.status_code == 204

    second_delete = await client.delete(f"/v1/llm_providers/{entity_id}")
    assert second_delete.status_code == 404, (
        f"second DELETE on missing row: expected 404, got "
        f"{second_delete.status_code}: {second_delete.text}"
    )
    body = second_delete.json()
    assert body["type"] == "/errors/not-found"
    assert body["status"] == 404
