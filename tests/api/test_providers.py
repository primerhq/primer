"""End-to-end tests for the four Phase-1 provider routers.

Covers the standard CRUD + Find loop for one entity (LLMProvider) in
detail; the other three providers (EmbeddingProvider,
CrossEncoderProvider, Toolset) get smoke tests since they share the
same router factory.

Also covers entity-specific endpoints:
* ``GET    /v1/<provider>/{id}/models``  -- live model list
* ``POST   /v1/<provider>/{id}/invalidate``
* ``GET    /v1/toolsets/{id}/tools``     -- live tool list
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from pydantic import SecretStr

from matrix.api.registries import ProviderRegistry
from matrix.model.provider import (
    AnthropicConfig,
    CrossEncoderModel,
    CrossEncoderProvider,
    CrossEncoderProviderType,
    EmbeddingModel,
    EmbeddingProvider,
    EmbeddingProviderType,
    HuggingFaceConfig,
    HuggingFaceCrossEncoderConfig,
    Limits,
    LLMModel,
    LLMProvider,
    LLMProviderType,
    McpConfig,
    StdioConfig,
    Toolset,
    ToolsetProviderType,
    TransportType,
)


def _llm() -> LLMProvider:
    return LLMProvider(
        id="anthropic-1",
        provider=LLMProviderType.ANTHROPIC,
        models=[LLMModel(name="claude-sonnet-4-6", context_length=200_000)],
        config=AnthropicConfig(api_key=SecretStr("sk-x")),
        limits=Limits(max_concurrency=4),
    )


def _embedding() -> EmbeddingProvider:
    return EmbeddingProvider(
        id="hf-1",
        provider=EmbeddingProviderType.HUGGINGFACE,
        models=[EmbeddingModel(name="sentence-transformers/all-MiniLM-L6-v2")],
        config=HuggingFaceConfig(token=SecretStr("hf_x")),
        limits=Limits(max_concurrency=2),
    )


def _cross_encoder() -> CrossEncoderProvider:
    return CrossEncoderProvider(
        id="ce-1",
        provider=CrossEncoderProviderType.HUGGINGFACE,
        models=[CrossEncoderModel(name="BAAI/bge-reranker-v2-m3")],
        config=HuggingFaceCrossEncoderConfig(token=None),
        limits=Limits(max_concurrency=2),
    )


def _toolset() -> Toolset:
    return Toolset(
        id="ts-1",
        provider=ToolsetProviderType.MCP,
        config=McpConfig(
            transport=TransportType.STDIO,
            config=StdioConfig(command=["echo"]),
        ),
    )


# ===========================================================================
# CRUD + Find — exercised against LLMProvider; the same router factory
# powers the other three so they only get smoke checks below.
# ===========================================================================


class TestLLMProviderCRUD:
    @pytest.mark.asyncio
    async def test_create_then_get_round_trip(self, client) -> None:
        body = _llm().model_dump(mode="json")
        resp = await client.post("/v1/llm_providers", json=body)
        assert resp.status_code == 201, resp.text
        assert resp.json()["id"] == "anthropic-1"

        get = await client.get("/v1/llm_providers/anthropic-1")
        assert get.status_code == 200
        assert get.json()["id"] == "anthropic-1"

    @pytest.mark.asyncio
    async def test_create_duplicate_returns_409_conflict(self, client) -> None:
        body = _llm().model_dump(mode="json")
        await client.post("/v1/llm_providers", json=body)
        dup = await client.post("/v1/llm_providers", json=body)
        assert dup.status_code == 409
        assert dup.json()["type"] == "/errors/conflict"

    @pytest.mark.asyncio
    async def test_get_unknown_returns_404(self, client) -> None:
        resp = await client.get("/v1/llm_providers/missing")
        assert resp.status_code == 404
        assert resp.json()["type"] == "/errors/not-found"

    @pytest.mark.asyncio
    async def test_put_updates_when_path_id_matches(self, client) -> None:
        body = _llm().model_dump(mode="json")
        await client.post("/v1/llm_providers", json=body)
        body["limits"]["max_concurrency"] = 8
        put = await client.put("/v1/llm_providers/anthropic-1", json=body)
        assert put.status_code == 200
        assert put.json()["limits"]["max_concurrency"] == 8

    @pytest.mark.asyncio
    async def test_put_with_mismatched_id_returns_409(self, client) -> None:
        body = _llm().model_dump(mode="json")
        await client.post("/v1/llm_providers", json=body)
        put = await client.put("/v1/llm_providers/different-id", json=body)
        assert put.status_code == 409

    @pytest.mark.asyncio
    async def test_put_unknown_returns_404(self, client) -> None:
        body = _llm().model_dump(mode="json")
        put = await client.put("/v1/llm_providers/anthropic-1", json=body)
        assert put.status_code == 404

    @pytest.mark.asyncio
    async def test_delete_then_get_returns_404(self, client) -> None:
        body = _llm().model_dump(mode="json")
        await client.post("/v1/llm_providers", json=body)
        delete = await client.delete("/v1/llm_providers/anthropic-1")
        assert delete.status_code == 204
        get = await client.get("/v1/llm_providers/anthropic-1")
        assert get.status_code == 404

    @pytest.mark.asyncio
    async def test_delete_unknown_returns_404(self, client) -> None:
        resp = await client.delete("/v1/llm_providers/missing")
        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_list_paginates(self, client) -> None:
        for i in range(3):
            body = _llm().model_dump(mode="json")
            body["id"] = f"row-{i}"
            await client.post("/v1/llm_providers", json=body)
        listed = await client.get("/v1/llm_providers?limit=2&offset=0")
        assert listed.status_code == 200
        page = listed.json()
        assert page["kind"] == "offset"
        assert page["length"] == 2
        assert page["total"] == 3

    @pytest.mark.asyncio
    async def test_find_returns_offset_page(self, client) -> None:
        body = _llm().model_dump(mode="json")
        await client.post("/v1/llm_providers", json=body)
        find = await client.post(
            "/v1/llm_providers/find",
            json={"page": {"kind": "offset", "offset": 0, "length": 20}},
        )
        assert find.status_code == 200
        assert find.json()["length"] == 1


# ===========================================================================
# Cascade invalidation — PUT/DELETE drop the cached adapter.
# ===========================================================================


class TestCascadeInvalidation:
    @pytest.mark.asyncio
    async def test_put_invalidates_cached_llm(
        self, client, fake_provider_registry
    ) -> None:
        body = _llm().model_dump(mode="json")
        await client.post("/v1/llm_providers", json=body)
        registry: ProviderRegistry = fake_provider_registry

        sentinel_v1 = MagicMock()
        sentinel_v1.aclose = AsyncMock()
        sentinel_v2 = MagicMock()
        sentinel_v2.aclose = AsyncMock()
        registry._llm_factory = lambda _p: sentinel_v1  # type: ignore[attr-defined]
        first = await registry.get_llm("anthropic-1")
        assert first is sentinel_v1

        registry._llm_factory = lambda _p: sentinel_v2  # type: ignore[attr-defined]
        body["limits"]["max_concurrency"] = 8
        put = await client.put("/v1/llm_providers/anthropic-1", json=body)
        assert put.status_code == 200

        second = await registry.get_llm("anthropic-1")
        assert second is sentinel_v2
        sentinel_v1.aclose.assert_awaited_once()


# ===========================================================================
# /models live-fetch endpoint
# ===========================================================================


class TestLiveModelsEndpoint:
    @pytest.mark.asyncio
    async def test_returns_models_from_adapter(
        self, client, fake_provider_registry
    ) -> None:
        body = _llm().model_dump(mode="json")
        await client.post("/v1/llm_providers", json=body)

        adapter = MagicMock()
        adapter.list_models = AsyncMock(return_value=["claude-sonnet-4-6", "haiku-4"])
        adapter.aclose = AsyncMock()
        fake_provider_registry._llm_factory = lambda _p: adapter  # type: ignore[attr-defined]

        resp = await client.get("/v1/llm_providers/anthropic-1/models")
        assert resp.status_code == 200
        assert resp.json() == {"models": ["claude-sonnet-4-6", "haiku-4"]}

    @pytest.mark.asyncio
    async def test_404_when_provider_missing(self, client) -> None:
        resp = await client.get("/v1/llm_providers/missing/models")
        assert resp.status_code == 404


# ===========================================================================
# Smoke tests for the other three provider routers (CRUD + invalidate).
# ===========================================================================


class TestEmbeddingProviderSmoke:
    @pytest.mark.asyncio
    async def test_crud_round_trip(self, client) -> None:
        body = _embedding().model_dump(mode="json")
        assert (await client.post("/v1/embedding_providers", json=body)).status_code == 201
        assert (await client.get("/v1/embedding_providers/hf-1")).status_code == 200
        assert (
            await client.delete("/v1/embedding_providers/hf-1")
        ).status_code == 204

    @pytest.mark.asyncio
    async def test_explicit_invalidate(self, client) -> None:
        body = _embedding().model_dump(mode="json")
        await client.post("/v1/embedding_providers", json=body)
        resp = await client.post("/v1/embedding_providers/hf-1/invalidate")
        assert resp.status_code == 204


class TestCrossEncoderProviderSmoke:
    @pytest.mark.asyncio
    async def test_crud_round_trip(self, client) -> None:
        body = _cross_encoder().model_dump(mode="json")
        assert (
            await client.post("/v1/cross_encoder_providers", json=body)
        ).status_code == 201
        assert (
            await client.get("/v1/cross_encoder_providers/ce-1")
        ).status_code == 200
        assert (
            await client.delete("/v1/cross_encoder_providers/ce-1")
        ).status_code == 204


class TestToolsetSmoke:
    @pytest.mark.asyncio
    async def test_crud_round_trip(self, client) -> None:
        body = _toolset().model_dump(mode="json")
        assert (await client.post("/v1/toolsets", json=body)).status_code == 201
        assert (await client.get("/v1/toolsets/ts-1")).status_code == 200
        assert (await client.delete("/v1/toolsets/ts-1")).status_code == 204

    @pytest.mark.asyncio
    async def test_list_tools_returns_tool_descriptors(
        self, client, fake_provider_registry
    ) -> None:
        body = _toolset().model_dump(mode="json")
        await client.post("/v1/toolsets", json=body)

        async def _gen(*, principal=None):
            for tn in ("foo", "bar"):
                tool = MagicMock()
                tool.model_dump = MagicMock(return_value={"id": tn})
                yield tool

        provider_mock = MagicMock()
        provider_mock.list_tools = _gen
        provider_mock.aclose = AsyncMock()
        fake_provider_registry._toolset_factory = lambda _t: provider_mock  # type: ignore[attr-defined]

        resp = await client.get("/v1/toolsets/ts-1/tools")
        assert resp.status_code == 200
        assert resp.json() == {"tools": [{"id": "foo"}, {"id": "bar"}]}
