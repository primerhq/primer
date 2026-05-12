"""Unit tests for matrix.api.registries.provider_registry.ProviderRegistry."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from pydantic import SecretStr

from matrix.api.registries.provider_registry import ProviderRegistry
from matrix.model.except_ import ConfigError, NotFoundError
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
)


class _FakeStorage:
    def __init__(self) -> None:
        self._data: dict[str, Any] = {}

    async def get(self, id: str):
        return self._data.get(id)

    async def create(self, entity):
        self._data[entity.id] = entity
        return entity

    async def update(self, entity):
        self._data[entity.id] = entity
        return entity

    async def delete(self, id: str) -> None:
        self._data.pop(id, None)


class _FakeStorageProvider:
    def __init__(self) -> None:
        self._stores: dict[type, _FakeStorage] = {}

    def get_storage(self, model_class: type) -> _FakeStorage:
        return self._stores.setdefault(model_class, _FakeStorage())

    async def initialize(self) -> None:
        return

    async def aclose(self) -> None:
        return


def _make_llm_provider() -> LLMProvider:
    return LLMProvider(
        id="anthropic-1",
        provider=LLMProviderType.ANTHROPIC,
        models=[LLMModel(name="claude-sonnet-4-6", context_length=200_000)],
        config=AnthropicConfig(api_key=SecretStr("sk-x")),
        limits=Limits(max_concurrency=4),
    )


def _make_embedding_provider() -> EmbeddingProvider:
    return EmbeddingProvider(
        id="hf-1",
        provider=EmbeddingProviderType.HUGGINGFACE,
        models=[EmbeddingModel(name="sentence-transformers/all-MiniLM-L6-v2")],
        config=HuggingFaceConfig(token=SecretStr("hf_x")),
        limits=Limits(max_concurrency=2),
    )


def _make_cross_encoder_provider() -> CrossEncoderProvider:
    return CrossEncoderProvider(
        id="ce-1",
        provider=CrossEncoderProviderType.HUGGINGFACE,
        models=[CrossEncoderModel(name="BAAI/bge-reranker-v2-m3")],
        config=HuggingFaceCrossEncoderConfig(token=SecretStr("hf_x")),
        limits=Limits(max_concurrency=2),
    )


class TestLLMResolution:
    @pytest.mark.asyncio
    async def test_lookup_constructs_and_caches(self) -> None:
        sp = _FakeStorageProvider()
        await sp.get_storage(LLMProvider).create(_make_llm_provider())

        ctor = MagicMock(return_value=MagicMock())
        registry = ProviderRegistry(sp, llm_factory=lambda p: ctor(p))

        first = await registry.get_llm("anthropic-1")
        second = await registry.get_llm("anthropic-1")
        assert first is second
        assert ctor.call_count == 1

    @pytest.mark.asyncio
    async def test_missing_provider_raises_not_found(self) -> None:
        sp = _FakeStorageProvider()
        registry = ProviderRegistry(sp, llm_factory=lambda p: MagicMock())
        with pytest.raises(NotFoundError, match="anthropic-1"):
            await registry.get_llm("anthropic-1")

    @pytest.mark.asyncio
    async def test_invalidate_drops_cache_and_calls_aclose(self) -> None:
        sp = _FakeStorageProvider()
        await sp.get_storage(LLMProvider).create(_make_llm_provider())

        adapter = MagicMock()
        adapter.aclose = AsyncMock()
        ctor = MagicMock(return_value=adapter)
        registry = ProviderRegistry(sp, llm_factory=lambda p: ctor(p))

        await registry.get_llm("anthropic-1")
        await registry.invalidate_llm("anthropic-1")

        adapter.aclose.assert_awaited_once()
        await registry.get_llm("anthropic-1")
        assert ctor.call_count == 2

    @pytest.mark.asyncio
    async def test_invalidate_unknown_id_is_noop(self) -> None:
        sp = _FakeStorageProvider()
        registry = ProviderRegistry(sp, llm_factory=lambda p: MagicMock())
        await registry.invalidate_llm("never-cached")


class TestEmbedderResolution:
    @pytest.mark.asyncio
    async def test_lookup_constructs_and_caches(self) -> None:
        sp = _FakeStorageProvider()
        await sp.get_storage(EmbeddingProvider).create(_make_embedding_provider())

        ctor = MagicMock(return_value=MagicMock())
        registry = ProviderRegistry(sp, embedder_factory=lambda p: ctor(p))

        first = await registry.get_embedder("hf-1")
        second = await registry.get_embedder("hf-1")
        assert first is second
        assert ctor.call_count == 1


class TestToolsetDispatchDefault:
    @pytest.mark.asyncio
    async def test_default_factory_constructs_mcp_provider(self) -> None:
        from matrix.model.provider import (
            McpConfig,
            StdioConfig,
            Toolset,
            ToolsetProviderType,
            TransportType,
        )
        from matrix.toolset.mcp import McpToolsetProvider

        sp = _FakeStorageProvider()
        await sp.get_storage(Toolset).create(
            Toolset(
                id="t1",
                provider=ToolsetProviderType.MCP,
                config=McpConfig(
                    transport=TransportType.STDIO,
                    config=StdioConfig(command=["x"]),
                ),
            )
        )
        registry = ProviderRegistry(sp)
        provider = await registry.get_toolset("t1")
        assert isinstance(provider, McpToolsetProvider)


class TestCrossEncoderResolution:
    @pytest.mark.asyncio
    async def test_lookup_constructs_and_caches(self) -> None:
        sp = _FakeStorageProvider()
        await sp.get_storage(CrossEncoderProvider).create(_make_cross_encoder_provider())

        ctor = MagicMock(return_value=MagicMock())
        registry = ProviderRegistry(sp, cross_encoder_factory=lambda p: ctor(p))

        first = await registry.get_cross_encoder("ce-1")
        second = await registry.get_cross_encoder("ce-1")
        assert first is second
        assert ctor.call_count == 1

    @pytest.mark.asyncio
    async def test_missing_provider_raises_not_found(self) -> None:
        sp = _FakeStorageProvider()
        registry = ProviderRegistry(sp, cross_encoder_factory=lambda p: MagicMock())
        with pytest.raises(NotFoundError, match="ce-missing"):
            await registry.get_cross_encoder("ce-missing")

    @pytest.mark.asyncio
    async def test_invalidate_drops_cache_and_calls_aclose(self) -> None:
        sp = _FakeStorageProvider()
        await sp.get_storage(CrossEncoderProvider).create(_make_cross_encoder_provider())

        adapter = MagicMock()
        adapter.aclose = AsyncMock()
        registry = ProviderRegistry(sp, cross_encoder_factory=lambda p: adapter)

        await registry.get_cross_encoder("ce-1")
        await registry.invalidate_cross_encoder("ce-1")
        adapter.aclose.assert_awaited_once()


class TestEmbedderInvalidation:
    @pytest.mark.asyncio
    async def test_invalidate_drops_cache_and_calls_aclose(self) -> None:
        sp = _FakeStorageProvider()
        await sp.get_storage(EmbeddingProvider).create(_make_embedding_provider())

        adapter = MagicMock()
        adapter.aclose = AsyncMock()
        registry = ProviderRegistry(sp, embedder_factory=lambda p: adapter)

        await registry.get_embedder("hf-1")
        await registry.invalidate_embedder("hf-1")
        adapter.aclose.assert_awaited_once()


class TestToolsetInvalidation:
    @pytest.mark.asyncio
    async def test_invalidate_unknown_id_is_noop(self) -> None:
        sp = _FakeStorageProvider()
        registry = ProviderRegistry(sp, toolset_factory=lambda t: MagicMock())
        await registry.invalidate_toolset("never-cached")


class TestAclose:
    @pytest.mark.asyncio
    async def test_aclose_calls_each_cached_adapter_and_clears(self) -> None:
        sp = _FakeStorageProvider()
        await sp.get_storage(LLMProvider).create(_make_llm_provider())
        await sp.get_storage(EmbeddingProvider).create(_make_embedding_provider())

        llm_adapter = MagicMock()
        llm_adapter.aclose = AsyncMock()
        emb_adapter = MagicMock()
        emb_adapter.aclose = AsyncMock()

        registry = ProviderRegistry(
            sp,
            llm_factory=lambda p: llm_adapter,
            embedder_factory=lambda p: emb_adapter,
        )
        await registry.get_llm("anthropic-1")
        await registry.get_embedder("hf-1")

        await registry.aclose()
        llm_adapter.aclose.assert_awaited_once()
        emb_adapter.aclose.assert_awaited_once()
