"""REST tests for /v1/web_search_providers — Task 6.1 covers the CRUD
half (POST/GET/PUT/DELETE/list) + reserved-id guards + cascade-block.
Tasks 6.2 and 6.3 append test classes for the singleton routes and
the _test / _types extras."""

from __future__ import annotations

import pytest


# ---------- Provider CRUD -----------------------------------------


class TestProviderCrud:
    @pytest.mark.asyncio
    async def test_list_empty(self, client) -> None:
        r = await client.get("/v1/web_search_providers?limit=10")
        assert r.status_code == 200, r.text

    @pytest.mark.asyncio
    async def test_create_tavily_redacts_api_key_in_response(self, client) -> None:
        body = {
            "id": "tavily-prod",
            "provider_type": "tavily",
            "config": {"type": "tavily", "api_key": "tvly-secret-XXXX"},
        }
        r = await client.post("/v1/web_search_providers", json=body)
        assert r.status_code in (200, 201), r.text
        data = r.json()
        # SecretStr default redaction.
        assert "tvly-secret-XXXX" not in r.text
        assert data["config"]["api_key"] != "tvly-secret-XXXX"

    @pytest.mark.asyncio
    async def test_create_reserved_id_rejected(self, client) -> None:
        body = {
            "id": "DuckDuckGo",
            "provider_type": "duckduckgo",
            "config": {"type": "duckduckgo"},
        }
        r = await client.post("/v1/web_search_providers", json=body)
        assert r.status_code == 409, r.text

    @pytest.mark.asyncio
    async def test_create_provider_type_config_mismatch_rejected(self, client) -> None:
        body = {
            "id": "broken",
            "provider_type": "tavily",
            "config": {"type": "duckduckgo"},
        }
        r = await client.post("/v1/web_search_providers", json=body)
        assert r.status_code == 422, r.text

    @pytest.mark.asyncio
    async def test_delete_reserved_id_rejected(self, client) -> None:
        r = await client.delete("/v1/web_search_providers/DuckDuckGo")
        assert r.status_code == 403, r.text


@pytest.mark.skip(reason="depends on Task 6.2's singleton PUT route")
class TestCascadeBlockOnDelete:
    @pytest.mark.asyncio
    async def test_delete_blocked_when_referenced_by_active_config(
        self, client, app
    ) -> None:
        await client.post(
            "/v1/web_search_providers",
            json={
                "id": "tavily-a",
                "provider_type": "tavily",
                "config": {"type": "tavily", "api_key": "x"},
            },
        )
        await client.put(
            "/v1/web_search_active_config",
            json={
                "config": {"mode": "single", "provider_id": "tavily-a"},
            },
        )
        r = await client.delete("/v1/web_search_providers/tavily-a")
        assert r.status_code == 409, r.text
        body = r.json()
        assert body["detail"]["error"] == "cascade_blocked"
        assert body["detail"]["referenced_by"] == "_active_web_search_config"
