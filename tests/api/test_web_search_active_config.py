"""Singleton GET + PUT tests for /v1/web_search_active_config."""

from __future__ import annotations

import pytest


class TestSingletonGet:
    @pytest.mark.asyncio
    async def test_get_missing_returns_503(self, client, app) -> None:
        # The 503 path must hold even after Phase 7's bootstrap auto-
        # seeds the singleton: explicitly delete it before asserting,
        # so this test keeps passing once bootstrap is wired into the
        # test-app lifespan.
        from primer.model.web_search import (
            ACTIVE_WEB_SEARCH_CONFIG_ID,
            ActiveWebSearchConfig,
        )

        sp = app.state.storage_provider
        ac_storage = sp.get_storage(ActiveWebSearchConfig)
        try:
            await ac_storage.delete(ACTIVE_WEB_SEARCH_CONFIG_ID)
        except Exception:
            pass  # already missing — Phase 7 not landed yet.

        r = await client.get("/v1/web_search_active_config")
        # 503 subsystem_not_bootstrapped per spec §9.2.
        assert r.status_code == 503, r.text
        assert r.json()["extensions"]["error"] == "subsystem_not_bootstrapped"


class TestSingletonPut:
    @pytest.mark.asyncio
    async def test_put_single_mode_with_existing_provider(self, client) -> None:
        await client.post(
            "/v1/web_search_providers",
            json={
                "id": "tavily-a",
                "provider_type": "tavily",
                "config": {"type": "tavily", "api_key": "x"},
            },
        )
        r = await client.put(
            "/v1/web_search_active_config",
            json={"config": {"mode": "single", "provider_id": "tavily-a"}},
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["config"]["mode"] == "single"
        assert body["config"]["provider_id"] == "tavily-a"

    @pytest.mark.asyncio
    async def test_put_single_mode_unknown_provider_rejected(self, client) -> None:
        r = await client.put(
            "/v1/web_search_active_config",
            json={"config": {"mode": "single", "provider_id": "nope"}},
        )
        assert r.status_code == 422, r.text
        body = r.json()
        assert "nope" in body["extensions"]["unknown_ids"]

    @pytest.mark.asyncio
    async def test_put_aggregated_mode_with_existing_providers(self, client) -> None:
        await client.post(
            "/v1/web_search_providers",
            json={
                "id": "tavily-a",
                "provider_type": "tavily",
                "config": {"type": "tavily", "api_key": "x"},
            },
        )
        await client.post(
            "/v1/web_search_providers",
            json={
                "id": "tavily-b",
                "provider_type": "tavily",
                "config": {"type": "tavily", "api_key": "y"},
            },
        )
        r = await client.put(
            "/v1/web_search_active_config",
            json={
                "config": {
                    "mode": "aggregated",
                    "provider_ids": ["tavily-a", "tavily-b"],
                },
            },
        )
        assert r.status_code == 200, r.text
        assert r.json()["config"]["provider_ids"] == ["tavily-a", "tavily-b"]

    @pytest.mark.asyncio
    async def test_put_aggregated_mode_partial_unknown_rejected(self, client) -> None:
        await client.post(
            "/v1/web_search_providers",
            json={
                "id": "tavily-a",
                "provider_type": "tavily",
                "config": {"type": "tavily", "api_key": "x"},
            },
        )
        r = await client.put(
            "/v1/web_search_active_config",
            json={
                "config": {
                    "mode": "aggregated",
                    "provider_ids": ["tavily-a", "nope"],
                },
            },
        )
        assert r.status_code == 422, r.text
        assert "nope" in r.json()["extensions"]["unknown_ids"]

    @pytest.mark.asyncio
    async def test_put_empty_aggregated_rejected(self, client) -> None:
        r = await client.put(
            "/v1/web_search_active_config",
            json={"config": {"mode": "aggregated", "provider_ids": []}},
        )
        assert r.status_code == 422, r.text

    @pytest.mark.asyncio
    async def test_put_invalidates_service_cache(self, client, app) -> None:
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
            json={"config": {"mode": "single", "provider_id": "tavily-a"}},
        )
        r = await client.get("/v1/web_search_active_config")
        assert r.status_code == 200
        assert r.json()["config"]["provider_id"] == "tavily-a"
