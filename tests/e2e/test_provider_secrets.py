"""E2E: provider secret-handling invariant.

Covers backlog item T0027 — spec §7 says API keys round-trip via
``dump_for_storage`` for the storage path but are NEVER echoed back
to API consumers. The GET response's config.api_key must therefore
be either absent, null, or a fixed mask — never the plaintext.
"""

from __future__ import annotations

import httpx
import pytest


_PLAINTEXT_KEY = "sk-test-PLAINTEXT-MUST-NEVER-LEAK"


def _llm_body(entity_id: str) -> dict:
    return {
        "id": entity_id,
        "provider": "anthropic",
        "models": [{"name": "claude-sonnet-4-6", "context_length": 200_000}],
        "config": {"api_key": _PLAINTEXT_KEY},
        "limits": {"max_concurrency": 1},
    }


def _find_secret_leak(node: object) -> str | None:
    """Walk a JSON tree and return the first string equal to the
    plaintext key, or ``None`` if it does not appear anywhere."""
    if isinstance(node, str):
        return node if node == _PLAINTEXT_KEY else None
    if isinstance(node, dict):
        for value in node.values():
            hit = _find_secret_leak(value)
            if hit is not None:
                return hit
        return None
    if isinstance(node, list):
        for item in node:
            hit = _find_secret_leak(item)
            if hit is not None:
                return hit
        return None
    return None


@pytest.mark.asyncio
async def test_t0027_provider_secrets_never_echoed(
    client: httpx.AsyncClient, unique_suffix: str,
) -> None:
    entity_id = f"llm-secret-{unique_suffix}"
    body = _llm_body(entity_id)
    create = await client.post("/v1/llm_providers", json=body)
    assert create.status_code == 201, create.text

    try:
        # The CREATE response itself must not echo the plaintext.
        leak = _find_secret_leak(create.json())
        assert leak is None, (
            f"plaintext api_key leaked through CREATE response: {leak!r}"
        )

        got = await client.get(f"/v1/llm_providers/{entity_id}")
        assert got.status_code == 200, got.text
        body_out = got.json()
        leak = _find_secret_leak(body_out)
        assert leak is None, (
            f"plaintext api_key leaked through GET response: {leak!r}"
        )

        # Sanity: the api_key field must STILL appear (we don't want it
        # silently stripped — that would also be a bug). It just must
        # not equal the plaintext.
        api_key = body_out["config"].get("api_key")
        assert api_key is not None, (
            f"config.api_key field missing from GET response: {body_out!r}"
        )
        assert api_key != _PLAINTEXT_KEY

        # The same invariant must hold across the list endpoint.
        listed = await client.get("/v1/llm_providers?limit=200&offset=0")
        assert listed.status_code == 200
        leak = _find_secret_leak(listed.json())
        assert leak is None, (
            f"plaintext api_key leaked through LIST response: {leak!r}"
        )
    finally:
        await client.delete(f"/v1/llm_providers/{entity_id}")
