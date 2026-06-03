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


class TestTestRoute:
    @pytest.mark.asyncio
    async def test_test_endpoint_with_valid_duckduckgo_draft_returns_ok(
        self, client, monkeypatch
    ) -> None:
        from primer.web_search.duckduckgo import DuckDuckGoAdapter
        from primer.web_search.adapter import SearchHit

        async def _fake_search(self, *, query, count, safe_search):
            return [SearchHit(title="ok", url="https://example/", snippet="")]

        monkeypatch.setattr(DuckDuckGoAdapter, "search", _fake_search)

        r = await client.post(
            "/v1/web_search_providers/_test",
            json={
                "id": "ignored-draft-id",
                "provider_type": "duckduckgo",
                "config": {"type": "duckduckgo"},
            },
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["ok"] is True
        assert len(body["hits"]) == 1

    @pytest.mark.asyncio
    async def test_test_endpoint_with_tavily_bad_key_reports_error(
        self, client, monkeypatch
    ) -> None:
        from primer.web_search.tavily import TavilyAdapter
        from primer.web_search.adapter import WebSearchProviderError

        async def _fake_search(self, *, query, count, safe_search):
            raise WebSearchProviderError("tavily auth failed")

        monkeypatch.setattr(TavilyAdapter, "search", _fake_search)

        r = await client.post(
            "/v1/web_search_providers/_test",
            json={
                "id": "ignored",
                "provider_type": "tavily",
                "config": {"type": "tavily", "api_key": "bad"},
            },
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["ok"] is False
        assert "tavily auth" in body["error"]


class TestTypesRoute:
    @pytest.mark.asyncio
    async def test_types_returns_duckduckgo_and_tavily(self, client) -> None:
        r = await client.get("/v1/web_search_providers/_types")
        assert r.status_code == 200, r.text
        body = r.json()
        assert set(body.keys()) == {"duckduckgo", "tavily"}
        assert body["duckduckgo"]["config_fields"] == []
        assert body["tavily"]["config_fields"] == ["api_key"]
