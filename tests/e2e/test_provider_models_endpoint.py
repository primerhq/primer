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
